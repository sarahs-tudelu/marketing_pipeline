import hashlib
import io
import json
from copy import deepcopy
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from social.app import create_app
from social.db import now, sessions
from social.drive_import import DriveClient, DriveError, folder_id, poll, save_source
from social.models import Asset, DriveImport, DriveSource
from social.storage import LocalStore


def photo(color="red"):
    output = io.BytesIO()
    Image.new("RGB", (40, 40), color).save(output, format="PNG")
    return output.getvalue()


def file_info(key, data=None, **overrides):
    data = photo() if data is None else data
    return {
        "id": key,
        "name": key + ".png",
        "mimeType": "image/png",
        "size": str(len(data)),
        "md5Checksum": hashlib.md5(data, usedforsecurity=False).hexdigest(),
        **overrides,
    }


class FakeDrive:
    def __init__(self, files, bodies=None, pages=None):
        self.files = files
        self.bodies = bodies or {}
        self.pages = pages or {}
        self.downloads = []
        self.lists = []
        self.manifests = {}

    def __call__(self, settings):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def folder(self, key):
        return {"name": "Marketing media", "driveId": "shared-drive"}

    def listing(self, key, token=None, drive_id=""):
        self.lists.append((key, token, drive_id))
        return deepcopy(self.pages.get((key, token), {"files": self.files if key == "root-folder" else []}))

    def download(self, file, limit):
        self.downloads.append(file["id"])
        return self.bodies.get(file["id"], photo())

    def manifest(self, file, parent, drive_id):
        if file["id"] not in self.manifests:
            raise DriveError("Missing source manifest")
        return self.manifests[file["id"]]


def configure(db, **fields):
    return save_source(db, {"folder": "root-folder", "source": "company", "recursive": "on", **fields})


def due(db):
    source = db.get(DriveSource, 1)
    source.next_poll_at = now() - timedelta(seconds=1)
    db.commit()


def test_imports_original_bytes_and_never_reimports_unchanged_files(db, settings):
    configure(db)
    drive = FakeDrive([file_info("a")])
    poll(db, settings, drive)
    asset = db.scalar(select(Asset))
    assert asset.rights == "not_public"
    assert asset.source == "company"
    assert asset.tags["google_drive_file_id"] == "a"
    assert LocalStore(settings).read(asset.storage_key) == photo()
    assert db.get(DriveImport, "a").asset_id == asset.id
    poll(db, settings, drive)
    assert len(drive.lists) == 1  # Poll interval enforced.
    due(db)
    poll(db, settings, drive)
    assert drive.downloads == ["a"]
    assert len(db.scalars(select(Asset)).all()) == 1
    drive.files = []
    due(db)
    poll(db, settings, drive)
    assert db.get(Asset, asset.id)  # Removing a Drive file never removes an asset.


def test_changed_bytes_create_new_asset_and_keep_old_version(db, settings):
    configure(db)
    drive = FakeDrive([file_info("a")])
    poll(db, settings, drive)
    original = db.get(DriveImport, "a").asset_id
    drive.files = [file_info("a", photo("blue"))]
    drive.bodies["a"] = photo("blue")
    due(db)
    poll(db, settings, drive)
    assert db.get(DriveImport, "a").asset_id != original
    assert LocalStore(settings).read(db.get(Asset, original).storage_key) == photo()
    assert len(db.scalars(select(Asset)).all()) == 2


def test_missing_drive_checksum_uses_content_and_metadata_to_avoid_duplicates(db, settings):
    configure(db, tags='{"product": "partitions"}')
    first = file_info("a", version="1", modifiedTime="2026-11-01T00:00:00Z")
    first.pop("md5Checksum")
    drive = FakeDrive([first])
    poll(db, settings, drive)
    original = db.get(DriveImport, "a").asset_id
    first["version"] = "2"
    first["modifiedTime"] = "2026-11-02T00:00:00Z"
    due(db)
    poll(db, settings, drive)
    assert db.get(DriveImport, "a").asset_id == original
    assert len(db.scalars(select(Asset)).all()) == 1


def test_resume_after_crash_between_last_item_and_scan_completion(db, settings):
    source = configure(db)
    source.scan_state = {"folders": [], "pending": [], "seen": ["root-folder"], "token": None, "drive_id": ""}
    db.commit()
    drive = FakeDrive([])
    poll(db, settings, drive)
    assert source.last_completed_at and not source.error
    assert not drive.lists


def test_pause_from_another_session_supersedes_stale_worker(db, settings):
    source = configure(db)
    with sessions(db.get_bind())() as other:
        current = other.get(DriveSource, 1)
        current.enabled = False
        other.commit()
    assert source.enabled  # Worker has an old snapshot.
    drive = FakeDrive([file_info("a")])
    poll(db, settings, drive)
    db.expire_all()
    assert not db.get(DriveSource, 1).enabled
    assert not db.scalar(select(Asset))


