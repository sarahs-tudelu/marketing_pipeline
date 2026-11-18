"""Settings → Usage: what AI requests cost, billed totals from the providers, and post performance."""

import json
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from social import usage
from social.app import create_app, money
from social.db import now
from social.facts import load_facts
from social.media_studio import process_jobs
from social.models import Engagement, MediaJob, Post, ProviderCost, ProviderSync, UsageRecord
from social.providers import Claude, OpenAI


def test_token_costs_follow_the_price_table():
    claude = usage.token_units("anthropic", {"input_tokens": 1000, "output_tokens": 2000,
                                             "cache_read_input_tokens": 10_000, "cache_creation_input_tokens": 0})
    assert claude == {"input": 1000, "cached_input": 10_000, "output": 2000}
    # $4 and $20 per million, cache reads $0.20 per million.
    assert usage.token_cost("anthropic", "claude-opus-5-5", claude) == pytest.approx(0.004 + 0.04 + 0.002)
    responses = usage.token_units("openai", {"input_tokens": 3000, "output_tokens": 500,
                                             "input_tokens_details": {"cached_tokens": 1000}})
    assert responses == {"input": 2000, "cached_input": 1000, "output": 500}
    assert usage.token_cost("openai", "gpt-6-astra", responses) == pytest.approx(0.02 + 0.001 + 0.025)
    audio = usage.token_units("openai", {"prompt_tokens": 1200, "completion_tokens": 300,
                                         "prompt_tokens_details": {"audio_tokens": 1000}})
    assert usage.token_cost("openai", "gpt-audio-1.5", audio) == pytest.approx(200 * 2.5e-6 + 1000 * 32e-6 + 300 * 10e-6)
    # A dated snapshot uses its model's row; a model missing from the table has no price, not a guess.
    assert usage.token_cost("openai", "gpt-6-astra-2026-10-01", responses) is not None
    assert usage.token_cost("openai", "unknown-model", responses) is None


def test_requests_are_recorded_for_their_post_and_saved_after_the_step(db, settings):
    def claude(request):
        body = json.loads(request.content)
        assert body["model"] == "claude-opus-5-5"
        return httpx.Response(200, json={
            "stop_reason": "end_turn", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": '{"parts": []}'}],
            "usage": {"input_tokens": 1000, "output_tokens": 500},
        })

    def openai(request):
        return httpx.Response(200, json={"data": [{"embedding": [0.1]}], "usage": {"prompt_tokens": 50}})

    settings.anthropic_api_key, settings.openai_api_key = SecretStr("key"), SecretStr("key")
    with usage.attributed(post_id="post-1", platform="instagram"):
        Claude(settings, httpx.Client(transport=httpx.MockTransport(claude))).generate({})
    OpenAI(settings, httpx.Client(transport=httpx.MockTransport(openai))).call(
        "embeddings", {"model": "text-embedding-3-small", "input": "x"})
    assert db.scalars(select(UsageRecord)).all() == []  # Nothing is written mid-step.
    assert usage.flush(db) == 2
    caption, embedding = sorted(db.scalars(select(UsageRecord)).all(), key=lambda r: r.provider)
    assert (caption.purpose, caption.post_id, caption.platform) == ("caption", "post-1", "instagram")
    assert caption.cost_usd == pytest.approx(0.004 + 0.01) and caption.estimated
    assert (embedding.purpose, embedding.post_id) == ("embedding", None)


def test_runway_charges_are_recorded_once_with_their_media_job(db, settings):
    settings.ai_mode, settings.runway_api_key = "live", SecretStr("mock")
    job = MediaJob(batch_key="usage-test", platform="instagram", state="running", stage="visual", task_id="task-1",
                   submitted_at=now(), brief={"execution_mode": "live", "operation": "video", "active_generation": "scene",
                                              "active_model": "nano_banana_2.1", "prompt": "p"})
    db.add(job)
    db.commit()
    statuses = [{"status": "RUNNING", "estimatedCost": {"credits": 6}}, {"status": "FAILED", "cost": {"credits": 4}}]
    runway = SimpleNamespace(task=lambda task_id: statuses.pop(0), close=lambda: None)
    process_jobs(db, settings, provider=runway)
    assert job.brief["active_estimate"] == 6 and db.scalars(select(UsageRecord)).all() == []
    process_jobs(db, settings, provider=runway)
    [record] = db.scalars(select(UsageRecord)).all()
    # Runway's reported charge, not the estimate; a scene image for this job and platform.
    assert (record.provider, record.purpose, record.model) == ("runway", "scene_image", "nano_banana_2.1")
    assert (record.units, record.cost_usd, record.estimated) == ({"credits": 4}, 0.04, False)
    assert (record.media_job_id, record.platform, record.request_id) == (job.id, "instagram", "task-1")
    # A resumed task polled again is charged once.
    usage.record_runway_task(job, {"cost": {"credits": 4}})
    usage.flush(db)
    assert len(db.scalars(select(UsageRecord)).all()) == 1


