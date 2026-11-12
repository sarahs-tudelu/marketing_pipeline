import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from social.app import create_app
from social.db import now
from social.lifecycle import PostBusy
from social.models import Account, Control, MediaJob, Post, PostRetry, RequestRecord, Transition
from social.pipeline import publish_due
from social.retries import block_reason, retry_post
from social.scheduler import schedule
from social.worker import advance_queue


def failed(db, post, *, step="limit", state="rejected", irreversible=False):
    post.status, post.error = "failed", "policy: Platform policy/permission rejection"
    record = RequestRecord(post_id=post.id, step=step, state=state, irreversible=irreversible,
                           dry_run=post.dry_run, request={"method": "GET" if step == "limit" else "POST"},
                           response={"error": "original failure"})
    db.add(record)
    db.commit()
    return record


def test_retry_reuses_content_preserves_audit_and_completes_with_automation_off(db, settings, post, monkeypatch):
    record = failed(db, post)
    original_payload = (post.caption, post.parts, post.asset_ids, post.facts_id)
    job = MediaJob(platform=post.platform, batch_key="retry-test", brief={"workflow": "automatic"},
                   state="complete", post_id=post.id)
    db.add(job)
    db.get(Control, 1).automation_enabled = False
    db.commit()
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", Mock(side_effect=AssertionError("No live calls")))
    with TestClient(create_app(settings)) as client:
        token = client.get("/api/session").json()["csrf"]
        assert 'Retry post' in client.get(f"/posts/{post.id}").text
        assert client.post(f"/forms/posts/{post.id}/retry").status_code == 403
        response = client.post(f"/forms/posts/{post.id}/retry", data={"csrf": token}, follow_redirects=False)
        assert response.status_code == 303
        retry_id = response.headers["location"].split("/")[-1]
        repeat = client.post(f"/api/posts/{post.id}/retry", headers={"x-csrf-token": token})
        assert repeat.json()["id"] == retry_id
        assert "Open retry" in client.get(f"/posts/{post.id}").text
    retry = db.get(Post, retry_id)
    assert retry.status == "generated" and retry.id != post.id
    assert (retry.caption, retry.parts, retry.asset_ids, retry.facts_id) == original_payload
    assert retry.idempotency_key != post.idempotency_key and not retry.approved_digest
    db.refresh(job)
    assert job.post_id == retry.id
    advance_queue(db, settings)
    db.refresh(retry)
    assert retry.status == "scheduled" and retry.dry_run
    publish_due(db, settings, at=retry.scheduled_at)
    assert retry.status == "published" and retry.dry_run
    db.refresh(post)
    db.refresh(record)
    assert post.status == "failed" and post.error.startswith("policy:")
    assert record.state == "rejected" and record.response == {"error": "original failure"}
    assert len(db.scalars(select(PostRetry)).all()) == 1
    assert db.scalar(select(Transition).where(Transition.post_id == retry.id, Transition.to_status == "generated")).actor == "local-developer"


@pytest.mark.parametrize("step,state,irreversible,allowed", [
    ("publish", "pending", True, False), ("publish", "done", True, False),
    ("publish", "unexpected", True, False), ("publish", "done", False, False),
    ("inbox-init", "done", True, False), ("publish", "rejected", True, True),
    ("publish", "retryable", True, True), ("container-0", "done", False, True),
])
def test_retry_respects_delivery_checkpoints(db, settings, post, step, state, irreversible, allowed):
    failed(db, post, step=step, state=state, irreversible=irreversible)
    if allowed:
        assert retry_post(db, settings, post, "admin").status == "generated"
    else:
        with pytest.raises(ValueError, match="accepted"):
            retry_post(db, settings, post, "admin")
        db.rollback()
        assert db.scalars(select(PostRetry)).all() == []
        with TestClient(create_app(settings)) as client:
            token = client.get("/api/session").json()["csrf"]
            response = client.post(f"/forms/posts/{post.id}/retry", data={"csrf": token})
            assert response.status_code == 400 and "Post workspace" in response.text
            assert 'disabled>Retry post' in response.text


@pytest.mark.parametrize("field,value", [
    ("remote_id", "platform-id"), ("published_at", now()),
    ("live_url", "https://example.org/post"), ("error", "ambiguous: timeout"),
    ("status", "published"),
])
def test_retry_never_replays_confirmed_or_ambiguous_delivery(db, settings, post, field, value):
    failed(db, post)
    setattr(post, field, value)
    db.commit()
    with pytest.raises(ValueError):
        retry_post(db, settings, post, "admin")
    db.rollback()
    assert db.scalars(select(PostRetry)).all() == []


