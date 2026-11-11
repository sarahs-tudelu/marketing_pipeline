import base64
import hashlib
import io

import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber
from fastapi.testclient import TestClient
from pydantic import SecretStr

from social.app import create_app
from social.storage import S3Store, storage


@pytest.fixture
def spaces(settings, assets):
    raw = storage(settings).read(assets[0].storage_key)
    config = settings.model_copy(
        update={
            "storage_backend": "s3",
            "s3_bucket": "test-media",
            "s3_region": "nyc3",
            "s3_endpoint_url": "https://nyc3.digitaloceanspaces.com",
            "media_base_url": "https://media.example.org",
            "aws_access_key_id": SecretStr("mock-access"),
            "aws_secret_access_key": SecretStr("mock-secret"),
            "auth_mode": "basic",
            "admin_username": "admin",
            "admin_password": SecretStr("mock-admin"),
        }
    )
    app = create_app(config)
    with Stubber(app.state.media_store.client) as stub, TestClient(app) as client:
        yield config, stub, client, assets[0].storage_key, raw
        stub.assert_no_pending_responses()


def head(stub, key, size):
    stub.add_response(
        "head_object",
        {"ContentLength": size, "ContentType": "text/html"},
        {"Bucket": "test-media", "Key": key},
    )


def test_spaces_get_head_and_conditional_delivery_preserves_original_bytes(spaces):
    _, stub, client, key, raw = spaces
    for method in ("HEAD", "GET"):
        head(stub, key, len(raw))
        stream = io.BytesIO(raw)
        if method == "GET":
            stub.add_response(
                "get_object",
                {"Body": StreamingBody(stream, len(raw)), "ContentLength": len(raw)},
                {"Bucket": "test-media", "Key": key},
            )
        response = client.request(method, "/media/" + key)
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.headers["content-length"] == str(len(raw))
        assert "immutable" in response.headers["cache-control"]
        assert response.content == (raw if method == "GET" else b"")
        if method == "GET":
            assert stream.closed
    head(stub, key, len(raw))
    cached = client.get("/media/" + key, headers={"If-None-Match": response.headers["etag"]})
    assert cached.status_code == 304 and not cached.content


@pytest.mark.parametrize(
    "requested,first,last", [("bytes=1-3", 1, 3), ("bytes=-3", -3, -1), ("bytes=5-", 5, -1)]
)
def test_spaces_streams_video_compatible_byte_ranges(spaces, requested, first, last):
    _, stub, client, key, raw = spaces
    first = first if first >= 0 else len(raw) + first
    last = last if last >= 0 else len(raw) + last
    data = raw[first : last + 1]
    stream = io.BytesIO(data)
    head(stub, key, len(raw))
    expected_range = f"bytes {first}-{last}/{len(raw)}"
    stub.add_response(
        "get_object",
        {
            "Body": StreamingBody(stream, len(data)),
            "ContentLength": len(data),
            "ContentRange": expected_range,
        },
        {"Bucket": "test-media", "Key": key, "Range": f"bytes={first}-{last}"},
    )
    response = client.get("/media/" + key, headers={"Range": requested})
    assert response.status_code == 206 and response.content == data
    assert response.headers["content-range"] == expected_range
    assert response.headers["content-length"] == str(len(data))
    assert stream.closed


@pytest.mark.parametrize("requested", ["bytes=999999-", "bytes=4-1", "bytes=0-1,3-4", "bytes=-0", "bytes=-"])
def test_spaces_rejects_invalid_ranges_without_downloading(spaces, requested):
    _, stub, client, key, raw = spaces
    head(stub, key, len(raw))
    response = client.get("/media/" + key, headers={"Range": requested})
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(raw)}"
    assert response.headers["cache-control"] == "no-store"


def test_public_bucket_delivery_excludes_unregistered_objects_admin_and_mutations(spaces):
    _, _, client, key, _ = spaces
    for suffix in (".env", "credentials.json", "../.env", "a" * 64 + ".jpg"):
        assert client.get("/media/" + suffix).status_code in {401, 404}
    assert client.post("/media/" + key).status_code == 405
    assert client.get("/admin/connections").status_code == 401


@pytest.mark.parametrize(
    "code,status,expected", [("NoSuchKey", 404, 404), ("AccessDenied", 403, 503), ("SlowDown", 503, 503)]
)
def test_spaces_errors_are_sanitized_and_not_cached(spaces, code, status, expected):
    _, stub, client, key, _ = spaces
    stub.add_client_error(
        "head_object",
        service_error_code=code,
        service_message="private-upstream-details",
        http_status_code=status,
        expected_params={"Bucket": "test-media", "Key": key},
    )
    response = client.get("/media/" + key)
    assert response.status_code == expected
    assert "private-upstream-details" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_spaces_ignoring_range_does_not_send_incorrect_response(spaces):
    _, stub, client, key, raw = spaces
    head(stub, key, len(raw))
    stream = io.BytesIO(raw)
    stub.add_response(
        "get_object",
        {"Body": StreamingBody(stream, len(raw)), "ContentLength": len(raw)},
        {"Bucket": "test-media", "Key": key, "Range": "bytes=0-1"},
    )
    response = client.get("/media/" + key, headers={"Range": "bytes=0-1"})
    assert response.status_code == 503 and stream.closed


def test_spaces_upload_retains_metadata_bytes_and_does_not_request_public_acl(spaces):
    config, _, _, key, raw = spaces
    store = S3Store(config)
    try:
        with Stubber(store.client) as stub:
            stub.add_response(
                "put_object",
                {},
                {
                    "Bucket": "test-media",
                    "Key": key,
                    "Body": raw,
                    "ContentLength": len(raw),
                    "ContentMD5": base64.b64encode(hashlib.md5(raw, usedforsecurity=False).digest()).decode(
                        "ascii"
                    ),
                    "ContentType": "image/jpeg",
                    "Metadata": {"sha256": hashlib.sha256(raw).hexdigest()},
                },
            )
            store.put(key, raw, "image/jpeg")
            stub.assert_no_pending_responses()
        assert store.url(key) == "https://media.example.org/" + key
    finally:
        store.client.close()


def test_local_media_still_serves_original_bytes_and_ranges(settings, assets):
    key = assets[0].storage_key
    raw = storage(settings).read(key)
    with TestClient(create_app(settings)) as client:
        response = client.get("/media/" + key)
        assert response.content == raw
        assert "immutable" in response.headers["cache-control"]
        assert client.head("/media/" + key).headers["content-length"] == str(len(raw))
        partial = client.get("/media/" + key, headers={"Range": "bytes=0-3"})
        assert partial.status_code == 206 and partial.content == raw[:4]


def test_spaces_put_uses_fixed_length_original_bytes_and_checksum(spaces):
    config, _, _, key, raw = spaces
    store = S3Store(config)
    captured = []

    class StopBeforeNetwork(Exception):
        pass

    def capture(request, **kwargs):
        captured.append(request)
        assert request.headers["Content-Length"] == str(len(raw)).encode()
        assert request.headers["Content-MD5"] == base64.b64encode(
            hashlib.md5(raw, usedforsecurity=False).digest()
        )
        assert "aws-chunked" not in str(request.headers.get("Content-Encoding", ""))
        assert "Transfer-Encoding" not in request.headers and "X-Amz-Trailer" not in request.headers
        assert request.body.read() == raw
        raise StopBeforeNetwork()

    store.client.meta.events.register("before-send.s3.PutObject", capture)
    try:
        with pytest.raises(StopBeforeNetwork):
            store.put(key, raw, "image/jpeg")
        assert len(captured) == 1
    finally:
        store.client.close()