def test_billed_costs_are_read_from_each_cost_api(db, settings, monkeypatch):
    settings.ai_mode = "live"
    settings.runway_api_key = SecretStr("runway")
    settings.openai_admin_key, settings.openai_cost_project_id = SecretStr("sk-admin-x"), "proj_tudelu"
    settings.anthropic_api_key, settings.anthropic_cost_api_key_id = SecretStr("sk-ant-owner"), "apikey_app"
    today = now().date()
    seen = {}

    def handler(request):
        url = urlsplit(str(request.url))
        query = parse_qs(url.query)
        seen[url.netloc + url.path] = (dict(request.headers), query)
        if url.path == "/v1/organization":
            return httpx.Response(200, json={"creditBalance": 5000, "tier": {}, "usage": {}})
        if url.path == "/v1/organization/usage":
            # Runway takes at most 90 days a request, so a 95-day read is two requests.
            body = json.loads(request.content)
            seen.setdefault("runway ranges", []).append((body["startDate"], body["beforeDate"]))
            inside = body["startDate"] <= str(today) < body["beforeDate"]
            return httpx.Response(200, json={"results": [{"date": str(today), "usedCredits": [
                {"model": "veo3.1", "amount": 160}, {"model": "nano_banana_2.1", "amount": 4}]}] if inside else []})
        if url.path == "/v1/organization/costs":
            start = int(query["start_time"][0])
            return httpx.Response(200, json={"object": "page", "has_more": False, "next_page": None, "data": [
                {"object": "bucket", "start_time": start, "end_time": start + 86400, "results": [
                    {"object": "organization.costs.result", "amount": {"value": 1.25, "currency": "usd"},
                     "line_item": "gpt-6-astra, input_tokens", "project_id": None}]}]})
        if url.path == "/v1/organizations/analytics/cost":
            # At most 31 days a request; today's row is in the range that holds it.
            inside = query["starting_at"][0][:10] <= str(today) <= query["ending_at"][0][:10]
            return httpx.Response(200, json={"has_more": False, "next_page": None, "data_refreshed_at": "x", "data": [
                {"starting_at": f"{today}T00:00:00Z", "ending_at": "", "group": {"model": "claude-opus-5-5"},
                 "cost": {"currency": "USD", "amount": "250.000000", "requests": 9}}] if inside else []})
        return httpx.Response(404)

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: real_client(transport=httpx.MockTransport(handler)))
    assert usage.sync(db, settings) == {"runway": "synced", "openai": "synced", "anthropic": "synced"}
    billed = {(c.provider, c.item): c.amount_usd for c in db.scalars(select(ProviderCost)).all()}
    assert billed[("runway", "veo3.1")] == pytest.approx(1.60) and billed[("anthropic", "claude-opus-5-5")] == 2.50
    assert billed[("openai", "gpt-6-astra, input_tokens")] == 1.25
    # Each request asked only for this app's spend, with the documented parameters and headers.
    _, openai_query = seen["api.openai.com/v1/organization/costs"]
    assert openai_query["group_by"] == ["line_item"] and openai_query["project_ids"] == ["proj_tudelu"]
    headers, anthropic_query = seen["api.anthropic.com/v1/organizations/analytics/cost"]
    assert headers["x-api-key"] == "sk-ant-owner" and headers["anthropic-beta"] == usage.ANALYTICS_BETA
    assert anthropic_query["api_key_ids[]"] == ["apikey_app"] and anthropic_query["group_by[]"] == ["model"]
    assert db.get(ProviderSync, "runway").details["credit_balance"] == 5000


