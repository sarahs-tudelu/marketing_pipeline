import asyncio
import os
from datetime import timedelta
from html import unescape
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from test_campaign_edit import campaign_fixture  # noqa: F401

from social.adapters import ADAPTERS
from social.app import create_app
from social.db import now, uid
from social.media_queue import batch_wait_reason
from social.media_studio import replenish_campaigns
from social.models import Asset, MediaCampaign, MediaJob, Post, RequestRecord, Transition
from social.pipeline import draft


def job(db, campaign, *, post=None, state="complete", replaces=None):
    brief = dict(campaign.brief)
    if replaces:
        brief.update(workflow="immediate", replaces={"post_id": replaces.post_id, "clip_index": 0})
    row = MediaJob(
        campaign_id=None if replaces else campaign.id,
        platform="instagram",
        batch_key=uid(),
        brief=brief,
        post_id=post.id if post else None,
        state=state,
    )
    db.add(row)
    db.commit()
    return row


def test_two_private_campaigns_queue_at_their_interval_while_posts_wait(
    db, settings, campaign, assets, house_logo, monkeypatch
):
    start = now()
    due = start + timedelta(minutes=30)
    campaign.brief = campaign.brief | {"interval_hours": 0.5}
    campaign.enabled, campaign.sequence, campaign.next_run_at = True, 1, due
    video = campaign.brief | {"campaign": "Photo to video", "operation": "video", "duration": 8,
                              "reference_ids": [assets[0].id]}
    other = MediaCampaign(name=video["campaign"], brief=video, enabled=True, sequence=1, next_run_at=due)
    db.add(other)
    db.commit()
    queued_posts = []
    for owner, status in [(campaign, "scheduled"), (campaign, "awaiting_review"), (other, "scheduled")]:
        previous = draft(db, "instagram", owner.name, [assets[0].id], owner.brief["facts_id"])
        previous.simulation_only, previous.status = True, status
        if status == "scheduled":
            previous.scheduled_at = start + timedelta(days=1)
        job(db, owner, post=previous)
        queued_posts.append((previous, status, previous.scheduled_at))

    monkeypatch.setitem(ADAPTERS, "instagram", lambda *args: SimpleNamespace(dry=False))
    settings.ai_mode, settings.live_platforms, settings.runway_api_key = "live", "instagram", SecretStr("test")
    tick = due - timedelta(seconds=1)
    monkeypatch.setattr("social.media_studio.now", lambda: tick)
    replenish_campaigns(db, settings)
    assert campaign.sequence == other.sequence == 1
    tick = due
    replenish_campaigns(db, settings)
    replenish_campaigns(db, settings)
    assert campaign.sequence == other.sequence == 2
    assert campaign.next_run_at == other.next_run_at == due + timedelta(minutes=30)
    for owner in [campaign, other]:
        new = db.scalar(select(MediaJob).where(MediaJob.batch_key == f"{owner.id}:1:instagram"))
        assert new.state == "queued" and new.brief["operation"] == owner.brief["operation"]
    for previous, status, delivery_time in queued_posts:
        db.refresh(previous)
        assert previous.simulation_only and previous.status == status and previous.scheduled_at == delivery_time


def test_campaign_keeps_producing_without_repeating_stopped_platform(db, settings, campaign, monkeypatch):
    campaign.enabled = True
    campaign.brief = campaign.brief | {"platforms": ["instagram", "facebook"]}
    stopped = job(db, campaign, state="failed")
    monkeypatch.setitem(ADAPTERS, "facebook", lambda *args: SimpleNamespace(dry=False))
    settings.ai_mode, settings.live_platforms, settings.runway_api_key = "live", "facebook", SecretStr("test")
    replenish_campaigns(db, settings)
    assert campaign.sequence == 1 and not campaign.error
    jobs = db.scalars(select(MediaJob).where(MediaJob.campaign_id == campaign.id)).all()
    assert [(j.platform, j.state) for j in jobs if j.id != stopped.id] == [("facebook", "queued")]
    assert stopped.state == "failed"


def test_simulated_post_only_releases_private_campaign_after_media_completes(db, campaign, post):
    post.simulation_only, post.status = True, "held"
    original = job(db, campaign, post=post)
    assert not batch_wait_reason(db, campaign.id)
    repair = job(db, campaign, state="running", replaces=original)
    assert "media job in the previous batch is running" in batch_wait_reason(db, campaign.id)
    repair.state = "failed"
    db.commit()
    assert "media job in the previous batch is failed" in batch_wait_reason(db, campaign.id)
    repair.state = "cancelled"
    campaign.brief = campaign.brief | {"rights": "public"}
    db.commit()
    assert "post in the previous batch is held" in batch_wait_reason(db, campaign.id)


def ids(client, campaign_id=""):
    return [r["id"] for r in client.get("/api/media-jobs", params={"campaign_id": campaign_id}).json()]


