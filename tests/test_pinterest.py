"""Pinterest: real Google Drive photos and videos only, AI captions, official API v5."""

import io
import re
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import SecretStr
from sqlalchemy import select

from social.adapters import ADAPTERS
from social.adapters.base import Deferred
from social.app import create_app
from social.connections import begin, callback
from social.db import now
from social.facts import content_digest, load_facts
from social.gate import run_gate
from social.human_review import approve
from social.lifecycle import transition
from social.media_studio import CreativeBrief
from social.models import Account, Asset, CheckResult, Post, RequestRecord
from social.pipeline import draft, generate
from social.recovery import held_reason
from social.renditions import fitted_size
from social.scheduler import schedule
from social.security import Vault
from social.seed import seed_media
from social.storage import ingest


def jpeg(width, height):
    stream = io.BytesIO()
    Image.new("RGB", (width, height), "#7d9d89").save(stream, format="JPEG")
    return stream.getvalue()


def drive_asset(db, settings, data, name="pergola.jpg", **extra):
    tags = {"product": "demo", "google_drive_file_id": f"drive-{name}", "google_drive_folder_id": "folder"}
    asset = ingest(db, settings, data, name, rights="public", tags=tags, **extra)
    db.commit()
    return asset


def configured(settings):
    return settings.model_copy(
        update={
            "app_base_url": "https://social.example.org",
            "media_base_url": "https://media.example.org",
            "pinterest_client_id": "pin-client",
            "pinterest_client_secret": SecretStr("pin-secret"),
            "pinterest_board_id": "549755885175",
        }
    )


def claimed(db, settings, asset):
    from social.media_studio import platform_copy

    facts = load_facts(db, settings.facts_path)
    # As Post from library media does: a video gets the logo ending and sound in a linked copy.
    asset = platform_copy(db, settings, asset, "pinterest")
    post = draft(db, "pinterest", "Pinterest test", [asset.id], facts.id, product="demo")
    generate(db, settings, post)
    run_gate(db, settings, post)
    assert post.status == "awaiting_review"
    approve(db, settings, post, "test-reviewer", content_digest(post, [db.get(Asset, i) for i in post.asset_ids]))
    assert post.status == "approved", [
        (c.name, c.details)
        for c in db.scalars(select(CheckResult).where(CheckResult.post_id == post.id))
        if not c.passed
    ]
    schedule(db, settings, post)
    transition(db, post, "publishing", "Test worker claim")
    db.commit()
    return post


def test_pinterest_takes_only_real_drive_media(db, settings, assets):
    facts = load_facts(db, settings.facts_path)
    with pytest.raises(ValueError, match="imported from Google Drive"):
        draft(db, "pinterest", "Not from Drive", [assets[0].id], facts.id, product="demo")
    made = ingest(
        db,
        settings,
        jpeg(800, 1200),
        "concept.jpg",
        source="ai_generated",
        generator="runway/gen4_image",
        prompt="A pergola",
        rights="public",
        tags={"google_drive_file_id": "drive-concept"},
    )
    with pytest.raises(ValueError, match="made or edited with AI"):
        draft(db, "pinterest", "AI", [made.id], facts.id, product="demo")
    with pytest.raises(ValueError, match="Pinterest only posts real photos"):
        CreativeBrief(
            campaign="c", prompt="p", platforms=["instagram", "pinterest"], facts_id=facts.id, rights="public"
        )

    real = drive_asset(db, settings, jpeg(800, 1200))
    post = draft(db, "pinterest", "Drive photo", [real.id], facts.id, product="demo")
    generate(db, settings, post)
    run_gate(db, settings, post)
    assert post.status == "awaiting_review" and post.caption and not post.ai_disclosure
    check = db.scalar(
        select(CheckResult).where(CheckResult.post_id == post.id, CheckResult.name == "drive_media")
    )
    assert check.passed
    # The caption writer saw what the photo shows, since real media has no generation prompt.
    assert real.media.get("visual_summary")

    # A later correction marking the photo as AI-edited holds the post at its next check.
    transition(db, post, "held", "Media details changed")
    real.source, real.generator, real.prompt = "ai_edited", "editor", "Retouched sky"
    transition(db, post, "generated", "Requested automated recheck")
    db.commit()
    run_gate(db, settings, post)
    assert post.status == "held"
    problem = next(p for p in held_reason(db, post)["problems"] if p["name"] == "drive_media")
    assert problem["title"] == "This platform only posts real photos and videos from Google Drive."
    assert problem["details"] == [
        "Pinterest only posts real photos and videos, and this media was made or edited with AI."
    ]


