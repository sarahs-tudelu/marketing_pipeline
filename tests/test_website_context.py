import hashlib
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from social import media_studio
from social.adapters import ADAPTERS
from social.facts import save_facts
from social.media_studio import CreativeBrief, WebsiteContextError, replenish_campaigns, website_snapshot
from social.models import MediaCampaign, MediaJob

HTML = b'<meta name="description" content="Architectural systems"><p>Pergolas</p><p>Coming soon</p><script>ignore me</script>'


@pytest.fixture
def website(monkeypatch):
    client_type = httpx.Client
    requests, pauses, responses = [], [], []
    monkeypatch.setattr(media_studio, "time", SimpleNamespace(time=time.time, sleep=pauses.append))

    def install(handler):
        def respond(request):
            requests.append(request)
            response = handler(request, len(requests))
            responses.append(response)
            return response
        monkeypatch.setattr(media_studio.httpx, "Client", lambda **options: client_type(
            transport=httpx.MockTransport(respond), **options,
        ))
        return requests, pauses, responses
    return install


@pytest.mark.parametrize("failure", ["timeout", "connection", "http"])
def test_temporary_website_failure_gets_one_retry_then_preserves_real_context(settings, website, failure):
    settings.ai_mode = "live"

    def respond(request, attempt):
        if attempt == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("upstream diagnostic", request=request)
            if failure == "connection":
                raise httpx.ConnectError("upstream diagnostic", request=request)
            return httpx.Response(503, text="upstream diagnostic")
        return httpx.Response(200, content=HTML)

    requests, pauses, responses = website(respond)
    snapshot = website_snapshot(settings)
    assert len(requests) == 2 and pauses == [1]
    assert all(response.is_closed for response in responses)
    assert snapshot["text"] == "Architectural systems | Pergolas | Coming soon"
    assert snapshot["sha256"] == hashlib.sha256(HTML).hexdigest()
    assert snapshot["fetched_at"] and not snapshot.get("demo_only")


def test_failed_connection_reports_sanitized_reason_after_two_attempts(settings, website, caplog):
    settings.ai_mode = "live"

    def fail(request, _):
        raise httpx.ReadTimeout("private transport details", request=request)

    requests, pauses, _ = website(fail)
    with caplog.at_level("INFO", logger="social"), pytest.raises(WebsiteContextError, match="timed out") as caught:
        website_snapshot(settings)
    assert caught.value.retryable and len(requests) == 2 and pauses == [1]
    assert "website_context_failed" in caplog.text and "private transport details" not in caplog.text


@pytest.mark.parametrize("status", [429, 503])
def test_retry_after_defers_instead_of_immediately_retrying(settings, website, status):
    settings.ai_mode = "live"
    requests, pauses, _ = website(lambda *args: httpx.Response(status, headers={"retry-after": "900"}))
    with pytest.raises(WebsiteContextError, match=f"HTTP {status}") as caught:
        website_snapshot(settings)
    assert caught.value.retryable and caught.value.retry_after == 900
    assert len(requests) == 1 and not pauses


def test_retry_after_accepts_http_dates_and_ignores_invalid_values():
    date = format_datetime(datetime.now(timezone.utc) + timedelta(minutes=15), usegmt=True)
    assert 898 < media_studio.website_retry_after(httpx.Response(503, headers={"retry-after": date})) <= 900
    for invalid in ("", "not a date", "-20", "9" * 50):
        assert media_studio.website_retry_after(httpx.Response(503, headers={"retry-after": invalid})) == 0


@pytest.mark.parametrize("status", [403, 404, 302])
def test_access_errors_and_redirects_do_not_retry_or_follow_another_origin(settings, website, status):
    settings.ai_mode = "live"
    requests, pauses, _ = website(lambda *args: httpx.Response(
        status, headers={"location": "https://elsewhere.example/private"}, text="private diagnostic",
    ))
    with pytest.raises(WebsiteContextError, match=f"HTTP {status}") as caught:
        website_snapshot(settings)
    assert not caught.value.retryable and len(requests) == 1 and not pauses
    assert "private" not in str(caught.value)
    assert str(requests[0].url) == "https://tudelu.com/"


@pytest.mark.parametrize("content,reason", [(b"x" * 2_000_001, "2 MB"), (b"<script>empty</script>", "no readable")])
def test_invalid_website_content_still_stops_without_silent_fallback(settings, website, content, reason):
    settings.ai_mode = "live"
    requests, pauses, _ = website(lambda *args: httpx.Response(200, content=content))
    with pytest.raises(WebsiteContextError, match=reason) as caught:
        website_snapshot(settings)
    assert not caught.value.retryable and len(requests) == 1 and not pauses


@pytest.fixture
def live_campaign(db, settings, monkeypatch):
    sheet = save_facts(db, {
        "version": "website-test", "brand_voice": "Simple", "hashtags": {},
        "facts": [{"id": "name", "product": "company", "text": "Test brand."}],
    })
    brief = CreativeBrief(campaign="Website campaign", prompt="Architectural textures", platforms=["instagram"],
                          facts_id=sheet.id, rights="public", website=True)
    campaign = MediaCampaign(name=brief.campaign, brief=brief.model_dump(), enabled=True)
    db.add(campaign)
    db.commit()
    settings.ai_mode, settings.live_platforms, settings.runway_api_key = "live", "instagram", SecretStr("test-only")
    monkeypatch.setitem(ADAPTERS, "instagram", lambda *args: SimpleNamespace(dry=False))
    return campaign


@pytest.mark.parametrize("status,retry_after,delay", [(503, "", 300), (429, "900", 900)])
def test_campaign_stays_enabled_and_resumes_at_website_retry_time(
    db, settings, live_campaign, monkeypatch, website, status, retry_after, delay,
):
    clock = media_studio.now()
    monkeypatch.setattr(media_studio, "now", lambda: clock)
    healthy = False
    requests, _, _ = website(lambda *args: httpx.Response(200, content=HTML) if healthy else httpx.Response(
        status, headers={"retry-after": retry_after},
    ))
    replenish_campaigns(db, settings)
    assert live_campaign.enabled and live_campaign.sequence == 0
    assert live_campaign.next_run_at == clock + timedelta(seconds=delay)
    assert "Campaign remains enabled" in live_campaign.error
    assert db.scalar(select(MediaJob)) is None  # No paid generation or partial batch.
    attempted = len(requests)
    replenish_campaigns(db, settings)
    assert len(requests) == attempted  # Wait for the stored retry, not every worker tick.
    healthy = True
    clock += timedelta(seconds=delay)
    replenish_campaigns(db, settings)
    assert live_campaign.enabled and live_campaign.sequence == 1 and not live_campaign.error
    job = db.scalar(select(MediaJob))
    assert job.state == "queued" and job.brief["website_snapshot"]["sha256"] == hashlib.sha256(HTML).hexdigest()
    replenish_campaigns(db, settings)
    assert len(db.scalars(select(MediaJob)).all()) == 1


def test_campaign_still_pauses_when_website_access_requires_attention(db, settings, live_campaign, website):
    website(lambda *args: httpx.Response(403))
    replenish_campaigns(db, settings)
    assert not live_campaign.enabled and "HTTP 403" in live_campaign.error
    assert live_campaign.sequence == 0 and db.scalar(select(MediaJob)) is None


def test_fixture_website_never_makes_a_request(settings, website):
    requests, _, _ = website(lambda *args: pytest.fail("Fixture mode cannot fetch the website"))
    assert website_snapshot(settings)["demo_only"] and not requests