@pytest.mark.parametrize("pause", ["global", "platform"])
def test_retry_requires_explicit_resume_after_pause(db, settings, post, pause):
    failed(db, post)
    if pause == "global":
        db.get(Control, 1).killed = True
    else:
        db.get(Account, post.platform).paused = True
    db.commit()
    with pytest.raises(ValueError, match="[Rr]esume"):
        retry_post(db, settings, post, "admin")


def test_retry_failed_checks_hold_before_scheduling(db, settings, post, assets):
    failed(db, post)
    retry = retry_post(db, settings, post, "admin")
    assets[0].checksum = "not-the-file-checksum"
    db.commit()
    advance_queue(db, settings)
    assert retry.status == "held" and retry.scheduled_at is None
    assert not db.scalars(select(RequestRecord).where(RequestRecord.post_id == retry.id)).all()


def test_retry_preserves_simulation_when_platform_is_now_live(db, settings, post, monkeypatch):
    failed(db, post)
    retry = retry_post(db, settings, post, "admin")
    monkeypatch.setattr("social.adapters.adapter", lambda *a, **kw: SimpleNamespace(dry=False))
    advance_queue(db, settings)
    assert retry.status == "scheduled" and retry.dry_run


def test_live_retry_requires_same_connected_account_at_creation_and_scheduling(db, settings, post, monkeypatch):
    post.dry_run, post.account_id = False, "original-account"
    failed(db, post)
    account = db.get(Account, post.platform)
    account.external_id = "different-account"
    db.commit()
    assert "account changed" in block_reason(db, settings, post)
    account.external_id = "original-account"
    db.commit()
    selected = SimpleNamespace(dry=True, client=Mock())
    monkeypatch.setattr("social.adapters.adapter", lambda *a, **kw: selected)
    assert "enable live posting" in block_reason(db, settings, post)
    selected.dry = False
    retry = retry_post(db, settings, post, "admin")
    assert not retry.dry_run and retry.account_id == "original-account"
    retry.status = "approved"
    db.commit()
    selected.dry = True
    with pytest.raises(ValueError, match="original live account"):
        schedule(db, settings, retry)
    db.rollback()
    selected.dry = False
    account.external_id = "different-account"
    db.commit()
    with pytest.raises(ValueError, match="original live account"):
        schedule(db, settings, retry)


def test_concurrent_retry_clicks_create_only_one_attempt(db, settings, post):
    failed(db, post)
    engine, post_id = db.get_bind(), post.id
    ready = Barrier(2)

    def perform():
        with Session(engine, expire_on_commit=False) as session:
            source = session.get(Post, post_id)
            ready.wait(timeout=5)
            try:
                return retry_post(session, settings, source, "admin").id
            except PostBusy:
                return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(perform) for _ in range(2)]
        ids = [f.result(timeout=10) for f in futures]
    links = db.scalars(select(PostRetry)).all()
    assert len(links) == 1
    assert {id for id in ids if id} == {links[0].retry_post_id}
    assert retry_post(db, settings, post, "admin").id == links[0].retry_post_id


@pytest.mark.skipif(not os.getenv("BROWSER_BINARY"), reason="BROWSER_BINARY is not configured")
def test_retry_button_opens_new_attempt_and_shows_progress(db, settings, post, tmp_path):
    from test_studio_ui import serve_studio_request

    playwright = pytest.importorskip("playwright.async_api")
    failed(db, post)

    async def exercise(client):
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(executable_path=os.environ["BROWSER_BINARY"], headless=True)
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            await page.route("**/*", lambda route: serve_studio_request(route, client))
            await page.goto(f"http://studio.test/posts/{post.id}")
            await page.locator("#post-retry").screenshot(path=str(tmp_path / "retry-option.png"))
            async with page.expect_navigation():
                await page.get_by_role("button", name="Retry post", exact=True).click()
            await playwright.expect(page.locator("#post-retry")).to_contain_text("Retry attempt")
            await playwright.expect(page.locator("#post-retry .loading-spinner")).to_be_visible()
            retry_id = db.get(PostRetry, post.id).retry_post_id
            advance_queue(db, settings)
            # Route interception follows the redirect internally; use its resulting canonical URL.
            await page.goto(f"http://studio.test/posts/{retry_id}")
            await playwright.expect(page.locator(".section-head .status")).to_have_text("scheduled")
            await playwright.expect(page.locator("#post-retry .loading-spinner")).to_have_count(0)
            await page.goto(f"http://studio.test/posts/{post.id}")
            await playwright.expect(page.get_by_role("link", name="Open retry", exact=True)).to_have_attribute("href", f"/posts/{retry_id}")
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await browser.close()

    with TestClient(create_app(settings), base_url="http://studio.test") as client:
        asyncio.run(exercise(client))