def test_what_each_provider_needs_before_its_costs_can_be_read(db, settings):
    assert "live mode" in usage.setup_needed(settings, "openai")
    settings.ai_mode = "live"
    assert "OPENAI_ADMIN_KEY" in usage.setup_needed(settings, "openai")
    assert "ANTHROPIC_API_KEY" in usage.setup_needed(settings, "anthropic")
    # The app's own Anthropic key works when it's an admin's personal key, so no admin key is needed.
    settings.anthropic_api_key = SecretStr("sk-ant-owner")
    assert usage.setup_needed(settings, "anthropic") == ""
    assert usage.sync(db, settings, fetch={})["openai"] == "not connected"


def published(db, settings, platform, *, ai, views, interactions):
    facts = load_facts(db, settings.facts_path)
    post = Post(
        platform=platform, campaign=f"{platform} post", asset_ids=[], facts_id=facts.id, status="published",
        published_at=now() - timedelta(days=1), dry_run=False, ai_disclosure=ai)
    db.add(post)
    db.flush()
    db.add(Engagement(post_id=post.id, metrics={"views": views, "total_interactions": interactions}))
    return post


def test_the_dashboard_leads_with_spend_and_performance_and_keeps_details_expandable(db, settings):
    real = published(db, settings, "instagram", ai=False, views=2000, interactions=100)
    concept = published(db, settings, "linkedin", ai=True, views=1000, interactions=20)
    job = MediaJob(batch_key="usage-dash", platform="linkedin", state="complete", brief={}, post_id=concept.id)
    db.add(job)
    db.flush()
    db.add_all([
        UsageRecord(provider="runway", purpose="video_clip", model="veo3.1", units={"credits": 160}, cost_usd=1.60,
                    estimated=False, request_id="t1", media_job_id=job.id, platform="linkedin"),
        UsageRecord(provider="anthropic", purpose="caption", model="claude-opus-5-5", units={"input": 1000},
                    cost_usd=0.40, post_id=real.id, platform="instagram"),
        UsageRecord(provider="openai", purpose="media_search", model="gpt-6-astra", units={"input": 500},
                    cost_usd=0.10),
    ])
    # Runway is connected: its billed total replaces the app's record of it.
    db.add(ProviderCost(provider="runway", day=now().date(), item="veo3.1", amount_usd=2.00, units={"credits": 200}))
    db.add(ProviderSync(provider="runway", synced_at=now(), error="", details={"scope": "this Runway API project"}))
    db.commit()
    report = usage.dashboard(db, settings, 30)
    assert report["total"] == pytest.approx(2.00 + 0.40 + 0.10) and report["published"] == 2
    assert report["cost_per_post"] == pytest.approx(2.50 / 2)
    assert report["overall"]["views"] == 3000 and report["overall"]["engagement"] == pytest.approx(120 / 3000)
    runway = next(p for p in report["providers"] if p["key"] == "runway")
    assert (runway["source"], runway["amount"], runway["recorded"]) == ("billed", 2.00, 1.60)
    linkedin = next(r for r in report["by_platform"] if r["platform"] == "linkedin")
    assert linkedin["cost_per_post"] == pytest.approx(1.60) and linkedin["cost_per_1k_views"] == pytest.approx(1.60)
    assert report["unattributed"] == pytest.approx(0.10)
    with TestClient(create_app(settings)) as client:
        page = client.get("/admin/usage?days=7").text
        assert "Usage &amp; costs" in page and "AI spend" in page and money(report["total"]) in page
        assert page.index("AI spend") < page.index("<h2>Costs</h2>") < page.index("<h2>Performance</h2>") < page.index(
            "<h2>Details</h2>")
        assert "Cost per post" in page and "Provider connections" in page and 'aria-current="true">Last 7 days' in page
        token = client.get("/api/session").json()["csrf"]
        response = client.post("/forms/usage/sync", data={"csrf": token, "days": "90"}, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/admin/usage?days=90"


def test_web_requests_save_the_ai_requests_they_made(db, settings):
    with TestClient(create_app(settings)) as client:
        usage.record_tokens("openai", "media_search", "gpt-6-astra", {"input_tokens": 100, "output_tokens": 10})
        assert client.get("/admin/assets").status_code == 200
    [record] = db.scalars(select(UsageRecord)).all()
    assert record.purpose == "media_search" and record.cost_usd == pytest.approx(0.001 + 0.0005)


def test_money_reads_well_at_every_size():
    assert [money(v) for v in (None, 0, 0.0421, 12.345, 4321.9)] == ["—", "$0.00", "$0.042", "$12.35", "$4,322"]