def test_fitted_size_keeps_the_media_shape():
    # Landscape media uses the box turned sideways; shapes outside the limits are padded to the box.
    assert fitted_size(2400, 1800, 1000, 1500, 0.5, 1.91) == (1332, 1000)
    assert fitted_size(3000, 4000, 1000, 1500, 0.5, 1.91) == (1000, 1332)
    assert fitted_size(640, 480, 1000, 1500, 0.5, 1.91) == (640, 480)
    assert fitted_size(4000, 1000, 1000, 1500, 0.5, 1.91) == (1000, 1500)


def test_library_post_resizes_a_large_drive_photo(db, settings, assets):
    big = drive_asset(db, settings, jpeg(2400, 1800), "terrace.jpg")
    facts = load_facts(db, settings.facts_path)
    db.commit()
    with TestClient(create_app(settings)) as client:
        token = client.get("/api/session").json()["csrf"]
        page = client.get("/admin/compose").text
        assert (
            '<option value="pinterest" >Pinterest</option>' in page
            and re.search(r"terrace\.jpg</span>\s*<small>Photo · JPEG · 2400 × 1800 · Google Drive", page)
        )
        refused = client.post(
            "/forms/posts",
            data={
                "csrf": token,
                "campaign": "Not Drive",
                "platform": "pinterest",
                "kind": "promotional",
                "product": "demo",
                "facts_id": facts.id,
                "asset_ids": [assets[0].id],
            },
        )
        assert refused.status_code == 400 and "imported from Google Drive" in refused.text
        assert 'value="Not Drive"' in refused.text
        created = client.post(
            "/forms/posts",
            data={
                "csrf": token,
                "campaign": "Terrace",
                "platform": "pinterest",
                "kind": "promotional",
                "product": "demo",
                "facts_id": facts.id,
                "asset_ids": [big.id],
            },
        )
        assert created.status_code == 200
    post = db.scalar(select(Post).where(Post.campaign == "Terrace"))
    copy = db.get(Asset, post.asset_ids[0])
    assert copy.id != big.id and copy.source == "company"
    assert (copy.media["width"], copy.media["height"]) == (1332, 1000)
    assert copy.tags["rendition"] == {"platform": "pinterest", "original_asset_id": big.id}
    assert copy.tags["source_asset_ids"] == [big.id] and copy.tags["google_drive_file_id"]


@pytest.mark.parametrize("kind", ["image", "video"])
def test_dry_pinterest_publish_records_requests(db, settings, house_logo, kind):
    data = jpeg(1000, 1500) if kind == "image" else seed_media(duration=6)[1]
    asset = drive_asset(db, settings, data, "pin.jpg" if kind == "image" else "pin.mp4")
    post = claimed(db, settings, asset)

    def network_forbidden(request):
        pytest.fail("Dry-run attempted network access")

    a = ADAPTERS["pinterest"](
        settings, db, post, httpx.Client(transport=httpx.MockTransport(network_forbidden))
    )
    result = a.publish(post, post.idempotency_key)
    assert result.status == "published" and not result.url
    records = {r.step: r for r in db.scalars(select(RequestRecord).where(RequestRecord.post_id == post.id))}
    expected = (
        ["publish"] if kind == "image" else ["media-register", "media-upload", "media-status", "publish"]
    )
    assert sorted(records) == sorted(expected)
    pin = records["publish"].request["json"]
    assert pin["description"] == post.caption and "ai_disclosures" not in pin
    source = pin["media_source"]
    if kind == "image":
        assert source["source_type"] == "image_url" and source["url"].endswith(asset.storage_key)
    else:
        assert source == {"source_type": "video_id", "media_id": "1", "cover_image_key_frame_time": 1}


