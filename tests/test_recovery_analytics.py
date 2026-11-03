from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from social.adapters import ADAPTERS
from social.analytics import collect, fetch, report
from social.db import now
from social.gate import evaluate, run_gate
from social.models import Control, Engagement, Post
from social.pipeline import publish_due
from social.scheduler import schedule
from social.worker import advance_queue


def test_worker_recovers_acknowledged_post_even_if_rights_later_expire(db, settings, post, assets):
    from social.lifecycle import transition

    run_gate(db, settings, post)
    at = schedule(db, settings, post)
    transition(db, post, "publishing", "Test interrupted worker")
    db.commit()
    result = ADAPTERS[post.platform](settings, db, post).publish(post, post.idempotency_key)
    # Acknowledgement is durable; Post finalization was interrupted.
    assert not post.remote_id
    assets[0].rights_expire_at = now() - timedelta(days=1)
    db.commit()
    publish_due(db, settings, at=at + timedelta(days=1))
    assert post.status == "published"
    assert post.remote_id == result.platform_post_id


def test_retry_respects_spacing_against_another_recent_delivery(db, settings, post):
    from social.lifecycle import transition

    run_gate(db, settings, post)
    at = schedule(db, settings, post)
    transition(db, post, "publishing", "Test waiting for safe retry")
    db.add(
        Post(
            platform=post.platform,
            account_id=post.account_id,
            campaign="other",
            facts_id=post.facts_id,
            asset_ids=post.asset_ids,
            status="published",
            published_at=at - timedelta(minutes=20),
        )
    )
    db.commit()
    publish_due(db, settings, at=at)
    assert post.status == "publishing" and not post.remote_id


def test_unexpected_failure_counts_toward_automatic_pause(db, settings, post, monkeypatch):
    from social.models import Account

    settings = settings.model_copy(update={"failure_pause_threshold": 1})
    run_gate(db, settings, post)
    at = schedule(db, settings, post)

    class Broken:
        dry = True

        def publish(self, *args):
            raise RuntimeError("Unexpected transport state")

    monkeypatch.setattr("social.adapters.adapter", lambda *args: Broken())
    publish_due(db, settings, at=at)
    assert post.status == "failed"
    assert db.get(Account, post.platform).paused


def test_public_media_urls_must_resolve(db, settings, post, monkeypatch):
    monkeypatch.setattr("social.gate.public_url", lambda url: url)
    settings = settings.model_copy(update={"media_base_url": "https://media.example.org"})
    post.platform = "instagram"
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404)))
    checks = evaluate(db, settings, post, live=True, url_client=client)[0]
    assert not next(c for c in checks if c.name == "media_urls").passed


def test_auto_advance_stops_on_kill_switch(db, settings, post):
    control = db.get(Control, 1)
    control.killed = True
    db.commit()
    advance_queue(db, settings)
    assert post.status == "generated"
    control.killed = False
    db.commit()
    advance_queue(db, settings)
    assert post.status == "scheduled"


@pytest.mark.parametrize("platform", ["linkedin", "instagram", "facebook", "x", "tiktok"])
def test_insights_read_only_contracts(db, settings, post, monkeypatch, platform):
    post.platform, post.dry_run, post.remote_id = (
        platform,
        False,
        "urn:li:share:123" if platform == "linkedin" else "123",
    )
    calls = []

    class Probe:
        base = "https://official.example/v2"
        owner = "urn:li:organization:123"

        def connected(self):
            return True

        def get_status(self, remote):
            return {"data": {"publicaly_available_post_id": [123]}}

        def direct(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            if platform in {"instagram", "facebook"}:
                return {"data": [{"name": "views", "values": [{"value": 100}]}]}
            if platform == "linkedin":
                return {"elements": [{"totalShareStatistics": {"impressionCount": 100}}]}
            if platform == "x":
                return {"data": {"public_metrics": {"impression_count": 100}}}
            return {"data": {"videos": [{"id": "123", "view_count": 100}]}}

    monkeypatch.setattr("social.analytics.adapter", lambda *args: Probe())
    assert fetch(db, settings, post)
    assert len(calls) == 1
    method, url, kwargs = calls[0]
    assert method == ("POST" if platform == "tiktok" else "GET")
    assert not any(part in url for part in ("/publish", "/likes", "/comments", "/follow"))


def test_analytics_cadence_suggestions_never_change_caps(db, settings, post):
    original = settings.profiles_path.read_bytes()
    for i in range(10):
        measured = Post(
            platform="linkedin",
            campaign="measured",
            facts_id=post.facts_id,
            asset_ids=post.asset_ids,
            dry_run=False,
            status="published",
            published_at=now() - timedelta(days=10 - i),
        )
        db.add(measured)
        db.flush()
        db.add(
            Engagement(
                post_id=measured.id,
                simulated=False,
                metrics={"impressionCount": 100, "likeCount": 10 if i < 5 else 4},
            )
        )
    db.commit()
    result = report(db, settings)
    item = next(r for r in result["recommendations"] if r["platform"] == "linkedin")
    assert "fell over 25%" in item["suggestion"]
    assert not item["automatically_applied"]
    assert settings.profiles_path.read_bytes() == original


def test_demo_analytics_explicitly_simulated_and_daily_deduped(db, settings, post):
    post.status, post.published_at = "published", now()
    db.commit()
    collect(db, settings)
    collect(db, settings)
    rows = db.scalars(select(Engagement)).all()
    assert len(rows) == 1 and rows[0].simulated and rows[0].metrics["simulated"]
