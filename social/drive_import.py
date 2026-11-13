"""Read-only Google Drive ingestion, with durable pagination and immutable assets."""

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm.exc import StaleDataError

from .db import now
from .models import Asset, DriveImport, DriveSource
from .monitoring import event
from .provenance import source_error
from .rights import owner_value
from .storage import ingest

API = "https://www.googleapis.com/drive/v3/files"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"
FOLDER_MIME = "application/vnd.google-apps.folder"
FILE_FIELDS = "id,name,mimeType,size,md5Checksum,modifiedTime,version,parents,capabilities(canDownload)"
ID = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")


class DriveError(ValueError):
    """Safe to display; never contains upstream response bodies or credentials."""


class ImportMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    source: Literal["company", "ai_generated", "ai_edited"]
    rights: Literal["public", "not_public"] = "not_public"
    owner: str = Field(default="", max_length=200)
    tags: dict = Field(default_factory=dict)
    generator: str = Field(default="", max_length=200)
    prompt: str = ""
    rights_expire_at: datetime | None = None
    customer_permission: bool = False
    permission_reference: str = ""

    @model_validator(mode="after")
    def provenance(self):
        self.generator, self.prompt = self.generator.strip(), self.prompt.strip()
        self.owner = owner_value(self.owner)
        if error := source_error(self.source, self.generator, self.prompt, self.tags):
            raise ValueError(error)
        if self.customer_permission and not self.permission_reference.strip():
            raise ValueError("Customer permission requires an evidence reference")
        if self.source != "company" and not self.tags.get("asset_type"):
            self.tags = self.tags | {"asset_type": "ai_concept"}
        if not self.tags.get("product_family") and self.tags.get("product"):
            self.tags = self.tags | {"product_family": self.tags["product"]}
        if self.rights_expire_at and self.rights_expire_at.tzinfo:
            self.rights_expire_at = self.rights_expire_at.astimezone(timezone.utc).replace(tzinfo=None)
        return self


def folder_id(value):
    value = str(value).strip()
    if value.startswith("https://"):
        parsed = urlparse(value)
        if parsed.netloc != "drive.google.com":
            raise DriveError("Enter a Google Drive folder link or folder ID")
        match = re.fullmatch(r"/drive/(?:u/\d+/)?folders/([A-Za-z0-9_-]+)/?", parsed.path)
        value = match[1] if match else parse_qs(parsed.query).get("id", [""])[0]
    if not ID.fullmatch(value):
        raise DriveError("Enter a Google Drive folder link or folder ID")
    return value


def credential_info(settings):
    raw = settings.google_drive_credentials_json.get_secret_value()
    if raw:
        return json.loads(raw)
    path = settings.google_drive_credentials_file or settings.google_application_credentials
    return json.loads(Path(path).read_text(encoding="utf-8")) if path else None


def identity(settings):
    try:
        info = credential_info(settings)
        return info.get("client_email", "") if info and info.get("type") == "service_account" else ""
    except (OSError, ValueError, AttributeError):
        return ""


def authorized_session(settings):
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    info = credential_info(settings)
    if info is not None:
        credentials, _ = google.auth.load_credentials_from_dict(info, scopes=[SCOPE])
    else:
        credentials, _ = google.auth.default(scopes=[SCOPE])
    return AuthorizedSession(credentials, refresh_timeout=15)


