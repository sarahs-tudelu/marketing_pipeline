"""Optional real-browser checks: set BROWSER_BINARY and install playwright to run."""

import asyncio
import os
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner

from social.app import create_app
from social.connections import OAUTH
from social.facts import load_facts
from social.pipeline import draft
from social.provider_errors import ProviderError

pytestmark = pytest.mark.skipif(not os.getenv("BROWSER_BINARY"), reason="BROWSER_BINARY is not configured")


def test_media_studio_custom_prompt_and_responsive_layout(db, settings, tmp_path):
    playwright = pytest.importorskip("playwright.async_api")

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
            await page.goto("http://studio.test/admin/media-studio")
            await page.get_by_label("Campaign name", exact=True).fill("A custom editorial idea")
            await page.get_by_label("Your prompt", exact=True).fill(
                "A calm architectural study in warm light"
            )
            await page.get_by_label("Usage rights", exact=True).select_option("public")
            await page.get_by_label("Use tudelu.com to choose and describe the system shown").uncheck()
            await page.get_by_label("Content style").select_option("non_promotional")
            await page.screenshot(path=str(tmp_path / "media-studio-desktop.png"), full_page=True)
            await page.get_by_role("button", name="Queue idea / save campaign").click()
            await playwright.expect(
                page.get_by_role("heading", name="A custom editorial idea · Instagram")
            ).to_be_visible()
            jobs = client.get("/api/media-jobs").json()
            assert len(jobs) == 1 and jobs[0]["state"] == "queued"
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.screenshot(path=str(tmp_path / "media-studio-mobile.png"), full_page=True)
            assert not errors
            await browser.close()

    with TestClient(create_app(settings), base_url="http://studio.test") as client:
        asyncio.run(exercise(client))


@pytest.mark.parametrize("platform", ["x", "linkedin", "tiktok"])
@pytest.mark.parametrize("javascript_enabled", [True, False])
def test_connect_opens_provider_authorization_with_same_origin_form_policy(
    db, settings, platform, javascript_enabled
):
    playwright = pytest.importorskip("playwright.async_api")
    selected = settings.model_copy(
        update={
            "app_base_url": "https://studio.test",
            platform + "_client_id": "mock-client-id",
            platform + "_client_secret": settings.token_encryption_key,
        }
    )

    async def exercise(client):
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(
                executable_path=os.environ["BROWSER_BINARY"], headless=True
            )
            context = await browser.new_context(offline=True, java_script_enabled=javascript_enabled)
            page = await context.new_page()
            errors, provider_requests, submissions = [], [], []
            page.on(
                "console", lambda message: errors.append(message.text) if message.type == "error" else None
            )

            async def serve(route):
                request = route.request
                url = urlsplit(request.url)
                if url.netloc != "studio.test":
                    assert request.url.split("?", 1)[0] == OAUTH[platform]["authorize"]
                    assert request.method == "GET"
                    provider_requests.append(request)
                    await route.fulfill(content_type="text/html", body="<h1>Provider authorization</h1>")
                    return
                if request.method == "POST":
                    submissions.append(request)
                client.cookies.clear()
                response = client.request(
                    request.method,
                    request.url,
                    content=request.post_data_buffer,
                    headers=await request.all_headers(),
                    follow_redirects=False,
                )
                headers = {
                    key: value
                    for key, value in response.headers.items()
                    if key not in {"content-encoding", "content-length", "transfer-encoding"}
                }
                await route.fulfill(status=response.status_code, headers=headers, body=response.content)

            await page.route("**/*", serve)
            response = await page.goto("https://studio.test/admin/connections")
            assert "form-action 'self';" in response.headers["content-security-policy"]
            await page.locator(f'form[action="/oauth/{platform}/connect"] button').click()
            if not javascript_enabled:
                await page.locator("#oauth-continue").click()
            try:
                await playwright.expect(
                    page.get_by_role("heading", name="Provider authorization")
                ).to_be_visible(timeout=5000)
            except AssertionError as exc:
                raise AssertionError(f"Provider handoff failed. Browser errors: {errors}") from exc
            assert len(submissions) == len(provider_requests) == 1
            query = parse_qs(urlsplit(provider_requests[0].url).query)
            assert query["client_key" if platform == "tiktok" else "client_id"] == ["mock-client-id"]
            assert query["redirect_uri"] == [f"https://studio.test/oauth/{platform}/callback"]
            assert not errors
            await browser.close()

    with TestClient(create_app(selected), base_url="https://studio.test") as client:
        asyncio.run(exercise(client))