@pytest.mark.parametrize("status", ["draft", "generated", "held", "awaiting_review", "approved", "scheduled"])
def test_delete_completed_draft_from_its_current_list(db, settings, campaign, post, assets, status):
    post.status = status
    if status == "scheduled":
        post.scheduled_at = post.next_attempt_at = now()
    first = job(db, campaign, post=post)
    first.output_asset_id = assets[0].id
    sibling = draft(db, "facebook", campaign.name, [assets[0].id], post.facts_id)
    sibling_job = job(db, campaign, post=sibling)
    scope = f"?campaign_id={campaign.id}"
    with TestClient(create_app(settings)) as client:
        url = f"/forms/posts/{post.id}/delete{scope}"
        page = client.get("/admin/media-queue" + scope)
        if status in {"approved", "scheduled"}:
            query = f"?source=published-posts&campaign_id={campaign.id}"
            assert post.id not in page.text
            assert f"/posts/{post.id}{query}" in unescape(client.get("/admin/published-posts" + scope).text)
            url = f"/forms/posts/{post.id}/delete{query}"
            page = client.get(f"/posts/{post.id}{query}")
            destination, data = "/admin/published-posts" + scope, {}
        else:
            destination, data = "/admin/media-queue" + scope, {"return_to": "/admin/media-queue"}
        assert url in unescape(page.text) and "Delete draft" in page.text
        assert client.post(url, data=data).status_code == 403
        data["csrf"] = client.get("/api/session").json()["csrf"]
        for _ in range(2):
            response = client.post(url, data=data, follow_redirects=False)
            assert (
                response.status_code == 303 and response.headers["location"] == destination
            )
        assert ids(client, campaign.id) == [sibling_job.id]
    db.refresh(post)
    assert post.status == "deleted" and not post.scheduled_at and not post.next_attempt_at
    assert db.get(Asset, first.output_asset_id) and db.get(Post, sibling.id).status == "draft"
    assert db.scalar(
        select(Transition.actor).where(Transition.post_id == post.id, Transition.to_status == "deleted")
    )


def test_delivery_evidence_blocks_stale_delete_and_dismiss(db, settings, campaign, post):
    post.status = "held"
    item = job(db, campaign, post=post)
    with TestClient(create_app(settings)) as client:
        assert "Delete draft" in client.get("/admin/media-queue").text
        token = client.get("/api/session").json()["csrf"]
        db.add(RequestRecord(post_id=post.id, step="publish", request={}, state="pending", dry_run=False))
        db.commit()
        assert "Delete draft" not in client.get("/admin/media-queue").text
        reply = client.post(
            f"/forms/posts/{post.id}/delete", data={"csrf": token, "return_to": "/admin/media-queue"}
        )
        assert reply.status_code == 400 and "Reconcile" in reply.text
        assert (
            client.post(
                f"/forms/media-jobs/{item.id}/dismiss", data={"csrf": token, "post_id": post.id}
            ).status_code
            == 400
        )
        assert ids(client) == [item.id]


@pytest.mark.parametrize("resolution", ["delete", "published"])
def test_campaign_follows_replacement_chain_and_releases_next_batch(
    db, settings, campaign, post, assets, monkeypatch, resolution
):
    post.status = "failed"
    root = job(db, campaign, post=post)
    old = draft(db, "instagram", campaign.name, [assets[0].id], post.facts_id)
    old.status = "failed"
    middle = job(db, campaign, post=old, replaces=root)
    latest = draft(db, "instagram", campaign.name, [assets[0].id], post.facts_id)
    latest.status = "awaiting_review"
    current = job(db, campaign, post=latest, replaces=middle)
    campaign.sequence, campaign.enabled = 1, True
    campaign.error = "Waiting for the previous batch to finish or for held work to be corrected."
    db.commit()
    monkeypatch.setitem(ADAPTERS, "instagram", lambda *args: SimpleNamespace(dry=False))
    settings.ai_mode, settings.live_platforms, settings.runway_api_key = (
        "live",
        "instagram",
        SecretStr("test"),
    )
    replenish_campaigns(db, settings)
    assert (
        campaign.sequence == 1 and "Instagram post in the previous batch is awaiting review" in campaign.error
    )
    with TestClient(create_app(settings)) as client:
        assert ids(client) == ids(client, campaign.id) == [current.id]
        assert f"/admin/media-queue?campaign_id={campaign.id}" in client.get("/admin/media-studio").text
        if resolution == "delete":
            response = client.post(
                f"/forms/posts/{latest.id}/delete",
                data={
                    "return_to": "/admin/media-queue",
                    "csrf": client.get("/api/session").json()["csrf"],
                },
            )
            assert response.status_code == 200
        else:
            latest.status = "published"
            db.commit()
        assert ids(client, campaign.id) == []
        assert campaign.error not in client.get("/admin/media-studio").text
    db.expire_all()
    replenish_campaigns(db, settings)
    assert campaign.sequence == 2 and not campaign.error
    assert db.get(Post, root.post_id).status == db.get(Post, middle.post_id).status == "failed"
    assert db.scalar(select(MediaJob).where(MediaJob.batch_key == f"{campaign.id}:1:instagram"))


