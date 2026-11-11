import errno
import hashlib
import struct

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from social.app import create_app
from social.models import Asset
from social.seed import seed_media
from social.storage import LocalStore, ingest


def upload(client, data, name="clip.mp4", **fields):
    token = client.get("/api/session").json()["csrf"]
    return client.post(
        "/forms/assets",
        data={
            "csrf": token,
            "source": "company",
            "rights": "Company owned",
            "tags": '{"product":"company"}',
            "rights_expire_at": "2030-01-01T12:00",
            "customer_permission": "on",
            "permission_reference": "release-123",
            **fields,
        },
        files={"file": (name, data, "video/mp4")},
        follow_redirects=False,
    )


@pytest.mark.parametrize(
    "code,hint",
    [
        ("NoSuchBucket", "S3_BUCKET"),
        ("AccessDenied", "bucket write permissions"),
        ("InvalidAccessKeyId", "Spaces credentials"),
        ("SignatureDoesNotMatch", "Spaces credentials"),
        ("MissingContentLength", "fixed-length"),
        ("NotImplemented", "fixed-length"),
        ("BadDigest", "checksum"),
        ("SlowDown", "storage service status"),
    ],
)
def test_upload_storage_error_is_actionable_preserves_fields_and_rolls_back(
    db, settings, monkeypatch, caplog, code, hint
):
    caplog.set_level("INFO", logger="social")
    original_put = LocalStore.put

    def fail(*args):
        raise ClientError(
            {"Error": {"Code": code, "Message": "private-access-key private-provider-body"}}, "PutObject"
        )

    monkeypatch.setattr(LocalStore, "put", fail)
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        response = upload(client, seed_media()[1], name="clip<script>.mp4")
        assert response.status_code == 503 and hint in response.text
        assert 'role="alert"' in response.text and "select the file again" in response.text
        for value in ['value="Company owned"', 'value="release-123"', 'value="2030-01-01T12:00"']:
            assert value in response.text
        assert "clip&lt;script&gt;.mp4" in response.text
        assert "private-access-key" not in response.text + caplog.text
        assert "private-provider-body" not in response.text + caplog.text
        assert code in caplog.text
        assert not db.scalar(select(Asset))
        monkeypatch.setattr(LocalStore, "put", original_put)
        assert upload(client, seed_media()[1]).status_code == 303
    assert db.scalar(select(Asset))


@pytest.mark.parametrize(
    "error,hint",
    [
        (NoCredentialsError(), "Storage credentials are missing"),
        (EndpointConnectionError(endpoint_url="https://private-signed-url"), "Could not reach media storage"),
        (OSError(errno.ENOSPC, "private-path"), "ran out of temporary"),
        (PermissionError("private-path"), "cannot write"),
        (
            OperationalError("private-query", {}, RuntimeError("private-database-password")),
            "database connection",
        ),
    ],
)
def test_upload_other_failures_are_sanitized(db, settings, monkeypatch, error, hint):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr("social.app.ingest", fail)
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        response = upload(client, seed_media()[1])
    assert response.status_code == 503 and hint in response.text
    assert "private-" not in response.text


@pytest.mark.parametrize(
    "fields,hint",
    [
        ({"tags": "[]"}, "JSON object"),
        ({"tags": "invalid"}, "JSON object"),
        ({"rights_expire_at": "bad"}, "valid rights expiry"),
        ({"rights": ""}, "Usage rights"),
    ],
)
def test_upload_invalid_metadata_returns_form_without_saving(db, settings, fields, hint):
    with TestClient(create_app(settings)) as client:
        response = upload(client, seed_media()[1], **fields)
    assert response.status_code == 400 and hint in response.text
    assert not db.scalar(select(Asset))


def test_37000_kb_mp4_upload_is_under_limit_and_preserved(db, settings):
    video = seed_media()[1]
    # Valid ISO-BMFF free-space atom gives a realistic multipart/spooled-file size without a huge movie.
    padding_size = 37_000 * 1024 - len(video)
    data = video + struct.pack(">I4s", padding_size, b"free") + b"\0" * (padding_size - 8)
    with TestClient(create_app(settings)) as client:
        assert upload(client, data, name="37000-kb.mp4").status_code == 303
    asset = db.scalar(select(Asset))
    assert asset.media["bytes"] == len(data) and asset.media["mime"] == "video/mp4"
    assert asset.checksum == hashlib.sha256(data).hexdigest()
    assert LocalStore(settings).read(asset.storage_key) == data


def test_corrupt_mp4_missing_file_and_over_limit_return_actionable_errors(db, settings):
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        response = upload(client, b"invalid video bytes")
        assert response.status_code == 400 and "could not be decoded" in response.text
        response = client.post("/forms/assets", data={"csrf": client.get("/api/session").json()["csrf"]})
        assert response.status_code == 400 and "Choose a media file" in response.text
        settings.max_upload_bytes = 100
        response = upload(client, b"x" * 101)
        assert response.status_code == 400 and "upload limit" in response.text


def test_ingest_rejects_non_object_tags_before_storage(db, settings):
    with pytest.raises(ValueError, match="JSON object"):
        ingest(db, settings, seed_media()[0], "photo.jpg", tags=["not-an-object"], rights="Owned")
