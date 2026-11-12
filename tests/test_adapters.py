import json
from datetime import timedelta
from urllib.parse import parse_qsl

import httpx
import pytest
from sqlalchemy import select

from social.adapters import ADAPTERS
from social.adapters.base import Deferred, PublishError, error_for
from social.db import now
from social.facts import load_facts
from social.gate import run_gate
from social.models import Account, Control, RequestRecord
from social.pipeline import draft, generate, publish_due
from social.scheduler import schedule
from social.security import Vault
from social.seed import seed_media
from social.storage import ingest


@pytest.fixture(scope="module")
def video_bytes():
    return seed_media()[1]


def prepared(db, settings, assets, platform, video=None, ai=False):
    if video:
        a = ingest(db, settings, video, "clip.mp4", rights="Company owned")
        assets = [a]
    if ai:
        assets[0].source, assets[0].generator, assets[0].prompt = (
            "ai_generated",
            "fixture-model",
            "fixture-prompt",
        )
    facts = load_facts(db, settings.facts_path)
    post = draft(db, platform, "adapter-test", [a.id for a in assets], facts.id, product="demo")
    generate(db, settings, post)
    run_gate(db, settings, post)
    schedule(db, settings, post)
    from social.lifecycle import transition

    transition(db, post, "publishing", "Test worker claim")
    db.commit()
    return post


@pytest.mark.parametrize("platform", ["linkedin", "instagram", "facebook", "facebook-reel", "x", "tiktok"])
def test_dry_adapter_records_requests_without_network(
    db, settings, assets, video_bytes, reel_bytes, platform
):
    video = {"tiktok": video_bytes, "facebook-reel": reel_bytes}.get(platform)
    platform = platform.removesuffix("-reel")
    post = prepared(db, settings, assets, platform, video, ai=True)
    assert post.ai_disclosure
    assert "AI-generated or AI-edited media." not in post.caption

    def network_forbidden(request):
        pytest.fail("Dry-run attempted network access")

    a = ADAPTERS[platform](settings, db, post, httpx.Client(transport=httpx.MockTransport(network_forbidden)))
    result = a.publish(post, post.idempotency_key)
    assert result.status == ("awaiting_manual" if platform == "tiktok" else "published")
    assert not result.url
    records = db.scalars(select(RequestRecord).where(RequestRecord.post_id == post.id)).all()
    assert records and all(r.dry_run for r in records)
    assert all(r.request["headers"].get("Authorization", "<redacted>") == "<redacted>" for r in records)
    if platform == "instagram":
        assert next(r for r in records if r.step == "container-0").request["json"]["is_ai_generated"] is True
    if platform == "x":
        assert next(r for r in records if r.step == "publish").request["json"]["made_with_ai"] is True
    if platform == "facebook" and video:
        assert [r.step for r in records] == ["reel-start", "reel-upload", "reel-status", "publish"]
        assert records[-1].request["form"]["is_ai_generated"] == "true"
    if platform == "tiktok":
        init = next(r for r in records if r.step == "inbox-init")
        assert "/inbox/video/init/" in init.request["url"]
        assert set(init.request["json"]) == {"source_info"}


@pytest.fixture(scope="module")
def reel_bytes(video_bytes):
    from social.renditions import video_rendition

    return video_rendition(video_bytes, 720, 1280, 4)


def live_facebook(db, settings, post, handler):
    account = db.get(Account, "facebook")
    account.external_id, account.validated_at = "page", now()
    account.credentials = Vault(settings).encrypt(
        {"access_token": "SECRET-PAGE", "system_token": "SECRET-SYS"}
    )
    db.commit()
    adapter = ADAPTERS["facebook"](settings, db, post, httpx.Client(transport=httpx.MockTransport(handler)))
    adapter.dry = False
    return adapter