@pytest.mark.parametrize("kind", ["caption", "image", "checks"])
def test_generation_progress_and_completion_in_browser(db, settings, assets, monkeypatch, tmp_path, kind):
    playwright = pytest.importorskip("playwright.async_api")
    facts = load_facts(db, settings.facts_path)
    post = draft(db, "linkedin", "Browser preview", [a.id for a in assets], facts.id, product="demo")
    if kind == "checks":
        from social.pipeline import generate
        generate(db, settings, post)
    route_path = "/admin/assets" if kind == "image" else f"/posts/{post.id}"
    action = f"/forms/posts/{post.id}/{'check' if kind == 'checks' else 'generate'}" if kind != "image" else "/forms/generate-asset"
    label = {"caption": "Generate caption", "checks": "Run automated checks", "image": "Generate image"}[kind]
    status = {"caption": "Writing caption", "checks": "Checking post", "image": "Generating media"}[kind]
    selected = (
        settings
        if kind != "image"
        else settings.model_copy(update={"ai_mode": "live", "image_provider": "openai"})
    )

    def image_failure(*args, **kwargs):
        raise ProviderError(
            "Preview provider unavailable; try again.", provider="OpenAI", category="transient"
        )

    monkeypatch.setattr("social.providers.Providers.generate_asset", image_failure)

    async def exercise(client):
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(
                executable_path=os.environ["BROWSER_BINARY"], headless=True
            )
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            page.set_default_timeout(5000)
            page.set_default_navigation_timeout(5000)
            requests, errors = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))

            async def serve(route):
                request = route.request
                assert urlsplit(request.url).netloc == "studio.test"
                if request.method == "POST":
                    requests.append(request)
                response = client.request(
                    request.method,
                    request.url,
                    content=request.post_data_buffer,
                    headers=request.headers,
                    follow_redirects=True,
                )
                headers = {
                    key: value
                    for key, value in response.headers.items()
                    if key not in {"content-encoding", "content-length", "transfer-encoding"}
                }
                await route.fulfill(status=response.status_code, headers=headers, body=response.content)

            await page.route("**/*", serve)
            await page.goto("http://studio.test" + route_path)
            form = page.locator(f'form[action="{action}"]')
            if kind == "image":
                await form.locator('[name="prompt"]').fill("A preview image")
                await form.locator('[name="rights"]').select_option("not_public")
            handle = await form.element_handle()
            # Pause native submission after session refresh so the pending UI is inspectable.
            await page.evaluate("""() => {
                window.capturedSubmissions = [];
                window.nativeSubmit = HTMLFormElement.prototype.submit;
                HTMLFormElement.prototype.submit = function () {
                    window.capturedSubmissions.push(Object.fromEntries(new FormData(this)));
                    this.dataset.captured = 'true';
                };
            }""")
            await form.get_by_role("button", name=label).click()
            await playwright.expect(form).to_have_attribute("data-captured", "true")
            assert await handle.get_attribute("aria-busy") == "true"
            assert status in await handle.eval_on_selector('[role="status"]', "node => node.innerText")
            assert await handle.eval_on_selector("button", "node => node.disabled")
            assert not await handle.evaluate(
                "form => form.dispatchEvent(new SubmitEvent('submit', {bubbles:true, cancelable:true}))"
            )
            assert await page.evaluate("capturedSubmissions.length") == 1
            spinner = await handle.query_selector(".loading-spinner")
            assert await spinner.evaluate("node => getComputedStyle(node).animationName") == "loading-spin"
            await page.screenshot(path=str(tmp_path / f"{kind}-loading.png"), full_page=True)
            capture_dir = os.getenv("UI_CAPTURE_DIR")
            if capture_dir:
                Path(capture_dir).mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(Path(capture_dir) / f"{kind}-loading.png"), full_page=True)
            await page.emulate_media(reduced_motion="reduce")
            assert await spinner.evaluate("node => getComputedStyle(node).animationName") == "none"
            async with page.expect_navigation():
                await handle.evaluate("form => window.nativeSubmit.call(form)")
            assert len(requests) == 1
            assert parse_qs(requests[0].post_data)["csrf"]
            assert not errors
            assert await page.locator(".loading-state:visible").count() == 0
            if kind == "caption":
                assert await page.get_by_role("button", name="Run automated checks").is_enabled()
            elif kind == "image":
                assert await page.get_by_role("button", name=label).is_enabled()
                assert "Preview provider unavailable" in await page.get_by_role("alert").inner_text()
                assert await form.locator('[name="prompt"]').input_value() == "A preview image"
            await browser.close()

    with TestClient(create_app(selected), base_url="http://studio.test") as client:
        asyncio.run(exercise(client))


