import base64
import hashlib
import io
import json
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import av
import boto3
from botocore.config import Config
from PIL import Image

from .models import Asset
from .rights import owner_value, usage_rights


class MediaStore(Protocol):
    def put(self, key: str, data: bytes, mime: str): ...
    def read(self, key: str) -> bytes: ...
    def url(self, key: str) -> str: ...


class LocalStore:
    def __init__(self, settings):
        self.root = settings.media_dir.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.base = (settings.media_base_url or "http://localhost:8000/media").rstrip("/")

    def path(self, key):
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Invalid storage key")
        return path

    def put(self, key, data, mime):
        path = self.path(key)
        if path.exists() and path.read_bytes() != data:
            raise ValueError("Immutable asset collision")
        path.write_bytes(data)  # Original bytes including C2PA/IPTC are never transcoded.

    def read(self, key):
        return self.path(key).read_bytes()

    def url(self, key):
        return f"{self.base}/{quote(key)}"


class S3Store:
    def __init__(self, settings):
        self.bucket = settings.s3_bucket
        self.base = settings.media_base_url.rstrip("/")
        if not self.bucket or not self.base:
            raise ValueError("S3_BUCKET and MEDIA_BASE_URL required")
        self.client = boto3.client(
            "s3",
            region_name=settings.s3_region,
            endpoint_url=settings.s3_endpoint_url or None,
            aws_access_key_id=settings.aws_access_key_id.get_secret_value() or None,
            aws_secret_access_key=settings.aws_secret_access_key.get_secret_value() or None,
            config=Config(
                signature_version="s3v4",
                connect_timeout=10,
                read_timeout=30,
                retries={"mode": "standard", "total_max_attempts": 3},
                # S3-compatible endpoints may not accept AWS checksum trailers/chunked bodies.
                request_checksum_calculation="when_required"
                if settings.s3_endpoint_url
                else "when_supported",
                response_checksum_validation="when_required"
                if settings.s3_endpoint_url
                else "when_supported",
            ),
        )

    def put(self, key, data, mime):
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentLength=len(data),
            ContentMD5=base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode("ascii"),
            ContentType=mime,
            Metadata={"sha256": hashlib.sha256(data).hexdigest()},
        )

    def read(self, key):
        body = self.open(key)["Body"]
        try:
            return body.read()
        finally:
            body.close()

    def head(self, key):
        return self.client.head_object(Bucket=self.bucket, Key=key)

    def open(self, key, byte_range=None):
        arguments = {"Bucket": self.bucket, "Key": key}
        if byte_range:
            arguments["Range"] = byte_range
        return self.client.get_object(**arguments)

    def url(self, key):
        return f"{self.base}/{quote(key)}"


def storage(settings):
    return S3Store(settings) if settings.storage_backend == "s3" else LocalStore(settings)


def inspect_media(data, filename, settings):
    try:
        return _inspect_media(data, filename, settings)
    except av.error.FFmpegError:
        raise ValueError(
            "The audio/video file could not be decoded. Export a new MP4 (H.264/AAC) or audio file and retry."
        ) from None
    except Image.DecompressionBombError:
        raise ValueError(
            "The image dimensions are too large to process. Upload a smaller image rendition."
        ) from None


def _inspect_media(data, filename, settings):
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            return {
                "mime": Image.MIME[im.format],
                "width": im.width,
                "height": im.height,
                "bytes": len(data),
                "duration": 0,
                "frames": getattr(im, "n_frames", 1),
                "metadata_keys": sorted(im.info.keys()),
                "mode": im.mode,
            }
    except (OSError, SyntaxError):
        pass
    suffix = Path(filename).suffix.lower()
    audio_mimes = {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
    }
    if suffix in audio_mimes:
        with av.open(io.BytesIO(data)) as container:
            if not container.streams.audio or container.streams.video:
                raise ValueError("Expected an audio-only file")
            stream = container.streams.audio[0]
            samples = sum(frame.samples for frame in container.decode(stream))
            rate = stream.codec_context.sample_rate
            if not samples or not rate:
                raise ValueError("Empty or invalid audio")
            return {
                "mime": audio_mimes[suffix],
                "width": 0,
                "height": 0,
                "bytes": len(data),
                "duration": samples / rate,
                "codec": stream.codec_context.name,
                "sample_rate": rate,
                "metadata_keys": sorted(container.metadata.keys()),
            }
    if suffix not in {".mp4", ".mov", ".webm"}:
        raise ValueError("Unsupported or invalid media")
    with av.open(io.BytesIO(data)) as container:
        if not container.streams.video:
            raise ValueError("Expected a video stream")
        video = container.streams.video[0]
        codec = video.codec_context
        # Decode to catch corrupted streams, inspecting without rewriting the source.
        frame_count = sum(1 for _ in container.decode(video))
        if not frame_count or not container.duration:
            raise ValueError("Empty or invalid video stream")
        return {
            "mime": {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}[
                Path(filename).suffix.lower()
            ],
            "width": codec.width,
            "height": codec.height,
            "bytes": len(data),
            "duration": container.duration / av.time_base,
            "codec": codec.name,
            "fps": float(video.average_rate or 0),
            "pixel_format": codec.format.name,
            "bit_rate": container.bit_rate or 0,
            "audio_codecs": [s.codec_context.name for s in container.streams.audio],
            "metadata_keys": sorted(container.metadata.keys()),
            "decoded_frames": frame_count,
        }