def reel_api(video, statuses, finishes, seen):
    def handler(request):
        seen.append(request)
        if request.url.path == "/v26.0/page/video_reels":
            assert request.headers["authorization"] == "Bearer SECRET-PAGE"
            if request.headers["content-type"] == "application/json":
                assert json.loads(request.content) == {"upload_phase": "start"}
                return httpx.Response(
                    200,
                    json={
                        "video_id": "reel-1",
                        "upload_url": "https://rupload.facebook.com/video-upload/v26.0/reel-1",
                    },
                )
            finishes.append(dict(parse_qsl(request.content.decode())))
            return httpx.Response(200, json={"success": True})
        if request.url.host == "rupload.facebook.com":
            assert request.url.path == "/video-upload/v26.0/reel-1"
            assert request.headers["authorization"] == "OAuth SECRET-PAGE"
            assert request.headers["offset"] == "0" and request.headers["file_size"] == str(len(video))
            assert request.content == video
            return httpx.Response(200, json={"success": True})
        if request.url.path == "/v26.0/reel-1":
            assert request.url.params["fields"] == "status"
            return httpx.Response(200, json={"status": statuses.pop(0)})
        pytest.fail(f"Unexpected API request {request.method} {request.url}")

    return handler


COMPLETE = {"video_status": "upload_complete", "uploading_phase": {"status": "complete"}}


def test_facebook_ai_video_publishes_as_reel_with_native_ai_label(db, settings, assets, reel_bytes):
    from social.gate import evaluate

    post = prepared(db, settings, assets, "facebook", reel_bytes, ai=True)
    results, _ = evaluate(db, settings, post, live=True)
    disclosure = next(c for c in results if c.name == "disclosure")
    assert disclosure.passed and disclosure.details["method"] == "is_ai_generated"
    finishes, seen = [], []
    adapter = live_facebook(db, settings, post, reel_api(reel_bytes, [COMPLETE], finishes, seen))
    result = adapter.publish(post, post.idempotency_key)
    assert (result.platform_post_id, result.url) == ("reel-1", "https://www.facebook.com/reel/reel-1")
    assert finishes == [
        {
            "video_id": "reel-1",
            "upload_phase": "finish",
            "video_state": "PUBLISHED",
            "description": post.caption,
            "is_ai_generated": "true",
        }
    ]
    assert not any("graph-video" in str(r.url) or "/uploads" in r.url.path for r in seen)
    # A restart after Facebook acknowledged the reel recovers it without any request.
    adapter.client = httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("Recovery used network"))
    )
    assert adapter.publish(post, post.idempotency_key).platform_post_id == "reel-1"
    for record in db.scalars(select(RequestRecord).where(RequestRecord.post_id == post.id)):
        assert "SECRET-PAGE" not in str(record.request) and "SECRET-SYS" not in str(record.request)
        assert "encrypted" in record.response


def test_facebook_company_reel_is_not_labeled_ai(db, settings, assets, reel_bytes):
    post = prepared(db, settings, assets, "facebook", reel_bytes)
    finishes = []
    live_facebook(db, settings, post, reel_api(reel_bytes, [COMPLETE], finishes, [])).publish(
        post, post.idempotency_key
    )
    assert finishes[0]["is_ai_generated"] == "false"


def test_facebook_reel_waits_for_upload_and_rejects_processing_errors(db, settings, assets, reel_bytes):
    post = prepared(db, settings, assets, "facebook", reel_bytes, ai=True)
    uploading = {"video_status": "uploading", "uploading_phase": {"status": "in_progress"}}
    statuses, finishes, seen = [uploading, COMPLETE], [], []
    adapter = live_facebook(db, settings, post, reel_api(reel_bytes, statuses, finishes, seen))
    with pytest.raises(Deferred):
        adapter.publish(post, post.idempotency_key)
    assert not finishes
    adapter.publish(post, post.idempotency_key)
    assert len(finishes) == 1
    assert len([r for r in seen if r.url.host == "rupload.facebook.com"]) == 1

    other = prepared(db, settings, assets, "facebook", reel_bytes, ai=True)
    failed = {
        "video_status": "processing",
        "uploading_phase": {"status": "complete"},
        "processing_phase": {"status": "error", "error": {"message": "Resolution too low."}},
    }
    finishes = []
    adapter = live_facebook(db, settings, other, reel_api(reel_bytes, [failed], finishes, []))
    with pytest.raises(PublishError, match="540x960") as error:
        adapter.publish(other, other.idempotency_key)
    assert error.value.kind == "validation" and not finishes


def test_facebook_video_below_reels_minimum_is_held(db, settings, assets, video_bytes):
    from social.config import profiles
    from social.gate import media_errors

    small = ingest(db, settings, video_bytes, "square.mp4", rights="Company owned")
    assert any("540x960" in e for e in media_errors([small], profiles(settings)["facebook"], "facebook"))
    assert media_errors([small], profiles(settings)["instagram"], "instagram") == []