def test_pagination_and_nested_folders_resume_across_worker_sessions(db, settings):
    configure(db)
    settings.google_drive_batch_size = 1
    drive = FakeDrive(
        [],
        pages={
            ("root-folder", None): {
                "files": [file_info("a"), {"id": "sub", "mimeType": "application/vnd.google-apps.folder"}],
                "nextPageToken": "page2",
            },
            ("root-folder", "page2"): {"files": [file_info("b")]},
            ("sub", None): {"files": [file_info("c")]},
        },
    )
    factory = sessions(db.get_bind())
    for _ in range(4):
        with factory() as resumed:
            poll(resumed, settings, drive)
    db.expire_all()
    assert len(db.scalars(select(Asset)).all()) == 3
    assert drive.lists == [
        ("root-folder", None, "shared-drive"),
        ("root-folder", "page2", "shared-drive"),
        ("sub", None, "shared-drive"),
    ]
    assert db.get(DriveSource, 1).last_completed_at
    assert db.get(DriveSource, 1).scan_state == {}


def test_nonrecursive_ignores_folders_shortcuts_and_documents(db, settings):
    configure(db, recursive="")
    drive = FakeDrive(
        [
            {"id": "sub", "mimeType": "application/vnd.google-apps.folder"},
            {"id": "doc", "mimeType": "application/vnd.google-apps.document"},
            {"id": "link", "mimeType": "application/vnd.google-apps.shortcut"},
            file_info("a"),
        ]
    )
    poll(db, settings, drive)
    assert drive.downloads == ["a"]
    assert len(drive.lists) == 1


def test_bad_file_does_not_block_other_files_or_leak_provider_secrets(db, settings, monkeypatch, caplog):
    configure(db)
    drive = FakeDrive([file_info("bad"), file_info("good")])
    download = drive.download

    def fail_one(file, limit):
        if file["id"] == "bad":
            raise RuntimeError("private-token upstream-response")
        return download(file, limit)

    monkeypatch.setattr(drive, "download", fail_one)
    poll(db, settings, drive)
    assert db.get(DriveImport, "bad").status == "error"
    assert db.get(DriveImport, "good").status == "imported"
    assert "private-token" not in db.get(DriveImport, "bad").error + caplog.text
    monkeypatch.setattr(drive, "download", download)
    due(db)
    poll(db, settings, drive)
    assert db.get(DriveImport, "bad").status == "imported"


def test_storage_failure_rolls_back_asset_and_retries(db, settings, monkeypatch):
    configure(db)
    drive = FakeDrive([file_info("a")])
    original = LocalStore.put

    def fail(*args):
        raise OSError("private-storage-credentials")

    monkeypatch.setattr(LocalStore, "put", fail)
    poll(db, settings, drive)
    assert not db.scalar(select(Asset))
    assert db.get(DriveImport, "a").status == "error"
    monkeypatch.setattr(LocalStore, "put", original)
    due(db)
    poll(db, settings, drive)
    assert db.get(DriveImport, "a").status == "imported"


def test_mixed_sources_require_creation_details_and_preserve_permissions(db, settings):
    configure(db, source="manifest")
    drive = FakeDrive([file_info("ai"), file_info("customer"), file_info("missing")])
    drive.manifests = {
        "ai": {"source": "ai_generated", "generator": "example model", "prompt": "A concept"},
        "customer": {
            "source": "company",
            "tags": {"real_customer_content": True},
            "customer_permission": True,
            "permission_reference": "release-42",
        },
    }
    poll(db, settings, drive)
    ai = db.get(Asset, db.get(DriveImport, "ai").asset_id)
    assert ai.source == "ai_generated" and ai.prompt == "A concept"
    assert ai.tags["asset_type"] == "ai_concept"
    customer = db.get(Asset, db.get(DriveImport, "customer").asset_id)
    assert customer.customer_permission and customer.permission_reference == "release-42"
    assert db.get(DriveImport, "missing").status == "error"
    drive.manifests["ai"].pop("prompt")
    due(db)
    poll(db, settings, drive)
    assert db.get(DriveImport, "ai").status == "error"
    assert db.get(Asset, ai.id).prompt == "A concept"


def test_metadata_changes_create_new_records_without_rewriting_existing_rights(db, settings):
    configure(db)
    drive = FakeDrive([file_info("a")])
    poll(db, settings, drive)
    original = db.get(DriveImport, "a").asset_id
    configure(db, rights="public", owner="Tudelu")
    poll(db, settings, drive)
    assert db.get(Asset, original).rights == "not_public"
    current = db.get(Asset, db.get(DriveImport, "a").asset_id)
    assert current.rights == "public" and current.owner == "Tudelu"