def test_cancelling_retry_restores_held_draft_and_closing_failure_releases_campaign(
    db, settings, campaign, post
):
    post.status = "held"
    original = job(db, campaign, post=post)
    retry = job(db, campaign, state="queued", replaces=original)
    with TestClient(create_app(settings)) as client:
        token = client.get("/api/session").json()["csrf"]
        assert ids(client, campaign.id) == [retry.id]
        assert "media job in the previous batch is queued" in batch_wait_reason(db, campaign.id)
        client.post(f"/forms/media-jobs/{retry.id}/cancel", data={"csrf": token})
        assert ids(client, campaign.id) == [original.id]
        assert "post in the previous batch is held" in batch_wait_reason(db, campaign.id)
        post.status = "failed"
        db.commit()
        assert "Close failed attempt" in client.get("/admin/media-queue").text
        assert "failed" in batch_wait_reason(db, campaign.id)
        url = f"/forms/media-jobs/{original.id}/dismiss"
        assert client.post(url, data={"csrf": token, "post_id": "stale"}).status_code == 400
        assert client.post(url, data={"csrf": token, "post_id": post.id}).status_code == 200
        assert ids(client) == []
    assert not batch_wait_reason(db, campaign.id)
    db.refresh(original)
    assert original.state == "complete" and original.dismissed_at and post.status == "failed"


def test_old_blocker_is_visible_ahead_of_completed_library_jobs(db, settings, campaign, post):
    post.status = "held"
    blocker = job(db, campaign, post=post)
    db.add_all(
        [
            MediaJob(state="complete", platform="instagram", brief=campaign.brief, batch_key=uid())
            for _ in range(105)
        ]
    )
    db.commit()
    with TestClient(create_app(settings)) as client:
        assert ids(client)[0] == blocker.id
        assert ids(client, campaign.id) == [blocker.id]
        assert client.get("/admin/media-queue").context["active_jobs"] == 1


def test_hiding_library_work_keeps_media_and_published_history(db, settings, campaign, post, assets):
    post.status = "published"
    published = job(db, campaign, post=post)
    library = job(db, campaign)
    library.output_asset_id = published.output_asset_id = assets[0].id
    db.commit()
    with TestClient(create_app(settings)) as client:
        page = client.get("/admin/media-queue").text
        assert "Delete draft" not in page and page.count("Hide from In progress") == 1
        assert post.id not in page and post.id in client.get("/admin/published-posts").text
        for row in (library, published):
            url = f"/forms/media-jobs/{row.id}/dismiss"
            assert client.post(url).status_code == 403
            assert (
                client.post(
                    url,
                    data={"csrf": client.get("/api/session").json()["csrf"], "post_id": row.post_id or ""},
                ).status_code
                == 200
            )
        assert ids(client) == []
    db.expire_all()
    assert post.status == "published" and library.output_asset_id == published.output_asset_id == assets[0].id


@pytest.mark.skipif(not os.getenv("BROWSER_BINARY"), reason="BROWSER_BINARY is not configured")
def test_campaign_queue_polls_for_replacement_and_deletes_it_in_browser(db, settings, campaign, post, assets):
    from test_studio_ui import serve_studio_request

    playwright = pytest.importorskip("playwright.async_api")
    campaign.sequence = 1
    post.status = "held"
    original = job(db, campaign, post=post)

    async def exercise(client):
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(
                executable_path=os.environ["BROWSER_BINARY"], headless=True
            )
            page = await browser.new_page()
            await page.route("**/*", lambda route: serve_studio_request(route, client))
            await page.goto("http://studio.test/admin/media-studio")
            await page.get_by_role("link", name="View unfinished campaign work").click()
            await playwright.expect(page.locator(f"#job-{original.id}")).to_be_visible()
            await page.get_by_role("link", name="Review media & caption").click()
            await page.get_by_role("link", name="Back to In progress").click()
            await playwright.expect(page).to_have_url(f"http://studio.test/admin/media-queue?campaign_id={campaign.id}")
            post.status = "failed"
            revised = draft(db, "instagram", campaign.name, [assets[0].id], post.facts_id)
            redo = job(db, campaign, post=revised, replaces=original)
            await playwright.expect(page.locator(f"#job-{redo.id}")).to_be_visible(timeout=12000)
            await playwright.expect(page.locator(f"#job-{original.id}")).to_have_count(0)
            page.once("dialog", lambda dialog: dialog.accept())
            await page.get_by_role("button", name="Delete draft").click()
            await playwright.expect(
                page.get_by_text("This campaign has no unfinished jobs or posts.")
            ).to_be_visible()
            db.refresh(revised)
            assert revised.status == "deleted" and db.get(Asset, assets[0].id)
            await browser.close()

    with TestClient(create_app(settings), base_url="http://studio.test") as client:
        asyncio.run(exercise(client))
