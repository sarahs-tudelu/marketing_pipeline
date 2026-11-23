import asyncio
import os
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_campaign_edit import campaign_fixture  # noqa: F401
from test_human_review import ready
from test_media_queue_cleanup import job

from social.app import create_app
from social.db import now
from social.pipeline import draft


def make_post(db, original, platform, title, status, *, dry=True):
    post = draft(db, platform, title, original.asset_ids, original.facts_id)
    post.status, post.dry_run = status, dry
    db.commit()
    return post


def test_archive_uses_recorded_delivery_mode_and_groups_by_platform(db, settings, post):
    scheduled = make_post(db, post, "linkedin", "Due tomorrow", "scheduled", dry=False)
    real = make_post(db, post, "facebook", "Real installation", "published", dry=False)
    older = make_post(db, post, "facebook", "Earlier project", "published", dry=False)
    real.published_at, older.published_at = now(), now() - timedelta(days=10)
    manual = make_post(db, post, "tiktok", "Finish in the app", "awaiting_manual", dry=False)
    simulated = make_post(db, post, "facebook", "Practice run", "published")
    approved = make_post(db, post, "x", "Waiting for a slot", "approved")
    failed = make_post(db, post, "linkedin", "Failed delivery", "failed")
    with TestClient(create_app(settings)) as client:
        archive = client.get("/admin/published-posts")
        groups = archive.context["published_groups"]
        assert list(groups["real"]) == ["facebook", "linkedin", "tiktok"]
        assert [row.id for row in groups["real"]["facebook"]] == [real.id, older.id]
        assert (
            groups["real"]["linkedin"][0].id == scheduled.id and groups["real"]["tiktok"][0].id == manual.id
        )
        assert groups["simulated"]["facebook"][0].id == simulated.id
        assert groups["unscheduled"]["x"][0].id == approved.id
        assert failed.id not in archive.text and "Awaiting TikTok completion" in archive.text
        queue = client.get("/admin/media-queue")
        assert failed.id in queue.text and post.id in queue.text
        assert all(p.id not in queue.text for p in [real, scheduled, manual, simulated, approved])
        assert queue.context["active_jobs"] == 2


def test_review_slides_follow_current_campaign_attempts_and_exclude_other_work(db, settings, campaign, post):
    post.status = "awaiting_review"
    original = job(db, campaign, post=post)
    replacement = make_post(db, post, "instagram", "Current version", "awaiting_review")
    job(db, campaign, post=replacement, replaces=original)
    other = make_post(db, post, "linkedin", "Outside this campaign", "awaiting_review")
    failed = make_post(db, post, "x", "Needs correction", "held")
    with TestClient(create_app(settings)) as client:
        queue = client.get("/admin/media-queue")
        sections = queue.context["queue_sections"]
        assert [p.id for p, _ in sections["review"]["posts"]] == [replacement.id, other.id]
        assert [p.id for p, _ in sections["attention"]["posts"]] == [failed.id]
        assert queue.context["active_jobs"] == 3
        scoped = f"?source=media-queue&campaign_id={campaign.id}"
        slide = client.get(f"/posts/{replacement.id}" + scoped)
        assert slide.context["back_url"] == f"/admin/media-queue?campaign_id={campaign.id}"
        assert slide.context["slides"] == dict(
            id=replacement.id, position=1, total=1, previous=None, next=None, query=scoped
        )
        assert not client.get(f"/posts/{post.id}").context.get("slides")
        assert (
            "Outside this campaign"
            not in client.get("/admin/media-queue", params={"campaign_id": campaign.id}).text
        )


def test_last_approval_opens_delivery_group_and_stopping_returns_post_to_review(db, settings, post):
    ready(db, settings, post)
    with TestClient(create_app(settings)) as client:
        path = f"/posts/{post.id}?source=media-queue"
        preview = client.get(path)
        fields = {name: preview.context[name] for name in ("csrf", "reviewed_digest")}
        url = f"/forms/posts/{post.id}/approve?source=media-queue"
        invalid = client.post(url, data=fields | {"reviewed_digest": "old"})
        assert invalid.status_code == 400 and invalid.context["slides"]["total"] == 1
        response = client.post(url, data=fields, follow_redirects=False)
        assert (
            response.status_code == 303
            and response.headers["location"] == "/admin/published-posts#simulated-linkedin"
        )
        assert post.id not in client.get("/admin/media-queue").text
        stopped = client.post(
            f"/forms/posts/{post.id}/stop?source=published-posts", data={"csrf": fields["csrf"]}
        )
        db.refresh(post)
        assert stopped.status_code == 200 and post.status == "awaiting_review"
        assert post.id in client.get("/admin/media-queue").text
        assert post.id not in client.get("/admin/published-posts").text


@pytest.mark.skipif(not os.getenv("BROWSER_BINARY"), reason="BROWSER_BINARY is not configured")
def test_review_slideshow_and_collapsible_delivery_history_in_browser(db, settings, post):
    from test_studio_ui import serve_studio_request

    playwright = pytest.importorskip("playwright.async_api")
    ready(db, settings, post)
    other = make_post(db, post, "instagram", "Roof installation", "awaiting_review")
    make_post(db, post, "facebook", "Check the footage", "held")
    make_post(db, post, "facebook", "Completed project", "published", dry=False)

    async def exercise(client):
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(
                executable_path=os.environ["BROWSER_BINARY"], headless=True
            )
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.route("**/*", lambda route: serve_studio_request(route, client))
            await page.goto("http://studio.test/admin/media-queue")
            await playwright.expect(page.get_by_role("region", name="Failed or held")).to_contain_text(
                "Check the footage"
            )
            await page.get_by_role("link", name="Start review slideshow").click()
            await playwright.expect(page.get_by_text("Review 1 of 2")).to_be_visible()
            await playwright.expect(page.get_by_role("button", name="Previous post")).to_be_disabled()
            await page.get_by_role("link", name="Next post").click()
            await playwright.expect(page.get_by_role("heading", name=other.campaign)).to_be_visible()
            await page.keyboard.press("ArrowLeft")
            await playwright.expect(page.get_by_text("Review 1 of 2")).to_be_visible()
            await page.get_by_role("link", name="Back to In progress").click()
            await page.get_by_role("link", name="Start review slideshow").click()
            await page.get_by_role("button", name="Approve & schedule").click()
            await playwright.expect(page.get_by_text("Review 1 of 1")).to_be_visible()
            await playwright.expect(page.get_by_role("heading", name=other.campaign)).to_be_visible()
            await page.get_by_role("link", name="Published posts", exact=True).click()
            archive = page.locator("#simulated-linkedin")
            await playwright.expect(archive.get_by_role("link", name=post.campaign)).to_be_hidden()
            await archive.locator("summary").click()
            await playwright.expect(archive.get_by_text("Scheduled", exact=True)).to_be_visible()
            await archive.get_by_role("link", name=post.campaign).click()
            await page.get_by_role("link", name="Back to Published posts").click()
            await playwright.expect(archive.get_by_role("link", name=post.campaign)).to_be_visible()
            await archive.locator("summary").click()
            await playwright.expect(archive.get_by_role("link", name=post.campaign)).to_be_hidden()
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await browser.close()

    with TestClient(create_app(settings), base_url="http://studio.test") as client:
        asyncio.run(exercise(client))
