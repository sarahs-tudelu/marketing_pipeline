from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_availability import mock_platform
from test_availability import published as published

from social.app import create_app
from social.availability import check_post
from social.db import now
from social.facts import content_digest
from social.gate import evaluate
from social.lifecycle import PostBusy
from social.models import CheckResult, Control, Post, PostAvailability, PostRepost, RequestRecord
from social.pipeline import delete_draft, draft, publish_due
from social.reposts import create, sources
from social.retries import retry_post
from social.worker import advance_queue


def test_repost_reuses_content_and_requires_review_with_automation_off(db, settings, published, monkeypatch):
    mock_platform(monkeypatch, lambda _: httpx.Response(404, json={}))
    before = (published.status, published.remote_id, published.published_at, published.idempotency_key)
    db.get(Control, 1).automation_enabled = False
    db.commit()
    with pytest.raises(ValueError, match="confirm"):
        create(db, settings, published, "editor")
    db.rollback()
    replacement = create(db, settings, published, "editor", True)
    assert replacement.id != published.id and replacement.idempotency_key != published.idempotency_key
    assert (replacement.caption, replacement.parts, replacement.asset_ids, replacement.facts_id) == (
        published.caption,
        published.parts,
        published.asset_ids,
        published.facts_id,
    )
    assert replacement.status == "generated" and replacement.human_review_required
    assert not replacement.remote_id and not replacement.human_approved_digest
    assert create(db, settings, published, "editor", True).id == replacement.id
    advance_queue(db, settings)
    assert replacement.status == "awaiting_review" and not replacement.scheduled_at
    assert before == (
        published.status,
        published.remote_id,
        published.published_at,
        published.idempotency_key,
    )
    assert not db.scalar(select(RequestRecord.id))
    assert db.scalar(select(PostRepost)).actor == "editor"


@pytest.mark.parametrize("status", [200, 401, 403, 429, 503])
def test_repost_refreshes_stale_missing_observation_and_refuses_available_or_unknown(
    db, settings, published, monkeypatch, status
):
    mock_platform(monkeypatch, lambda _: httpx.Response(404, json={}))
    assert check_post(db, settings, published).state == "unavailable"
    mock_platform(
        monkeypatch, lambda _: httpx.Response(status, json={"id": "12345"} if status == 200 else {})
    )
    with pytest.raises(ValueError, match="not created"):
        create(db, settings, published, "editor", True)
    assert not db.scalar(select(PostRepost))
    assert db.get(PostAvailability, published.id).state != "unavailable"


def test_gate_exempts_only_explicit_missing_original_not_other_duplicates(
    db, settings, published, monkeypatch
):
    mock_platform(monkeypatch, lambda _: httpx.Response(404, json={}))
    replacement = create(db, settings, published, "editor", True)
    duplicate = next(c for c in evaluate(db, settings, replacement)[0] if c.name == "duplicates")
    assert duplicate.passed and duplicate.details["excluded_unavailable_originals"] == [published.id]
    unrelated = draft(
        db, published.platform, "Another post", published.asset_ids, published.facts_id, published.product
    )
    unrelated.caption, unrelated.status = published.caption, "published"
    db.commit()
    duplicate = next(c for c in evaluate(db, settings, replacement)[0] if c.name == "duplicates")
    assert not duplicate.passed


def test_restored_original_blocks_delivery_even_outside_duplicate_window(
    db, settings, published, assets, monkeypatch
):
    # The delivery gate is under test, not posting hours: without this it fails after 17:00 New York.
    monkeypatch.setattr("social.scheduler.in_window", lambda at, profile: True)
    published.published_at = now() - timedelta(days=400)
    db.commit()
    mock_platform(monkeypatch, lambda _: httpx.Response(404, json={}))
    replacement = create(db, settings, published, "editor", True)
    replacement.status, replacement.dry_run = "scheduled", True
    replacement.scheduled_at = now() - timedelta(minutes=1)
    replacement.approved_digest = replacement.human_approved_digest = content_digest(replacement, assets)
    db.commit()
    mock_platform(monkeypatch, lambda _: httpx.Response(200, json={"id": "12345"}))
    publish_due(db, settings, at=replacement.scheduled_at)
    assert replacement.status == "held"
    duplicate = db.scalar(
        select(CheckResult).where(CheckResult.post_id == replacement.id, CheckResult.name == "duplicates")
    )
    assert not duplicate.passed and duplicate.details["repost_sources"][0]["state"] == "available"
    assert not db.scalar(select(RequestRecord.id))


def test_deleted_draft_can_be_prepared_again_without_erasing_repost_audit(
    db, settings, published, monkeypatch
):
    mock_platform(monkeypatch, lambda _: httpx.Response(404, json={}))
    first = create(db, settings, published, "editor", True)
    delete_draft(db, first, "editor")
    second = create(db, settings, published, "editor", True)
    assert second.id != first.id and first.status == "deleted"
    assert len(db.scalars(select(PostRepost)).all()) == 2


def test_retry_and_repost_chains_keep_original_missing_checks(db, settings, published, monkeypatch):
    mock_platform(monkeypatch, lambda _: httpx.Response(404, json={}))
    replacement = create(db, settings, published, "editor", True)
    replacement.status, replacement.dry_run = "failed", True
    db.commit()
    retry = retry_post(db, settings, replacement, "editor")
    assert [p.id for p in sources(db, retry)] == [published.id]
    # Simulate a later acknowledged publication of the replacement, then its removal.
    retry.status, retry.dry_run, retry.remote_id, retry.published_at = "published", False, "67890", now()
    db.commit()
    newest = create(db, settings, retry, "editor", True)
    assert [p.id for p in sources(db, newest)] == [retry.id, published.id]


def test_repost_form_requires_confirmation_and_redirects_to_review_draft(
    db, settings, published, monkeypatch
):
    mock_platform(monkeypatch, lambda _: httpx.Response(404, json={}))
    check_post(db, settings, published)
    with TestClient(create_app(settings)) as client:
        token = client.get("/api/session").json()["csrf"]
        path = f"/forms/posts/{published.id}/repost"
        assert client.post(path).status_code == 403
        assert client.post(path, data={"csrf": token}).status_code == 400
        response = client.post(path, data={"csrf": token, "confirmed_removed": "on"})
        assert response.status_code == 200 and "Repost draft" in response.text
        assert "Open repost" in client.get(f"/posts/{published.id}").text


def test_concurrent_repost_requests_make_one_draft(db, settings, published, monkeypatch):
    mock_platform(monkeypatch, lambda _: httpx.Response(404, json={}))
    engine, post_id = db.get_bind(), published.id
    ready = Barrier(2)

    def run():
        with Session(engine, expire_on_commit=False) as session:
            source = session.get(Post, post_id)
            ready.wait(timeout=5)
            try:
                return create(session, settings, source, "editor", True).id
            except PostBusy:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(run) for _ in range(2)]
        ids = [job.result(timeout=10) for job in jobs]
    links = db.scalars(select(PostRepost)).all()
    assert len(links) == 1 and {i for i in ids if i} == {links[0].repost_post_id}


@pytest.mark.parametrize(
    "state,dry", [("failed", False), ("publishing", False), ("awaiting_manual", False), ("published", True)]
)
def test_no_repost_for_unconfirmed_or_simulated_delivery(db, settings, published, state, dry):
    published.status, published.dry_run = state, dry
    db.commit()
    with pytest.raises(ValueError, match="confirmed live"):
        create(db, settings, published, "editor", True)
    assert not db.scalar(select(PostRepost))
