"""Optional browser coverage for trimming clips on the plan page and describing media changes on a post."""

import asyncio
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_idea_plans import facts, logo  # noqa: F401
from test_media_changes import IDEA, library
from test_studio_ui import serve_studio_request

from social.app import create_app
from social.lifecycle import transition
from social.media_studio import process_jobs
from social.models import MediaJob, Post

pytestmark = pytest.mark.skipif(not os.getenv("BROWSER_BINARY"), reason="BROWSER_BINARY is not configured")


def test_trim_clips_and_describe_changes_in_browser(db, settings, tmp_path, facts, logo):  # noqa: F811
    playwright = pytest.importorskip("playwright.async_api")
    walls, closed, sunset = library(db, settings)
    capture = Path(os.getenv("UI_CAPTURE_DIR", str(tmp_path)))
    capture.mkdir(parents=True, exist_ok=True)

    async def exercise(client):
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(executable_path=os.environ["BROWSER_BINARY"], headless=True)
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            page.set_default_timeout(8000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("dialog", lambda dialog: asyncio.ensure_future(dialog.accept()))

            await page.route("**/*", lambda route: serve_studio_request(route, client))
            await page.goto("http://studio.test/admin/create")
            await page.get_by_label("Your idea").fill(IDEA)
            await page.get_by_role("button", name="Plan this idea").click()
            await playwright.expect(page.get_by_text("Each clip lasts 3–5 seconds")).to_be_visible()
            plan = page.locator("#real-plan")
            await plan.get_by_label("To (s)").first.fill("3.5")
            await plan.locator('input[name="real_clip"]').nth(2).uncheck()
            await page.screenshot(path=str(capture / "plan-clip-trims.png"), full_page=True)
            await page.get_by_label("Changes for the editor (optional)").fill("End on the closed wall photo")
            await plan.locator("#real-library summary").click()
            await plan.locator("#real-library li").filter(has_text="partition-wall-closed.jpg").locator("input").check()
            await page.get_by_role("button", name="Plan the edit again").click()
            await playwright.expect(page.get_by_text("Planned again with your changes")).to_be_visible()
            await playwright.expect(plan.locator(".real-media li").last).to_contain_text("partition-wall-closed.jpg")
            # The trim and the left-out clip made by hand are kept when the edit is planned again.
            await playwright.expect(plan.get_by_label("To (s)").first).to_have_value("3.5")
            await playwright.expect(plan.locator(".real-media li")).to_have_count(3)
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.screenshot(path=str(capture / "plan-clip-trims-mobile.png"), full_page=True)
            await page.set_viewport_size({"width": 1440, "height": 1000})
            await page.get_by_role("button", name="Create now").click()
            await playwright.expect(page.get_by_role("heading", name="Generation queue")).to_be_visible()

            for _ in range(3):
                process_jobs(db, settings)
            job = db.scalar(select(MediaJob))
            db.refresh(job)
            assert job.state == "complete", job.error
            post = db.get(Post, job.post_id)
            for status in ("generated", "checked", "awaiting_review"):
                transition(db, post, status, "Test")
            db.commit()

            await page.goto(f"http://studio.test/posts/{post.id}")
            box = page.locator("#change-media")
            await box.get_by_label("What should change in the media?").fill("Make every clip 3 seconds")
            await box.locator("summary").click()
            await box.locator("li").filter(has_text="pergola-sunset.jpg").locator("input").check()
            await page.screenshot(path=str(capture / "post-change-media.png"), full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.screenshot(path=str(capture / "post-change-media-mobile.png"), full_page=True)
            await page.set_viewport_size({"width": 1440, "height": 1000})
            await box.get_by_role("button", name="Make these changes").click()
            await playwright.expect(page.get_by_text("again with your changes")).to_be_visible()
            await page.goto(f"http://studio.test/posts/{post.id}")
            await playwright.expect(page.get_by_text("Editing your media again…")).to_be_visible()
            assert not errors
            await browser.close()

    with TestClient(create_app(settings), base_url="http://studio.test") as client:
        asyncio.run(exercise(client))
    revised = [j for j in db.scalars(select(MediaJob)) if j.brief.get("revision")]
    assert len(revised) == 1 and sunset.id in revised[0].brief["reference_ids"]