def test_delete_confirmation_and_cached_page_restore_in_browser(db, settings, assets):
    playwright = pytest.importorskip("playwright.async_api")
    facts = load_facts(db, settings.facts_path)
    post = draft(db, "linkedin", "Browser delete", [a.id for a in assets], facts.id, product="demo")

    async def exercise(client):
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(
                executable_path=os.environ["BROWSER_BINARY"], headless=True
            )
            page = await browser.new_page()
            page.set_default_timeout(5000)
            page.set_default_navigation_timeout(5000)

            async def serve(route):
                response = client.get(route.request.url)
                await route.fulfill(
                    status=response.status_code,
                    content_type=response.headers["content-type"],
                    body=response.content,
                )

            await page.route("http://studio.test/**", serve)
            await page.goto(f"http://studio.test/posts/{post.id}")
            page.once("dialog", lambda dialog: dialog.dismiss())
            button = page.get_by_role("button", name="Delete draft")
            await button.click()
            assert await button.is_enabled()
            assert await page.locator(".loading-state:visible").count() == 0
            # Stop native navigation after the enhancer runs, then emulate a cached-page return.
            await page.evaluate("HTMLFormElement.prototype.submit = function () {}")
            generate = page.get_by_role("button", name="Generate caption")
            await generate.click()
            assert await generate.is_disabled()
            await page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}))")
            assert await generate.is_enabled()
            assert await page.locator('[aria-busy="true"]').count() == 0
            assert await page.locator(".loading-state:visible").count() == 0
            await browser.close()

    with TestClient(create_app(settings), base_url="http://studio.test") as client:
        asyncio.run(exercise(client))


def test_background_post_progress_disables_actions_and_updates_result(db, settings, assets, monkeypatch):
    from social.pipeline import generate

    playwright = pytest.importorskip("playwright.async_api")
    facts = load_facts(db, settings.facts_path)
    post = draft(db, "linkedin", "Background progress", [assets[0].id], facts.id, product="demo")
    busy = True
    monkeypatch.setattr("social.app.post_processing", lambda *args: busy)

    async def exercise(client):
        nonlocal busy
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(executable_path=os.environ["BROWSER_BINARY"], headless=True)
            page = await browser.new_page()
            await page.route("**/*", lambda route: serve_studio_request(route, client))
            await page.goto(f"http://studio.test/posts/{post.id}")
            await playwright.expect(page.get_by_role("button", name="Generate caption")).to_be_disabled()
            await playwright.expect(page.get_by_role("button", name="Delete draft")).to_be_disabled()
            await playwright.expect(page.locator("#post-progress")).to_be_visible()
            generate(db, settings, post)
            busy = False
            await playwright.expect(page.get_by_role("button", name="Run automated checks")).to_be_enabled(timeout=8000)
            await playwright.expect(page.locator("#post-progress")).to_be_hidden()
            assert await page.get_by_role("alert").count() == 0
            await browser.close()

    with TestClient(create_app(settings), base_url="http://studio.test") as client:
        asyncio.run(exercise(client))


