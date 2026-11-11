"""Read-only delivery of ingested media from a private S3-compatible bucket."""

import re

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select

from .models import Asset

router = APIRouter()
MEDIA_KEY = re.compile(r"[a-f0-9]{64}\.(jpg|png|gif|webp|mp4|mov|webm|mp3|wav|m4a|ogg|flac)")
MIME = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "webm": "video/webm",
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "m4a": "audio/mp4",
    "ogg": "audio/ogg",
    "flac": "audio/flac",
}


def selected_range(value, size):
    """Resolve one byte range; reject malformed, multiple and unsatisfiable ranges."""
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value) if len(value) <= 80 else None
    if not match or not size or not any(match.groups()):
        raise ValueError("Invalid range")
    first, last = match.groups()
    if not first:
        suffix = int(last)
        if not suffix:
            raise ValueError("Invalid range")
        return max(0, size - suffix), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or start > end:
        raise ValueError("Invalid range")
    return start, end


class ObjectResponse(StreamingResponse):
    def __init__(self, body, **kwargs):
        self.body_stream = body
        super().__init__(body.iter_chunks(chunk_size=64 * 1024), **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.body_stream.close()


@router.api_route("/media/{key}", methods=["GET", "HEAD"])
def public_media(key: str, request: Request):
    match = MEDIA_KEY.fullmatch(key)
    if not match:
        raise HTTPException(404, "Media not found")
    with request.app.state.factory() as db:
        # Do not expose arbitrary existing bucket objects, even with a valid-looking key.
        exists = db.scalar(select(Asset.id).where(Asset.storage_key == key).limit(1))
    if not exists:
        raise HTTPException(404, "Media not found")

    store = request.app.state.media_store
    try:
        metadata = store.head(key)
        size = metadata["ContentLength"]
        headers = {
            "Content-Length": str(size),
            "Accept-Ranges": "bytes",
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
            "ETag": '"' + key.split(".")[0] + '"',
        }
        mime = MIME[match.group(1)]
        if request.headers.get("if-none-match") in {headers["ETag"], "*"}:
            return Response(
                status_code=304, headers={k: v for k, v in headers.items() if k != "Content-Length"}
            )
        if request.method == "HEAD":
            return Response(headers=headers, media_type=mime)
        byte_range, status = None, 200
        requested = request.headers.get("range")
        if_range = request.headers.get("if-range")
        if requested and (not if_range or if_range == headers["ETag"]):
            try:
                start, end = selected_range(requested, size)
            except ValueError:
                return Response(
                    status_code=416, headers={"Content-Range": f"bytes */{size}", "Cache-Control": "no-store"}
                )
            byte_range, status = f"bytes={start}-{end}", 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            headers["Content-Length"] = str(end - start + 1)
        result = store.open(key, byte_range)
        body = result["Body"]
        # A provider must honor ranges; never return incorrect video response headers.
        if result["ContentLength"] != int(headers["Content-Length"]) or (
            byte_range and result.get("ContentRange") != headers["Content-Range"]
        ):
            body.close()
            raise HTTPException(503, "Media temporarily unavailable", headers={"Cache-Control": "no-store"})
        return ObjectResponse(body, status_code=status, headers=headers, media_type=mime)
    except ClientError as exc:
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        missing = status == 404 or exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "NotFound"}
        raise HTTPException(
            404 if missing else 503,
            "Media not found" if missing else "Media temporarily unavailable",
            headers={"Cache-Control": "no-store"},
        ) from None
    except BotoCoreError:
        raise HTTPException(
            503, "Media temporarily unavailable", headers={"Cache-Control": "no-store"}
        ) from None
