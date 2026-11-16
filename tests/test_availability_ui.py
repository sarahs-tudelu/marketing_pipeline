import asyncio
import os
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from test_availability import mock_platform
from test_availability import published as published

from social.app import create_app
from social.worker import advance_queue

pytestmark = pytest.mark.skipif(not os.getenv("BROWSER_BINARY"), reason="BROWSER_BINARY is not configured")


def test_availability_and_repost_review_in_browser(db, settings, published, monkeypatch, tmp_path):
    playwright = pytest.importorskip("playwright.async_api")
    mock_platform(monkeypatch, lambda _: httpx.Response(404, json={}))

    async def exercise(client):
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(
                executable_path=os.environ["BROWSER_BINARY"], headless=True
            )
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            page.set_default_timeout(10000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            async def serve(route):
                request = route.request
                client.cookies.clear()
                response = client.request(
                    request.method,
                    request.url,
                    content=request.post_data_buffer,
                    headers=await request.all_headers(),
                    follow_redirects=False,
                )
                await route.fulfill(
                    status=response.status_code,
                    body=response.content,
                    headers={
                        k: v
                        for k, v in response.headers.items()
                        if k not in {"content-length", "content-encoding", "transfer-encoding"}
                    },
                )

            await page.route("**/*", serve)
            await page.goto(f"http://studio.test/posts/{published.id}")
            panel = page.locator("#post-availability")
            await playwright.expect(panel).to_contain_text("Not checked yet")
            await panel.get_by_role("button", name="Check availability now").click()
            await playwright.expect(panel).to_contain_text("Unavailable on platform")
            await playwright.expect(panel).to_contain_text("Last checked:")
            await page.get_by_label(
                "I checked the platform and confirmed the original post is no longer available."
            ).check()
            capture = Path(os.getenv("UI_CAPTURE_DIR", str(tmp_path)))
            capture.mkdir(exist_ok=True, parents=True)
            await page.screenshot(path=str(capture / "availability-desktop.png"), full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.screenshot(path=str(capture / "availability-mobile.png"), full_page=True)
            await page.get_by_role("button", name="Prepare repost for review").click()
            await playwright.expect(page.get_by_text("Repost draft", exact=True)).to_be_visible()
            advance_queue(db, settings)
            await playwright.expect(page.get_by_role("button", name="Approve & schedule")).to_be_visible()
            await page.goto(f"http://studio.test/posts/{published.id}")
            await playwright.expect(page.get_by_role("link", name="Open repost", exact=True)).to_be_visible()
            assert not errors
            await browser.close()

    with TestClient(create_app(settings), base_url="http://studio.test") as client:
        asyncio.run(exercise(client))