def test_media_queue_updates_without_discarding_creative_brief(db, settings):
    from test_runway_media import brief_for

    from social.media_studio import process_jobs, queue_jobs
    from social.worker import advance_queue

    playwright = pytest.importorskip("playwright.async_api")
    job = queue_jobs(db, settings, brief_for(db, settings))[0]
    db.commit()

    async def exercise(client):
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(executable_path=os.environ["BROWSER_BINARY"], headless=True)
            page = await browser.new_page()
            await page.route("**/*", lambda route: serve_studio_request(route, client))
            await page.goto("http://studio.test/admin/media-studio")
            await page.get_by_label("Your prompt", exact=True).fill("Keep my next creative idea")
            await playwright.expect(page.locator("#media-queue .loading-spinner")).to_be_visible()
            for _ in range(3):
                process_jobs(db, settings)
            assert job.state == "complete"
            await playwright.expect(page.locator("#media-queue .status")).to_have_text("complete", timeout=8000)
            await playwright.expect(page.get_by_label("Your prompt", exact=True)).to_have_value("Keep my next creative idea")
            await playwright.expect(page.locator("#media-queue .loading-spinner")).to_be_visible()
            advance_queue(db, settings)
            await playwright.expect(page.locator("#media-queue")).to_contain_text("scheduled", timeout=8000)
            await playwright.expect(page.locator("#media-queue .loading-spinner")).to_have_count(0)
            await playwright.expect(page.get_by_label("Your prompt", exact=True)).to_have_value("Keep my next creative idea")
            await browser.close()

    with TestClient(create_app(settings), base_url="http://studio.test") as client:
        asyncio.run(exercise(client))


def test_long_video_progress_refreshes_between_clips_and_preserves_form(db, settings, assets):
    from test_runway_media import brief_for

    from social.audio_review import observe_audio
    from social.media_studio import queue_jobs
    from social.providers import Providers

    playwright = pytest.importorskip("playwright.async_api")
    job = queue_jobs(db, settings, brief_for(db, settings, operation="video", duration=90))[0]
    job.state = "running"
    assets[0].original_name = "8f98c44dc10ebb7152c7453735800ae76568ca84a153f2c6a8bd91d045f8f591.mp4"
    observe_audio(db, settings, assets[0], Providers(settings))
    db.commit()

    async def exercise(client):
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(executable_path=os.environ["BROWSER_BINARY"], headless=True)
            page = await browser.new_page()
            await page.route("**/*", lambda route: serve_studio_request(route, client))
            await page.goto("http://studio.test/admin/media-studio")
            duration = page.get_by_label("Finished video duration (seconds)")
            await playwright.expect(duration).to_have_attribute("max", "90")
            await duration.fill("90")
            await playwright.expect(page.locator("#media-queue")).to_contain_text("Video clips saved: 0 / 9")
            job.brief = job.brief | {"video_segments": [s | {"output_asset_id": assets[0].id} if s["index"] == 0 else s
                                                     for s in job.brief["video_segments"]]}
            db.commit()
            await playwright.expect(page.locator("#media-queue")).to_contain_text("Video clips saved: 1 / 9", timeout=8000)
            await playwright.expect(duration).to_have_value("90")
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.goto("http://studio.test/admin/assets")
            await page.get_by_text("Audio summary and transcript", exact=True).click()
            await playwright.expect(page.get_by_text("No audio stream.", exact=True)).to_be_visible()
            await browser.close()

    with TestClient(create_app(settings), base_url="http://studio.test") as client:
        asyncio.run(exercise(client))