def test_disabled_or_unconfigured_imports_make_no_network_calls(db, settings):
    def fail(settings):
        raise AssertionError("Network called")

    poll(db, settings, fail)
    source = configure(db)
    source.enabled = False
    db.commit()
    poll(db, settings, fail)
    assert not source.error


def test_connection_failure_is_sanitized_and_scheduled_for_retry(db, settings):
    source = configure(db)

    def fail(settings):
        raise OSError("private-key")

    poll(db, settings, fail)
    assert "private-key" not in source.error
    assert source.next_poll_at > now()
    assert not source.scan_state


@pytest.mark.parametrize("value", ["https://evil.example/folders/key", "abc' or true", "../key", ""])
def test_folder_id_rejects_untrusted_urls_and_queries(value):
    with pytest.raises(DriveError):
        folder_id(value)


@pytest.mark.parametrize(
    "value",
    [
        "folder-id",
        "https://drive.google.com/drive/folders/folder-id?usp=sharing",
        "https://drive.google.com/drive/u/0/folders/folder-id",
    ],
)
def test_folder_links_are_accepted(value):
    assert folder_id(value) == "folder-id"


class Response:
    status_code = 200

    def __init__(self, data=b"", result=None):
        self.data, self.result = data, result
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.closed = True

    def json(self):
        return self.result

    def iter_content(self, chunk_size):
        yield self.data


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_download_is_readonly_bounded_and_verifies_original_bytes(settings):
    response = Response(photo())
    session = Session(response)
    client = DriveClient(settings, session)
    assert client.download(file_info("a"), 10000) == photo()
    url, options = session.calls[0]
    assert url == "https://www.googleapis.com/drive/v3/files/a"
    assert options["params"] == {"alt": "media", "supportsAllDrives": "true"}
    assert options["allow_redirects"] is False and response.closed
    with pytest.raises(DriveError, match="size limit"):
        client.download(file_info("a"), 1)
    with pytest.raises(DriveError, match="changed during download"):
        client.download(file_info("a", photo("blue")), 10000)
    with pytest.raises(DriveError, match="disabled"):
        client.download(file_info("a", capabilities={"canDownload": False}), 10000)
    response.data = b"a" * 20000
    with pytest.raises(DriveError, match="size limit"):
        client.download(file_info("a"), 10000)


def test_drive_client_escapes_sidecar_name_and_supports_shared_drive_paging(settings):
    response = Response(result={"files": []})
    session = Session(response)
    client = DriveClient(settings, session)
    client.listing("folder", "next", "shared", "a'b\\c.png.json")
    query = session.calls[0][1]["params"]
    assert query["q"] == "'folder' in parents and trashed = false and name = 'a\\'b\\\\c.png.json'"
    assert query["pageToken"] == "next" and query["driveId"] == "shared"
    assert query["corpora"] == "drive" and query["includeItemsFromAllDrives"] == "true"
    response.result = {"incompleteSearch": True}
    with pytest.raises(DriveError, match="incomplete"):
        client.listing("folder")
    response.status_code = 403
    with pytest.raises(DriveError, match="sharing permissions"):
        client.listing("folder")
    assert response.closed


def test_library_setup_validation_csrf_and_pause_resume(db, settings):
    with TestClient(create_app(settings)) as client:
        response = client.get("/admin/assets")
        assert response.status_code == 200 and "Automatic Google Drive imports" in response.text
        assert 'value="not_public" selected' in response.text
        assert client.post("/forms/google-drive", data={"folder": "root"}).status_code == 403
        token = client.get("/api/session").json()["csrf"]
        values = {"csrf": token, "folder": "root-folder", "source": "company", "rights": "not_public"}
        response = client.post("/forms/google-drive", data=values | {"tags": "invalid"})
        assert response.status_code == 400 and 'value="root-folder"' in response.text
        response = client.post("/forms/google-drive", data=values)
        assert response.status_code == 200 and "Importing enabled" in response.text
        response = client.post("/forms/google-drive/pause", data={"csrf": token})
        assert "Paused" in response.text
        db.expire_all()
        assert not db.get(DriveSource, 1).enabled
        response = client.post("/forms/google-drive/resume", data={"csrf": token})
        assert "Importing enabled" in response.text
        db.expire_all()
        assert db.get(DriveSource, 1).enabled
        assert client.post("/forms/google-drive/pause", data={}).status_code == 403


def test_service_account_identity_shown_without_credential_contents(db, settings, tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(
        json.dumps(
            {
                "type": "service_account",
                "client_email": "importer@example.iam.gserviceaccount.com",
                "private_key": "secret-private-key",
            }
        )
    )
    settings.google_drive_credentials_file = str(path)
    with TestClient(create_app(settings)) as client:
        response = client.get("/admin/assets")
        assert "importer@example.iam.gserviceaccount.com" in response.text.replace("<wbr>", "")
        assert "secret-private-key" not in response.text