def test_tiktok_manual_alert_keeps_label_instruction_without_caption_disclosure(
    db, settings, assets, video_bytes
):
    from social.models import Alert

    post = prepared(db, settings, assets, "tiktok", video_bytes, ai=True)
    publish_due(db, settings, at=post.scheduled_at)
    assert post.status == "awaiting_manual"
    alert = db.scalar(select(Alert).where(Alert.dedupe_key == "manual:" + post.id))
    assert "Turn AI-generated label ON" in alert.message
    assert "Caption to paste:\n" + post.caption in alert.message
    assert "AI-generated or AI-edited media." not in alert.message


def test_instagram_uses_system_token_for_quota_and_page_token_for_publishing(db, settings, assets, monkeypatch):
    monkeypatch.setattr("social.adapters.platforms.public_url", lambda u: u)
    settings = settings.model_copy(update={"media_base_url": "https://media.example.org"})
    post = prepared(db, settings, assets, "instagram")
    account = db.get(Account, "instagram")
    account.external_id, account.validated_at = "ig-account", now()
    account.credentials = Vault(settings).encrypt({"access_token": "SECRET-PAGE", "system_token": "SECRET-SYS"})
    db.commit()
    seen = []

    def handler(request):
        path = request.url.path
        seen.append(path)
        if path.endswith("/content_publishing_limit"):
            if request.headers["authorization"] != "Bearer SECRET-SYS":
                return httpx.Response(400, json={"error": {"code": 200, "message": "API access blocked."}})
            return httpx.Response(200, json={"data": [{"quota_usage": 0, "config": {"quota_total": 100}}]})
        assert request.headers["authorization"] == "Bearer SECRET-PAGE"
        responses = {
            "/v26.0/ig-account/media": {"id": "container"},
            "/v26.0/container": {"status_code": "FINISHED"},
            "/v26.0/ig-account/media_publish": {"id": "ig-post"},
            "/v26.0/ig-post": {"permalink": "https://www.instagram.com/p/example/"},
        }
        assert path in responses
        return httpx.Response(200, json=responses[path])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    adapter = ADAPTERS["instagram"](settings, db, post, client)
    adapter.dry = False
    assert adapter.limit()["data"][0]["quota_usage"] == 0
    result = adapter.publish(post, post.idempotency_key)
    assert result.status == "published" and result.platform_post_id == "ig-post"
    assert seen.count("/v26.0/ig-account/content_publishing_limit") == 2
    records = db.scalars(select(RequestRecord).where(RequestRecord.post_id == post.id)).all()
    assert all(record.state == "done" for record in records)
    assert all(record.request["headers"]["Authorization"] == "<redacted>" for record in records)
    assert "SECRET-" not in json.dumps([record.request for record in records])