async def serve_studio_request(route, client):
    request = route.request
    assert urlsplit(request.url).netloc == "studio.test"
    # Use only the browser's cookies; the test client's jar must not hide cookie loss.
    client.cookies.clear()
    response = client.request(
        request.method,
        request.url,
        content=request.post_data_buffer,
        headers=await request.all_headers(),
        follow_redirects=True,
    )
    headers = {
        key: value
        for key, value in response.headers.items()
        if key not in {"content-encoding", "content-length", "transfer-encoding"}
    }
    await route.fulfill(status=response.status_code, headers=headers, body=response.content)
    return response


@pytest.mark.parametrize("session_state", ["missing", "expired", "replaced"])
def test_stale_upload_refreshes_session_before_one_submission(
    db, settings, assets, monkeypatch, session_state
):
    playwright = pytest.importorskip("playwright.async_api")
    media_bytes = (settings.media_dir / assets[0].storage_key).read_bytes()

    async def exercise(client):
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(
                executable_path=os.environ["BROWSER_BINARY"], headless=True
            )
            page = await browser.new_page()
            requests, refreshes = [], []

            async def serve(route):
                response = await serve_studio_request(route, client)
                if urlsplit(route.request.url).path == "/api/session":
                    refreshes.append(response.json()["csrf"])
                if route.request.method == "POST":
                    requests.append((route.request, response.status_code))

            await page.route("**/*", serve)
            await page.goto("http://studio.test/admin/assets")
            form = page.locator('form[action="/forms/assets"]')
            old_token = await form.locator('[name="csrf"]').input_value()
            await form.locator('[name="rights"]').select_option("public")
            await form.locator('[name="owner"]').fill("Company-owned original")
            await form.locator('[name="file"]').set_input_files(
                {"name": "session-recovery.jpg", "mimeType": "image/jpeg", "buffer": media_bytes}
            )
            cookie = next(c for c in await page.context.cookies() if c["name"] == "session")
            if session_state == "missing":
                await page.context.clear_cookies()
                cookie_header = ""
            else:
                if session_state == "expired":
                    signer = TimestampSigner(settings.session_secret.get_secret_value())
                    payload = signer.unsign(cookie["value"])
                    with monkeypatch.context() as patch:
                        patch.setattr(TimestampSigner, "get_timestamp", lambda self: int(time.time()) - 3601)
                        cookie["value"] = signer.sign(payload).decode()
                else:
                    client.cookies.clear()
                    client.get("/api/session")
                    cookie["value"] = client.cookies.get("session")
                await page.context.add_cookies([cookie])
                cookie_header = f"session={cookie['value']}"
            # The unrefreshed token must still be rejected by the server.
            client.cookies.clear()
            rejected = client.post(
                "/forms/control",
                headers={"cookie": cookie_header},
                data={"csrf": old_token, "enabled": "true"},
            )
            assert rejected.status_code == 403
            async with page.expect_navigation():
                await form.get_by_role("button", name="Ingest asset").click()
            assert len(refreshes) == len(requests) == 1
            assert refreshes[0] != old_token
            request, status = requests[0]
            assert status == 200
            assert media_bytes in request.post_data_buffer
            assert refreshes[0].encode() in request.post_data_buffer
            assert b"Company-owned original" in request.post_data_buffer
            assert await page.get_by_role("heading", name="Asset library", exact=True).count() == 1
            assert await page.locator(".session-error").count() == 0
            await browser.close()

    with TestClient(create_app(settings), base_url="http://studio.test") as client:
        asyncio.run(exercise(client))