def ingest(
    db,
    settings,
    data,
    filename,
    *,
    source="company",
    generator=None,
    prompt=None,
    tags=None,
    rights="",
    owner="",
    rights_expire_at=None,
    customer_permission=False,
    permission_reference="",
    demo_only=False,
):
    if source not in {"company", "ai_generated", "ai_edited"}:
        raise ValueError("Invalid asset source")
    if not isinstance(filename, str) or not filename.strip() or len(Path(filename).name) > 255:
        raise ValueError("Choose a media file with a filename of 255 characters or fewer")
    if tags is not None and not isinstance(tags, dict):
        raise ValueError("Tags must be a JSON object")
    if generator and len(generator) > 200:
        raise ValueError("AI model / generator must be 200 characters or fewer")
    if not rights.strip() or not data or len(data) > settings.max_upload_bytes:
        raise ValueError("Usage rights and a nonempty file within MAX_UPLOAD_BYTES are required")
    if source != "company" and (not generator or not prompt):
        raise ValueError("AI assets require generator/model and prompt provenance")
    if customer_permission and not permission_reference.strip():
        raise ValueError("Customer permission requires an evidence reference")
    if rights_expire_at and rights_expire_at.tzinfo:
        from datetime import timezone

        rights_expire_at = rights_expire_at.astimezone(timezone.utc).replace(tzinfo=None)
    media = inspect_media(data, filename, settings)
    checksum = hashlib.sha256(data).hexdigest()
    extension = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/webm": ".webm",
        "audio/mpeg": ".mp3",
        "audio/wav": ".wav",
        "audio/mp4": ".m4a",
        "audio/ogg": ".ogg",
        "audio/flac": ".flac",
    }.get(media["mime"])
    if not extension:
        raise ValueError("Unsupported format")
    key = checksum + extension
    storage(settings).put(key, data, media["mime"])
    asset = Asset(
        source=source,
        storage_key=key,
        original_name=Path(filename).name,
        checksum=checksum,
        generator=generator,
        prompt=prompt,
        tags=tags or {},
        rights=rights,
        owner=owner_value(owner),
        rights_expire_at=rights_expire_at,
        customer_permission=customer_permission,
        permission_reference=permission_reference,
        media=media,
        demo_only=demo_only,
    )
    db.add(asset)
    db.flush()
    return asset


def watch_folder(db, settings):
    """Atomic delivery convention: media first, then <filename>.json manifest last."""
    root = settings.watch_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    for manifest in sorted(root.glob("*.json")):
        if manifest.name.endswith((".done.json", ".error.json")):
            continue
        try:
            values = json.loads(manifest.read_text(encoding="utf-8"))
            path = (root / values.pop("filename")).resolve()
            if not path.is_relative_to(root):
                raise ValueError("Invalid watched file path")
            values["rights"] = usage_rights(values.get("rights"))
            if values.get("rights_expire_at"):
                from datetime import datetime

                values["rights_expire_at"] = datetime.fromisoformat(values["rights_expire_at"])
            ingest(db, settings, path.read_bytes(), path.name, **values)
            db.commit()
            manifest.rename(manifest.with_suffix(".done.json"))
        except Exception as exc:
            db.rollback()
            from .monitoring import alert

            alert(db, "asset_ingest", f"Manifest {manifest.name} failed: {type(exc).__name__}", str(manifest))
            db.commit()
            manifest.rename(manifest.with_suffix(".error.json"))
