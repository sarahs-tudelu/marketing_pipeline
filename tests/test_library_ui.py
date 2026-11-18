"""Optional browser coverage for the media picker's filters and the asset library's filters and deletion."""

import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from test_idea_plans import facts, logo  # noqa: F401
from test_library import library

from social.app import create_app
from social.models import Asset

pytestmark = pytest.mark.skipif(not os.getenv("BROWSER_BINARY"), reason="BROWSER_BINARY is not configured")


def test_media_picker_and_library_filters_in_browser(db, settings, tmp_path, facts, logo):  # noqa: F811
    playwright = pytest.importorskip("playwright.async_api")
    video, terrace, concept, copy = library(db, settings)
    capture = Path(os.getenv("UI_CAPTURE_DIR", str(tmp_path)))
    capture.mkdir(parents=True, exist_ok=True)

    async def exercise(client):
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(executable_path=os.environ["BROWSER_BINARY"], headless=True)
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            page.set_default_timeout(5000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("dialog", lambda dialog: asyncio.ensure_future(dialog.accept()))

            async def serve(route):
                request = route.request
                assert urlsplit(request.url).netloc == "studio.test"
                client.cookies.clear()
                response = client.request(request.method, request.url, content=request.post_data_buffer,
                                          headers=await request.all_headers(), follow_redirects=False)
                await route.fulfill(
                    status=response.status_code,
                    headers={k: v for k, v in response.headers.items()
                             if k not in {"content-length", "content-encoding", "transfer-encoding"}},
                    body=response.content,
                )

            await page.route("**/*", serve)
            await page.goto("http://studio.test/admin/create")
            picker = page.locator("#idea-library")
            items = picker.locator("li")
            await picker.locator("summary").click()
            count = picker.locator("[data-picker-count]")
            await playwright.expect(count).to_have_text("3 files")
            await picker.get_by_label("Type").select_option("video")
            await playwright.expect(count).to_have_text("Showing 1 of 3 files")
            await playwright.expect(items.filter(has_text="crew-install.mp4")).to_be_visible()
            await picker.get_by_label("Type").select_option("")
            await picker.get_by_label("Search").fill("pergola")
            await playwright.expect(count).to_have_text("Showing 1 of 3 files")
            await picker.get_by_label("Search").fill("")
            await picker.get_by_label("Source").select_option("ai_generated")
            await playwright.expect(items.filter(has_text="concept.jpg")).to_be_visible()
            await picker.get_by_label("Source").select_option("")
            await picker.get_by_label("Sort").select_option("oldest")
            assert "crew-install.mp4" in await items.first.inner_text()
            await items.filter(has_text="crew-install.mp4").locator("input").check()
            await playwright.expect(picker.locator("[data-picker-chosen]")).to_have_text("1 chosen")
            await picker.get_by_label("Chosen only").check()
            await playwright.expect(count).to_have_text("Showing 1 of 3 files")
            await page.screenshot(path=str(capture / "idea-library-picker.png"), full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.screenshot(path=str(capture / "idea-library-picker-mobile.png"), full_page=True)

            await page.set_viewport_size({"width": 1440, "height": 1000})
            # A real project plan: the planned clips, then every other file this post can use.
            await page.goto("http://studio.test/admin/create")
            await page.get_by_label("Your idea").fill("Installation process of our partition walls")
            await page.get_by_label("Real project (your own media)").check()
            await page.get_by_role("button", name="Plan this idea").click()
            await playwright.expect(page.get_by_text("Adjust anything before editing")).to_be_visible()
            extra = page.locator("#real-library")
            await extra.locator("summary").click()
            await playwright.expect(extra.locator("li").filter(has_text="concept.jpg")).to_have_count(0)
            await page.screenshot(path=str(capture / "real-plan-picker.png"), full_page=True)
            await page.goto("http://studio.test/admin/assets")
            filters = page.get_by_role("search", name="Filter the library")
            await filters.get_by_label("Type").select_option("video")
            await page.wait_for_url("**/admin/assets?*type=video*")
            await playwright.expect(page.locator(".library-table tbody tr")).to_have_count(1)
            await page.screenshot(path=str(capture / "asset-library-filtered.png"), full_page=True)
            await page.goto("http://studio.test/admin/assets?source=ai_generated")
            await page.get_by_label("Tick all on this page").check()
            await page.get_by_role("button", name="Delete ticked files").click()
            await playwright.expect(page.get_by_text("Deleted 1 file.")).to_be_visible()
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.screenshot(path=str(capture / "asset-library-mobile.png"), full_page=True)
            assert not errors
            await browser.close()

    with TestClient(create_app(settings), base_url="http://studio.test") as client:
        asyncio.run(exercise(client))
    db.expire_all()
    assert db.get(Asset, video.id) is not None
    assert not [a for a in db.query(Asset).all() if a.source == "ai_generated"]