def test_storage_upload_failure_is_visible_and_form_can_retry(db, settings, assets, monkeypatch, tmp_path):
    from botocore.exceptions import ClientError

    from social.storage import LocalStore

    playwright = pytest.importorskip("playwright.async_api")
    original = LocalStore.put
    data = (settings.media_dir / assets[0].storage_key).read_bytes()

    def failed_put(*args, **kwargs):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "private-token"}}, "PutObject")

    monkeypatch.setattr(LocalStore, "put", failed_put)

    async def exercise(client):
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(executable_path=os.environ["BROWSER_BINARY"], headless=True)
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            page.set_default_timeout(5000)
            await page.route("**/*", lambda route: serve_studio_request(route, client))
            await page.goto("http://studio.test/admin/assets")
            form = page.locator('form[action="/forms/assets"]')
            await form.locator('[name="rights"]').select_option("public")
            await form.locator('[name="owner"]').fill("Company-owned original")
            await form.locator('[name="file"]').set_input_files({
                "name": "original.jpg", "mimeType": "image/jpeg", "buffer": data,
            })
            async with page.expect_navigation():
                await form.get_by_role("button", name="Ingest asset").click()
            await playwright.expect(page.get_by_role("alert")).to_contain_text("bucket write permissions")
            await playwright.expect(form.locator('[name="rights"]')).to_have_value("public")
            await playwright.expect(form.locator('[name="owner"]')).to_have_value("Company-owned original")
            await playwright.expect(page.get_by_role("status")).to_contain_text("select the file again")
            assert "private-token" not in await page.content()
            await page.screenshot(path=str(tmp_path / "upload-error.png"), full_page=True)
            monkeypatch.setattr(LocalStore, "put", original)
            await form.locator('[name="file"]').set_input_files({
                "name": "original.jpg", "mimeType": "image/jpeg", "buffer": data,
            })
            async with page.expect_navigation():
                await form.get_by_role("button", name="Ingest asset").click()
            assert await page.get_by_role("alert").count() == 0
            assert await page.get_by_role("heading", name="Asset library", exact=True).count() == 1
            await browser.close()

    with TestClient(create_app(settings), base_url="http://studio.test") as client:
        asyncio.run(exercise(client))


@pytest.mark.parametrize("failure", ["network", "unauthorized", "invalid_token"])
def test_session_refresh_failure_keeps_entries_and_allows_retry(db, settings, assets, failure):
    playwright = pytest.importorskip("playwright.async_api")

    async def exercise(client):
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(
                executable_path=os.environ["BROWSER_BINARY"], headless=True
            )
            page = await browser.new_page()
            requests = []
            fail_refresh = True

            async def serve(route):
                assert urlsplit(route.request.url).netloc == "studio.test"
                if fail_refresh and urlsplit(route.request.url).path == "/api/session":
                    if failure == "network":
                        await route.abort()
                    else:
                        await route.fulfill(
                            status=401 if failure == "unauthorized" else 200,
                            content_type="application/json",
                            body='{"csrf": null}',
                        )
                    return
                if route.request.method == "POST":
                    requests.append(route.request)
                await serve_studio_request(route, client)

            await page.route("**/*", serve)
            await page.goto("http://studio.test/admin/compose")
            form = page.locator('form[action="/forms/posts"]')
            await form.locator('[name="campaign"]').fill("Preserved campaign")
            await form.locator('[name="asset_ids"]').check()
            button = form.get_by_role("button", name="Create draft")
            await button.click()
            await playwright.expect(form.get_by_role("alert")).to_contain_text("Nothing was submitted")
            assert not requests
            assert await button.is_enabled()
            assert await form.locator('[name="campaign"]').input_value() == "Preserved campaign"
            assert await form.locator('[name="asset_ids"]').is_checked()
            assert await form.get_attribute("aria-busy") is None
            fail_refresh = False
            async with page.expect_navigation():
                await button.click()
            assert len(requests) == 1
            assert await page.get_by_role("heading", name="Preserved campaign", exact=True).count() == 1
            await browser.close()

    with TestClient(create_app(settings), base_url="http://studio.test") as client:
        asyncio.run(exercise(client))