class DriveClient:
    def __init__(self, settings, session=None):
        self.settings = settings
        self.session = session or authorized_session(settings)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.session.close()

    def request(self, file_id="", **kwargs):
        if file_id and not ID.fullmatch(file_id):
            raise DriveError("Google Drive returned an invalid file ID")
        response = self.session.get(
            API + ("/" + file_id if file_id else ""),
            timeout=self.settings.http_timeout_seconds,
            allow_redirects=False,
            **kwargs,
        )
        if response.status_code != 200:
            status = response.status_code
            response.close()
            if status in {401, 403, 404}:
                raise DriveError(
                    "Drive access failed. Check the service account, Drive API and folder sharing permissions."
                )
            raise DriveError("Google Drive is temporarily unavailable; importing will retry automatically.")
        return response

    def folder(self, key):
        with self.request(
            key, params={"fields": "id,name,mimeType,driveId,trashed", "supportsAllDrives": "true"}
        ) as response:
            folder = response.json()
        if folder.get("mimeType") != FOLDER_MIME or folder.get("trashed"):
            raise DriveError("Choose an accessible Google Drive folder that is not in the trash")
        return folder

    def listing(self, key, token=None, drive_id="", name=None):
        if not ID.fullmatch(key):
            raise DriveError("Google Drive returned an invalid folder ID")
        query = f"'{key}' in parents and trashed = false"
        if name is not None:
            escaped = name.replace("\\", "\\\\").replace("'", "\\'")
            query += f" and name = '{escaped}'"
        params = {
            "q": query,
            "pageSize": 100,
            "spaces": "drive",
            "fields": f"nextPageToken,incompleteSearch,files({FILE_FIELDS})",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if token:
            params["pageToken"] = token
        if drive_id:
            params.update(corpora="drive", driveId=drive_id)
        with self.request(params=params) as response:
            result = response.json()
        if result.get("incompleteSearch"):
            raise DriveError("Drive returned an incomplete folder listing; importing will retry.")
        return result

    def download(self, file, limit):
        if file.get("capabilities", {}).get("canDownload") is False:
            raise DriveError("Downloading this file is disabled in Google Drive")
        size = int(file.get("size", 0))
        if not 0 < size <= limit:
            raise DriveError("File is empty or exceeds the import size limit")
        with self.request(
            file["id"], params={"alt": "media", "supportsAllDrives": "true"}, stream=True
        ) as response:
            data = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                if len(data) + len(chunk) > limit:
                    raise DriveError("File exceeds the import size limit")
                data.extend(chunk)
        if len(data) != size or (
            file.get("md5Checksum")
            and hashlib.md5(data, usedforsecurity=False).hexdigest() != file["md5Checksum"]
        ):
            raise DriveError("File changed during download or was incomplete; importing will retry")
        return bytes(data)

    def manifest(self, file, parent, drive_id):
        result = self.listing(parent, drive_id=drive_id, name=file["name"] + ".json")
        files = result.get("files", [])
        if len(files) != 1 or result.get("nextPageToken"):
            raise DriveError("Mixed-media imports need exactly one matching <filename>.json source manifest")
        try:
            return json.loads(self.download(files[0], 65536))
        except (UnicodeError, json.JSONDecodeError):
            raise DriveError("The source manifest must contain valid UTF-8 JSON") from None


def save_source(db, form):
    key = folder_id(form.get("folder", ""))
    mode = str(form.get("source", ""))
    try:
        values = {
            "source": mode,
            "rights": form.get("rights", "not_public"),
            "owner": str(form.get("owner", "")).strip(),
            "tags": json.loads(str(form.get("tags", "{}")) or "{}"),
            "generator": str(form.get("generator", "")).strip(),
            "prompt": str(form.get("prompt", "")).strip(),
        }
        # Mixed folders supply the source, AI creation details and permissions per file.
        validated = ImportMetadata.model_validate(
            values | ({"source": "company"} if mode == "manifest" else {})
        )
    except (ValueError, TypeError):
        raise DriveError(
            "Choose a source and usage rights, enter valid tags, and supply model/prompt for AI media"
        ) from None
    source = db.get(DriveSource, 1)
    if source is None:
        source = DriveSource(id=1)
        db.add(source)
    source.folder_id = key
    source.folder_name = ""
    source.enabled = True
    source.recursive = form.get("recursive") == "on"
    source.defaults = validated.model_dump(mode="json") | {"source": mode}
    source.scan_state = {}
    source.next_poll_at = now()
    source.error = ""
    source.last_completed_at = None
    try:
        db.commit()
    except StaleDataError:
        db.rollback()
        raise DriveError("Import progress changed while saving; please save the settings again") from None
    return source


def panel(db, settings):
    source = db.get(DriveSource, 1)
    counts = dict(db.execute(select(DriveImport.status, func.count()).group_by(DriveImport.status)).all())
    return {
        "source": source,
        "email": identity(settings),
        "counts": counts,
        "recent": db.scalars(select(DriveImport).order_by(DriveImport.checked_at.desc()).limit(20)).all(),
        "interval": settings.google_drive_poll_seconds,
    }


def import_file(db, settings, source, client, file, parent, drive_id):
    values = dict(source.defaults)
    if values.get("source") == "manifest":
        values.pop("source")
        manifest = client.manifest(file, parent, drive_id)
        if not isinstance(manifest, dict):
            raise DriveError("The source manifest must be a JSON object")
        values.update(manifest)
    try:
        metadata = ImportMetadata.model_validate(values)
    except ValidationError:
        raise DriveError(
            "Source details are invalid: check source, rights, AI model/prompt and customer permission evidence"
        ) from None
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "content": file.get("md5Checksum")
                or [file.get("version"), file.get("modifiedTime"), file.get("size")],
                "metadata": metadata.model_dump(mode="json"),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    record = db.get(DriveImport, file["id"])
    if record and record.fingerprint == fingerprint:
        if record.status == "imported" or (record.retry_at and record.retry_at > now()):
            return
    data = client.download(file, settings.max_upload_bytes)
    # Reuse this file's asset when only Drive's version/modified timestamp changed.
    checksum = hashlib.sha256(data).hexdigest()
    tags = metadata.tags | {"google_drive_file_id": file["id"], "google_drive_folder_id": source.folder_id}
    arguments = metadata.model_dump() | {"tags": tags}
    arguments["generator"] = arguments["generator"] or None
    arguments["prompt"] = arguments["prompt"] or None
    previous = db.get(Asset, record.asset_id) if record and record.asset_id else None
    same = (
        previous
        and previous.checksum == checksum
        and all(getattr(previous, name) == value for name, value in arguments.items())
    )
    name = re.sub(r"[/\\]", "_", file["name"])
    if len(name) > 255:
        raise DriveError("Rename this file to 255 characters or fewer before importing")
    asset = previous if same else ingest(db, settings, data, name, **arguments)
    if record is None:
        record = DriveImport(file_id=file["id"])
        db.add(record)
    record.folder_id, record.name = source.folder_id, name
    record.fingerprint, record.asset_id = fingerprint, asset.id
    record.status, record.error, record.retry_at = "imported", "", None
    record.checked_at = now()


def failed_file(db, source, file, message):
    record = db.get(DriveImport, file["id"])
    if record is None:
        record = DriveImport(file_id=file["id"], fingerprint="")
        db.add(record)
    record.folder_id, record.name = source.folder_id, file["name"][:255]
    record.status, record.error = "error", message
    record.checked_at, record.retry_at = now(), now() + timedelta(minutes=5)


def poll(db, settings, client_factory=DriveClient):
    source = db.get(DriveSource, 1)
    if not source or not source.enabled or (source.next_poll_at and source.next_poll_at > now()):
        return
    try:
        with client_factory(settings) as client:
            state = deepcopy(source.scan_state)
            if not state:
                folder = client.folder(source.folder_id)
                source.folder_name = folder["name"][:255]
                state = {
                    "folders": [source.folder_id],
                    "seen": [source.folder_id],
                    "pending": [],
                    "token": None,
                    "drive_id": folder.get("driveId", ""),
                }
            if not state["pending"] and state["folders"]:
                parent = state["folders"][0]
                page = client.listing(parent, state["token"], state["drive_id"])
                state["parent"] = parent
                state["pending"] = page.get("files", [])
                state["token"] = page.get("nextPageToken")
                if not state["token"]:
                    state["folders"].pop(0)
            source.last_checked_at, source.error = now(), ""
            for _ in range(settings.google_drive_batch_size):
                if not state["pending"]:
                    break
                file = state["pending"][0]
                if file.get("mimeType") == FOLDER_MIME:
                    if source.recursive and file["id"] not in state["seen"]:
                        state["folders"].append(file["id"])
                        state["seen"].append(file["id"])
                elif file.get("mimeType", "").startswith(("image/", "video/", "audio/")):
                    try:
                        with db.begin_nested():
                            import_file(
                                db, settings, source, client, file, state["parent"], state["drive_id"]
                            )
                    except DriveError as exc:
                        failed_file(db, source, file, str(exc))
                    except StaleDataError:
                        raise
                    except Exception as exc:
                        event("drive_file_failed", error_type=type(exc).__name__)
                        failed_file(
                            db,
                            source,
                            file,
                            "Could not import this file. Check its media format, source details and storage connection.",
                        )
                state["pending"].pop(0)
                source.scan_state = deepcopy(state)
                db.commit()
            if not state["pending"] and not state["folders"]:
                source.scan_state = {}
                source.last_completed_at = now()
                source.next_poll_at = now() + timedelta(seconds=settings.google_drive_poll_seconds)
            else:
                source.scan_state = state
                source.next_poll_at = now()
            db.commit()
    except StaleDataError:
        db.rollback()  # A save/pause in the UI wins over an in-flight import.
    except Exception as exc:
        db.rollback()
        source = db.get(DriveSource, 1)
        source.error = (
            str(exc)
            if isinstance(exc, DriveError)
            else "Could not connect to Google Drive. Check credentials, API access and folder sharing."
        )
        source.scan_state = {}  # Restart safely after expired page tokens and revoked access.
        source.next_poll_at = now() + timedelta(seconds=settings.google_drive_poll_seconds)
        db.commit()
        event("drive_import_failed", error_type=type(exc).__name__)


def tick(factory, settings):
    with factory() as db:
        poll(db, settings)