@pytest.mark.parametrize("kind", ["image", "video"])
def test_live_pinterest_publish(db, settings, house_logo, monkeypatch, kind):
    monkeypatch.setattr("social.adapters.platforms.public_url", lambda u: u)
    settings = configured(settings)
    data = jpeg(1000, 1500) if kind == "image" else seed_media(duration=6)[1]
    asset = drive_asset(db, settings, data, "pin.jpg" if kind == "image" else "pin.mp4")
    post = claimed(db, settings, asset)
    account = db.get(Account, "pinterest")
    account.external_id, account.validated_at = "1234", now()
    account.credentials = Vault(settings).encrypt({"access_token": "pina-SECRET"})
    db.commit()
    seen, statuses = [], ["processing", "succeeded"]

    def handler(request):
        seen.append(request)
        path = request.url.path
        if request.url.host == "pinterest-media-upload.s3-accelerate.amazonaws.com":
            assert "authorization" not in request.headers
            body = request.read()
            assert b'name="policy"' in body and b"SIGNED-POLICY" in body and b'name="file"' in body
            return httpx.Response(204)
        assert request.headers["authorization"] == "Bearer pina-SECRET"
        if path == "/v5/media" and request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "media_id": "12345",
                    "media_type": "video",
                    "upload_url": "https://pinterest-media-upload.s3-accelerate.amazonaws.com/",
                    "upload_parameters": {
                        "key": "uploads/12345",
                        "policy": "SIGNED-POLICY",
                        "x-amz-signature": "SIG",
                    },
                },
            )
        if path == "/v5/media/12345":
            return httpx.Response(
                200, json={"media_id": "12345", "media_type": "video", "status": statuses.pop(0)}
            )
        if path == "/v5/pins":
            return httpx.Response(201, json={"id": "813744226420795884"})
        pytest.fail(f"Unexpected API request {request.method} {request.url}")

    a = ADAPTERS["pinterest"](settings, db, post, httpx.Client(transport=httpx.MockTransport(handler)))
    assert a.connected()
    a.dry = False
    if kind == "video":
        # Pinterest is still processing the upload: the worker checks again later without re-uploading.
        with pytest.raises(Deferred):
            a.publish(post, post.idempotency_key)
    result = a.publish(post, post.idempotency_key)
    assert result.platform_post_id == "813744226420795884"
    assert result.url == "https://www.pinterest.com/pin/813744226420795884/"
    posted = [r for r in seen if r.url.path == "/v5/pins"]
    uploads = [r for r in seen if r.url.host.endswith("amazonaws.com")]
    assert len(posted) == 1 and len(uploads) == (1 if kind == "video" else 0)
    pin = __import__("json").loads(posted[0].content)
    assert pin["board_id"] == "549755885175" and pin["description"] == post.caption
    a.client = httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("Recovery used network")))
    assert a.publish(post, post.idempotency_key).platform_post_id == "813744226420795884"
    for record in db.scalars(select(RequestRecord)):
        assert "pina-SECRET" not in str(record.request) and "SIGNED-POLICY" not in str(record.request)


def test_pinterest_oauth_checks_the_board(db, settings):
    settings = configured(settings)
    url = begin(db, settings, "pinterest", "browser")
    assert url.startswith("https://www.pinterest.com/oauth/?")
    params = parse_qs(urlparse(url).query)
    assert params["scope"] == ["boards:read,pins:read,pins:write,user_accounts:read"]
    assert params["client_id"] == ["pin-client"] and params["response_type"] == ["code"]

    def handler(request):
        if request.url.path == "/v5/oauth/token":
            assert request.headers["authorization"].startswith("Basic ")
            assert "pin-secret" not in request.content.decode()
            return httpx.Response(
                200,
                json={
                    "access_token": "pina-new",
                    "refresh_token": "pinr-new",
                    "expires_in": 2592000,
                    "refresh_token_expires_in": 31536000,
                    "token_type": "bearer",
                    "scope": "boards:read pins:read pins:write user_accounts:read",
                },
            )
        if request.url.path == "/v5/user_account":
            return httpx.Response(200, json={"id": "1234", "username": "tudelu", "account_type": "BUSINESS"})
        if request.url.path == "/v5/boards/549755885175":
            return httpx.Response(
                200, json={"id": "549755885175", "name": "Shade ideas", "owner": {"username": "tudelu"}}
            )
        pytest.fail(f"Unexpected request {request.url}")

    callback(
        db,
        settings,
        "pinterest",
        params["state"][0],
        "code",
        "browser",
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    account = db.get(Account, "pinterest")
    assert account.external_id == "1234" and account.refresh_expires_at
    assert Vault(settings).decrypt(account.credentials)["refresh_token"] == "pinr-new"

    other = begin(db, settings, "pinterest", "browser")

    def someone_elses_board(request):
        if request.url.path == "/v5/boards/549755885175":
            return httpx.Response(200, json={"id": "549755885175", "owner": {"username": "someone"}})
        return handler(request)

    with pytest.raises(ValueError, match="doesn't belong to the connected account"):
        callback(
            db,
            settings,
            "pinterest",
            parse_qs(urlparse(other).query)["state"][0],
            "code",
            "browser",
            httpx.Client(transport=httpx.MockTransport(someone_elses_board)),
        )