@pytest.mark.parametrize("platform", ["linkedin", "instagram", "facebook", "x", "tiktok"])
def test_mocked_live_publish_flows(db, settings, assets, video_bytes, monkeypatch, platform):
    monkeypatch.setattr("social.adapters.platforms.public_url", lambda u: u)
    settings = settings.model_copy(update={"media_base_url": "https://media.example.org"})
    post = prepared(db, settings, assets, platform, video_bytes if platform == "tiktok" else None)
    account = db.get(Account, platform)
    account.external_id, account.validated_at = "account", now()
    account.credentials = Vault(settings).encrypt({"access_token": "SECRET-ACCESS"})
    db.commit()
    seen = []

    def handler(request):
        seen.append(request)
        path = request.url.path
        if path.endswith("content_publishing_limit"):
            return httpx.Response(200, json={"data": [{"quota_usage": 0, "config": {"quota_total": 100}}]})
        if path.endswith("/media"):
            assert request.read()
            return httpx.Response(200, json={"id": "container"})
        if path.endswith("/container"):
            return httpx.Response(200, json={"status_code": "FINISHED"})
        if path.endswith("media_publish"):
            return httpx.Response(200, json={"id": "ig-post"})
        if path.endswith("/ig-post"):
            return httpx.Response(200, json={"permalink": "https://www.instagram.com/p/example/"})
        if path.endswith("/photos"):
            return httpx.Response(200, json={"id": "photo", "post_id": "page-post"})
        if path.endswith("/images"):
            return httpx.Response(
                200,
                json={
                    "value": {
                        "image": "urn:li:image:123",
                        "uploadUrl": "https://www.linkedin.com/dms-uploads/test?sig=secret",
                    }
                },
            )
        if "/dms-uploads/" in path:
            assert request.headers["authorization"] == "Bearer SECRET-ACCESS"
            return httpx.Response(201)
        if "/images/" in path:
            return httpx.Response(200, json={"status": "AVAILABLE"})
        if path.endswith("/posts"):
            return httpx.Response(201, headers={"x-restli-id": "urn:li:share:123"})
        if path.endswith("/initialize"):
            return httpx.Response(200, json={"data": {"id": "media-123"}})
        if path.endswith("/append"):
            assert b"segment_index" in request.content
            return httpx.Response(204)
        if path.endswith("/finalize"):
            return httpx.Response(
                200, json={"data": {"id": "media-123", "processing_info": {"state": "pending"}}}
            )
        if path.endswith("/media/upload"):
            return httpx.Response(200, json={"data": {"processing_info": {"state": "succeeded"}}})
        if path.endswith("/tweets"):
            return httpx.Response(201, json={"data": {"id": "post-123"}})
        if path.endswith("/inbox/video/init/"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "publish_id": "inbox-123",
                        "upload_url": "https://open-upload.tiktokapis.com/video/test?token=signed",
                    },
                    "error": {"code": "ok"},
                },
            )
        if path == "/video/test":
            assert request.headers["content-range"].startswith("bytes 0-")
            assert "authorization" not in request.headers
            return httpx.Response(201)
        if path.endswith("/status/fetch/"):
            return httpx.Response(
                200, json={"data": {"status": "SEND_TO_USER_INBOX"}, "error": {"code": "ok"}}
            )
        pytest.fail(f"Unexpected API request {request.method} {request.url}")

    a = ADAPTERS[platform](settings, db, post, httpx.Client(transport=httpx.MockTransport(handler)))
    a.dry = False
    result = a.publish(post, post.idempotency_key)
    assert result.platform_post_id
    count = len(
        [
            r
            for r in seen
            if r.method == "POST"
            and (
                r.url.path.endswith(("/posts", "/tweets", "/photos", "/media_publish", "/inbox/video/init/"))
            )
        ]
    )
    assert count == 1
    # Simulate a restart after the provider acknowledged publication but before
    # the worker stored the final Post status. Even volatile polls must not replay.
    a.client = httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("Recovery used network")))
    a.publish(post, post.idempotency_key)
    assert (
        len(
            [
                r
                for r in seen
                if r.method == "POST"
                and r.url.path.endswith(
                    ("/posts", "/tweets", "/photos", "/media_publish", "/inbox/video/init/")
                )
            ]
        )
        == 1
    )
    for record in db.scalars(select(RequestRecord)):
        assert "SECRET-ACCESS" not in str(record.request)
        assert "signed" not in str(record.request)
        assert "encrypted" in record.response


@pytest.mark.parametrize("platform", ["linkedin", "instagram", "facebook", "x", "tiktok"])
@pytest.mark.parametrize(
    "status,body,kind",
    [
        (429, {}, "transient"),
        (401, {}, "auth"),
        (403, {"error": {"message": "Policy rejection"}}, "policy"),
        (400, {"error": {"code": "invalid_param"}}, "validation"),
        (503, {}, "transient"),
    ],
)
def test_platform_error_classification(platform, status, body, kind):
    response = httpx.Response(status, json=body, headers={"retry-after": "120"})
    error = error_for(response, body)
    assert error.kind == kind
    if status == 429:
        assert error.retry_after == 120


def test_uncertain_publish_never_retried(db, settings, assets):
    post = prepared(db, settings, assets, "facebook")
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("lost reply")

    a = ADAPTERS["facebook"](settings, db, post, httpx.Client(transport=httpx.MockTransport(handler)))
    a.dry = False
    for _ in range(2):
        with pytest.raises(PublishError) as error:
            a.request(
                "publish",
                "POST",
                "https://graph.facebook.com/v26.0/page/photos",
                json={"url": "https://media.example.org/test.jpg"},
                irreversible=True,
            )
        assert error.value.kind == "ambiguous"
    assert len(calls) == 1


