import asyncio
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_runway_media import brief_for
from test_studio_ui import serve_studio_request

from social.app import create_app
from social.models import Control, MediaJob

pytestmark = pytest.mark.skipif(not os.getenv("BROWSER_BINARY"), reason="BROWSER_BINARY is not configured")


def test_automatic_posts_switch_changes_state_and_survives_reload(db, settings, tmp_path):
    playwright = pytest.importorskip("playwright.async_api")

    async def exercise(client):
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(executable_path=os.environ["BROWSER_BINARY"], headless=True)
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            async def serve(route):
                request = route.request
                client.cookies.clear()
                response = client.request(request.method, request.url, content=request.post_data_buffer,
                                          headers=await request.all_headers(), follow_redirects=True)
                await route.fulfill(status=response.status_code, body=response.content, headers={
                    key: value for key, value in response.headers.items()
                    if key not in {"content-length", "content-encoding", "transfer-encoding"}
                })

            await page.route("http://studio.test/**", serve)
            await page.goto("http://studio.test/")
            control = page.locator("#automation-controls")
            switch = control.get_by_role("switch")
            await playwright.expect(switch).to_have_attribute("aria-checked", "true")
            async with page.expect_navigation():
                await switch.click()
            await playwright.expect(switch).to_have_attribute("aria-checked", "false")
            await playwright.expect(switch).to_have_text("Enable automatic posts")
            await playwright.expect(control).to_contain_text("Already scheduled posts can still publish")
            await page.goto("http://studio.test/")
            await playwright.expect(switch).to_have_attribute("aria-checked", "false")
            await control.screenshot(path=str(tmp_path / "automatic-posts-off.png"))
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            async with page.expect_navigation():
                await switch.click()
            await playwright.expect(switch).to_have_attribute("aria-checked", "true")
            await browser.close()

    with TestClient(create_app(settings), base_url="http://studio.test") as client:
        asyncio.run(exercise(client))
    db.refresh(db.get(Control, 1))
    assert db.get(Control, 1).automation_enabled is True
    assert not db.get(Control, 1).killed


@pytest.mark.parametrize("button,workflow,state", [
    ("Use for next automatic post", "next", "planned"),
    ("Create now", "immediate", "queued"),
])
def test_admin_idea_buttons_preserve_selected_workflow(db, settings, tmp_path, button, workflow, state):
    playwright = pytest.importorskip("playwright.async_api")
    brief = brief_for(db, settings)
    db.get(Control, 1).automation_enabled = False
    db.commit()

    async def exercise(client):
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(executable_path=os.environ["BROWSER_BINARY"], headless=True)
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            page.set_default_timeout(5000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.route("**/*", lambda route: serve_studio_request(route, client))
            await page.goto("http://studio.test/")
            form = page.locator("#next-post-idea")
            await form.get_by_label("Your idea", exact=True).fill(brief.prompt)
            await form.get_by_label("Post name", exact=True).fill(brief.campaign)
            await form.locator('select[name="facts_id"]').select_option(brief.facts_id)
            await form.get_by_label("Usage rights", exact=True).select_option(brief.rights)
            await form.screenshot(path=str(tmp_path / "admin-idea-desktop.png"))
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await form.screenshot(path=str(tmp_path / "admin-idea-mobile.png"))
            async with page.expect_navigation():
                await form.get_by_role("button", name=button, exact=True).click()
            await playwright.expect(page.locator("#media-queue .status")).to_have_text(state)
            assert not errors
            await browser.close()

    with TestClient(create_app(settings), base_url="http://studio.test") as client:
        asyncio.run(exercise(client))
    job = db.scalar(select(MediaJob))
    assert job.brief["workflow"] == workflow
    assert job.brief["prompt"] == brief.prompt
    assert job.state == state
    assert len(db.scalars(select(MediaJob)).all()) == 1
