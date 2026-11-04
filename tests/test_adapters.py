from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from social.adapters import ADAPTERS
from social.adapters.base import PublishError, error_for
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


@pytest.mark.parametrize("platform", ["linkedin", "instagram", "facebook", "x", "tiktok"])
def test_dry_adapter_records_requests_without_network(db, settings, assets, video_bytes, platform):
    post = prepared(db, settings, assets, platform, video_bytes if platform == "tiktok" else None, ai=True)
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
    if platform == "tiktok":
        init = next(r for r in records if r.step == "inbox-init")
        assert "/inbox/video/init/" in init.request["url"]
        assert set(init.request["json"]) == {"source_info"}


def test_facebook_ai_video_requires_native_label_before_live_requests(db, settings, assets, video_bytes):
    from social.gate import evaluate

    post = prepared(db, settings, assets, "facebook", video_bytes, ai=True)
    results, _ = evaluate(db, settings, post, live=True)
    disclosure = next(c for c in results if c.name == "disclosure")
    assert not disclosure.passed
    assert "native AI label" in disclosure.details["reasons"][0]
    adapter = ADAPTERS["facebook"](
        settings,
        db,
        post,
        httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("Must not contact Facebook"))),
    )
    adapter.dry = False
    with pytest.raises(PublishError, match="native AI label"):
        adapter.publish(post, post.idempotency_key)
    assert not db.scalars(select(RequestRecord).where(RequestRecord.post_id == post.id)).all()


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
    at = schedule(db, settings, post)
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
def test_video_upload_paths(db, settings, assets, video_bytes, platform):
    post = prepared(db, settings, assets, platform, video_bytes)
    result = ADAPTERS[platform](settings, db, post).publish(post, post.idempotency_key)
    assert result.status == "published"
    rows = db.scalars(select(RequestRecord)).all()
    assert any(r.request.get("binary") or r.request.get("files") for r in rows)
