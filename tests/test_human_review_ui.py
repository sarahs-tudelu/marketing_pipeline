import asyncio
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from social.app import create_app
from social.gate import run_gate

pytestmark = pytest.mark.skipif(not os.getenv('BROWSER_BINARY'), reason='BROWSER_BINARY is not configured')


def test_review_workspace_preview_approval_stop_and_restart(db, settings, post, tmp_path):
    playwright = pytest.importorskip('playwright.async_api')
    post.human_review_required = True
    db.commit()
    run_gate(db, settings, post)

    async def exercise(client):
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(executable_path=os.environ['BROWSER_BINARY'], headless=True)
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.set_default_timeout(10000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))

            async def serve(route):
                request = route.request
                response = client.request(request.method, request.url, content=request.post_data_buffer,
                                          headers=await request.all_headers(), follow_redirects=False)
                await route.fulfill(status=response.status_code, body=response.content,
                                    headers={k: v for k, v in response.headers.items() if k not in {'content-length', 'content-encoding', 'transfer-encoding'}})

            await page.route('**/*', serve)
            await page.goto(f'http://studio.test/posts/{post.id}')
            media = page.locator('.review-media')
            caption = page.locator('.review-caption')
            await playwright.expect(media.locator('img')).to_be_visible()
            assert await media.locator('img').evaluate('image => image.complete && image.naturalWidth > 0')
            mb, cb = await media.bounding_box(), await caption.bounding_box()
            assert mb['x'] + mb['width'] <= cb['x'] and abs(mb['y'] - cb['y']) < 2
            capture = Path(os.getenv('UI_CAPTURE_DIR', str(tmp_path)))
            capture.mkdir(exist_ok=True, parents=True)
            await page.screenshot(path=str(capture / 'human-review-desktop.png'), full_page=True)
            await page.set_viewport_size({'width': 390, 'height': 844})
            assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            mb, cb = await media.bounding_box(), await caption.bounding_box()
            assert cb['y'] > mb['y']
            await page.screenshot(path=str(capture / 'human-review-mobile.png'), full_page=True)
            await page.get_by_role('button', name='Approve & schedule').click()
            await playwright.expect(page.get_by_role('button', name='Stop scheduled post')).to_be_visible()
            await page.get_by_role('button', name='Stop scheduled post').click()
            await playwright.expect(page.get_by_text('Ready for your review', exact=True)).to_be_visible()
            await page.get_by_label('What should change in the caption?').fill('Make the opening warmer')
            await page.get_by_role('button', name='Revise caption for review').click()
            await playwright.expect(page.get_by_role('button', name='Approve & schedule')).to_be_visible()
            page.on('dialog', lambda dialog: dialog.accept())
            await page.get_by_role('button', name='Delete & try again').click()
            await playwright.expect(page.locator('textarea[name=prompt]')).to_have_value(post.campaign)
            assert not errors
            await browser.close()

    with TestClient(create_app(settings), base_url='http://studio.test') as client:
        asyncio.run(exercise(client))