def test_rate_limit_can_retry_but_policy_cannot(db, settings, assets):
    post = prepared(db, settings, assets, "facebook")
    replies = iter([httpx.Response(429, json={}), httpx.Response(200, json={"id": "one"})])
    a = ADAPTERS["facebook"](
        settings, db, post, httpx.Client(transport=httpx.MockTransport(lambda _: next(replies)))
    )
    a.dry = False
    with pytest.raises(PublishError):
        a.request(
            "publish", "POST", "https://graph.facebook.com/v26.0/page/photos", json={}, irreversible=True
        )
    result, _ = a.request(
        "publish", "POST", "https://graph.facebook.com/v26.0/page/photos", json={}, irreversible=True
    )
    assert result == {"id": "one"}
    assert (
        a.request(
            "publish", "POST", "https://graph.facebook.com/v26.0/page/photos", json={}, irreversible=True
        )[0]
        == result
    )


def test_kill_switch_checked_before_each_step(db, settings, assets):
    post = prepared(db, settings, assets, "linkedin")
    a = ADAPTERS["linkedin"](settings, db, post)
    a.request("first", "GET", "https://api.linkedin.com/rest/images", fake={})
    db.get(Control, 1).killed = True
    db.commit()
    with pytest.raises(PublishError) as error:
        a.request("publish", "POST", "https://api.linkedin.com/rest/posts", json={}, irreversible=True)
    assert error.value.kind == "paused"
    assert not db.scalar(select(RequestRecord).where(RequestRecord.step == "publish"))


def test_policy_failure_auto_pauses_worker(db, settings, post, monkeypatch):
    run_gate(db, settings, post)
    at = schedule(db, settings, post)

    class Rejected:
        dry = True

        def publish(self, *args):
            raise PublishError("policy", "Policy rejection")

    monkeypatch.setattr("social.adapters.adapter", lambda *args: Rejected())
    publish_due(db, settings, at=at)
    assert post.status == "failed"
    assert db.get(Account, "linkedin").paused


def test_worker_retry_backoff_and_idempotent_success(db, settings, post, monkeypatch):
    run_gate(db, settings, post)
    # Keep every retry inside the publishing window, even when tests run near
    # closing time. The worker correctly defers retries outside that window.
    earliest = (now() + timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0)
    at = schedule(db, settings, post, earliest=earliest)
    from social.adapters.base import PublishResult

    calls = []

    class Flaky:
        dry = True

        def publish(self, p, key):
            calls.append(key)
            if len(calls) == 1:
                raise PublishError("transient", "Rate limit", 120)
            return PublishResult("only-post", "", "published")

    monkeypatch.setattr("social.adapters.adapter", lambda *args: Flaky())
    publish_due(db, settings, at=at)
    assert post.status == "publishing"
    assert post.next_attempt_at == at + timedelta(seconds=120)
    publish_due(db, settings, at=at + timedelta(seconds=60))
    assert len(calls) == 1
    publish_due(db, settings, at=at + timedelta(seconds=120))
    publish_due(db, settings, at=at + timedelta(seconds=180))
    assert post.status == "published" and len(calls) == 2 and len(set(calls)) == 1


def test_instagram_carousel_disclosure_on_parent_only(db, settings, assets):
    from social.models import Asset

    a = assets[0]
    clone = Asset(
        source="ai_edited",
        generator="fixture",
        prompt="edit",
        rights="Company owned",
        media=a.media,
        storage_key=a.storage_key,
        checksum=a.checksum,
        original_name="second.jpg",
        tags={},
    )
    db.add(clone)
    db.commit()
    post = prepared(db, settings, assets + [clone], "instagram")
    ADAPTERS["instagram"](settings, db, post).publish(post, post.idempotency_key)
    rows = db.scalars(select(RequestRecord)).all()
    assert all(
        "is_ai_generated" not in r.request["json"]
        for r in rows
        if r.step.startswith("container-") and not r.step.startswith("container-status")
    )
    assert next(r for r in rows if r.step == "carousel").request["json"]["is_ai_generated"]


@pytest.mark.parametrize("platform", ["facebook", "linkedin", "x"])
def test_video_upload_paths(db, settings, assets, video_bytes, reel_bytes, platform):
    post = prepared(db, settings, assets, platform, reel_bytes if platform == "facebook" else video_bytes)
    result = ADAPTERS[platform](settings, db, post).publish(post, post.idempotency_key)
    assert result.status == "published"
    rows = db.scalars(select(RequestRecord)).all()
    assert any(r.request.get("binary") or r.request.get("files") for r in rows)
