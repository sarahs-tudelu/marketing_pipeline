import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from social.app import create_app

pytestmark = pytest.mark.skipif(not os.getenv("BROWSER_BINARY"), reason="BROWSER_BINARY is not configured")


def test_edit_permissions_save_history_and_mobile_layout(db, settings, assets, tmp_path):
    playwright = pytest.importorskip("playwright.async_api")
    asset = assets[0]
    asset.rights = "not_public"
    db.commit()
    capture = Path(os.getenv("UI_CAPTURE_DIR", str(tmp_path)))
    capture.mkdir(parents=True, exist_ok=True)

    async def exercise(client):
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(
                executable_path=os.environ["BROWSER_BINARY"], headless=True
            )
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            page.set_default_timeout(5000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            async def serve(route):
                request = route.request
                assert urlsplit(request.url).netloc == "studio.test"
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
                    headers={
                        k: v
                        for k, v in response.headers.items()
                        if k not in {"content-length", "content-encoding", "transfer-encoding"}
                    },
                    body=response.content,
                )

            await page.route("**/*", serve)
            await page.goto("http://studio.test/admin/assets")
            await page.get_by_role("link", name="Edit details", exact=True).click()
            await playwright.expect(
                page.get_by_role("heading", name="Edit media details", exact=True)
            ).to_be_visible()
            await playwright.expect(page.get_by_label("Usage rights", exact=True)).to_have_value("not_public")
            await page.get_by_label("Usage rights", exact=True).select_option("public")
            await page.get_by_label("Owner (optional)", exact=True).fill("Tudelu")
            await page.get_by_label("File name", exact=True).fill("Showroom.jpg")
            await page.get_by_role("button", name="Save changes", exact=True).click()
            await playwright.expect(page.get_by_text("Media details saved.", exact=True)).to_be_visible()
            await playwright.expect(page.get_by_label("Usage rights", exact=True)).to_have_value("public")
            await page.get_by_text("local-developer", exact=False).click()
            await page.screenshot(path=str(capture / "asset-edit-desktop.png"), full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.screenshot(path=str(capture / "asset-edit-mobile.png"), full_page=True)
            await page.get_by_role("link", name="Back to asset library").click()
            await playwright.expect(page.get_by_role("link", name="View Showroom.jpg", exact=True)).to_be_visible()
            assert not errors
            await browser.close()

    with TestClient(create_app(settings), base_url="http://studio.test") as client:
        asyncio.run(exercise(client))
    db.expire_all()
    assert asset.rights == "public" and asset.owner == "Tudelu" and asset.original_name == "Showroom.jpg"
