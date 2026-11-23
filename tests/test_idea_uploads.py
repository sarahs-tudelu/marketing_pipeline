"""Photos, videos and audio added on the idea page: saved to the library and used for the post."""

import io

import av
import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select
from test_audio_review import audio_bytes
from test_idea_plans import facts, logo  # noqa: F401

from social.app import create_app
from social.media_studio import process_jobs
from social.models import Asset, MediaJob, Post
from social.seed import seed_media
from social.storage import storage
from social.worker import advance_queue

IDEA = "Our new pergola on the terrace, introductory post. End with the logo."


def jpeg(width=1600, height=1200):
    stream = io.BytesIO()
    Image.new("RGB", (width, height), "#7d9d89").save(stream, format="JPEG")
    return stream.getvalue()


def plan(client, facts, files, **fields):  # noqa: F811
    data = {
        "csrf": client.get("/api/session").json()["csrf"],
        "prompt": IDEA,
        "format": "auto",
        "kind": "auto",
        "platforms": ["instagram"],
        "rights": "public",
        "facts_id": facts.id,
    } | fields
    return client.post("/forms/ideas/plan", data=data, files=files)


def create(client, facts, upload_ids, **fields):  # noqa: F811
    data = {
        "csrf": client.get("/api/session").json()["csrf"],
        "prompt": IDEA,
        "format": "auto",
        "kind": "auto",
        "platforms": ["instagram"],
        "rights": "public",
        "facts_id": facts.id,
        "upload_use": "as_is",
        "upload_ids": upload_ids,
        "as_uploaded": "on",
        "campaign": "Terrace pergola",
        "plan_kind": "promotional",
        "mode": "once",
    } | fields
    return client.post("/forms/ideas/create", data=data)


def finish(db, settings):
    for _ in range(4):
        process_jobs(db, settings)
        advance_queue(db, settings)


@pytest.fixture
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Posting your own media must not contact Runway or any other service")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)


def test_photo_is_saved_and_posted_as_it_is(db, settings, facts, offline):  # noqa: F811
    with TestClient(create_app(settings)) as client:
        page = client.get("/admin/create").text
        assert 'enctype="multipart/form-data"' in page and 'name="files"' in page
        # Pinterest takes only real project posts from Drive media, which the idea page can now make.
        assert "Real project only" in page and "Post from library media" in page
        planned = plan(client, facts, [("files", ("terrace.jpg", jpeg(), "image/jpeg"))])
        assert planned.status_code == 200, planned.text
        upload = db.scalar(select(Asset).where(Asset.original_name == "terrace.jpg"))
        assert upload.source == "company" and upload.rights == "public"
        assert upload.tags["uploaded_with_idea"] and upload.tags["product"] == "company"
        page = planned.text
        assert "Your photo, posted as it is, for Instagram." in page
        assert f'name="upload_ids" value="{upload.id}"' in page and 'name="as_uploaded" value="on"' in page
        assert "Nothing is sent to Runway." in page and "Clips, in viewing order" not in page
        assert "can&#39;t carry music, motion or a logo end card" in page
        created = create(client, facts, [upload.id], operation="image")
        assert created.status_code == 200 and str(created.url).endswith("/admin/media-queue")
        assert "Your own photo, posted as it is and sized for Instagram." in created.text
    job = db.scalar(select(MediaJob))
    assert job.brief["as_uploaded"] and job.source_asset_ids == [upload.id]
    finish(db, settings)
    db.refresh(job)
    assert job.state == "complete" and not job.task_history and "prompt_writer" not in job.brief
    post = db.get(Post, job.post_id)
    assert post.status == "awaiting_review" and post.caption and not post.ai_disclosure
    copy = db.get(Asset, post.asset_ids[0])
    # The photo keeps its 4:3 shape, scaled to Instagram's size; the original is unchanged.
    assert (copy.media["width"], copy.media["height"]) == (1350, 1012)
    assert copy.source == "company" and copy.tags["rendition"]["original_asset_id"] == upload.id
    assert storage(settings).read(upload.storage_key) == jpeg()


def test_video_with_own_soundtrack_and_logo(db, settings, facts, logo, offline):  # noqa: F811
    video = seed_media(duration=6)[1]
    with TestClient(create_app(settings)) as client:
        planned = plan(
            client,
            facts,
            [
                ("files", ("walkway.mp4", video, "video/mp4")),
                ("files", ("guitar.wav", audio_bytes(seconds=12), "audio/wav")),
            ],
            platforms=["instagram", "facebook"],
        )
        assert planned.status_code == 200, planned.text
        clip = db.scalar(select(Asset).where(Asset.original_name == "walkway.mp4"))
        sound = db.scalar(select(Asset).where(Asset.original_name == "guitar.wav"))
        page = planned.text
        assert (
            "Your video, posted as it is (9 seconds and a 3-second logo end card), for Instagram and Facebook."
            in page
        )
        assert "guitar.wav replaces the video&#39;s own sound." in page
        assert "Every video ends with a cut to the Tudelu logo (3 seconds, added after your video)." in page
        assert "Sound: guitar.wav replaces the video's own sound." in page and 'name="sound"' not in page
        created = create(
            client,
            facts,
            [clip.id, sound.id],
            operation="video",
            duration="9",
            end_card="on",
            logo_id=logo.id,
            platforms=["instagram", "facebook"],
        )
        assert created.status_code == 200, created.text
    finish(db, settings)
    jobs = db.scalars(select(MediaJob)).all()
    assert [j.state for j in jobs] == ["complete", "complete"], [j.error for j in jobs]
    assert jobs[0].output_asset_id == jobs[1].output_asset_id
    for job in jobs:
        copy = db.get(Asset, db.get(Post, job.post_id).asset_ids[0])
        assert copy.tags["soundtrack_asset_id"] == sound.id and copy.tags["end_card"]["asset_id"] == logo.id
        assert copy.tags["source_asset_ids"] == [clip.id, sound.id, logo.id]
        assert abs(copy.media["duration"] - 9) < 0.2
        with av.open(io.BytesIO(storage(settings).read(copy.storage_key))) as container:
            assert container.streams.audio
        # Both destinations accept the same copy padded for Facebook Reels.
        assert (copy.media["width"], copy.media["height"]) == (720, 1280)


def test_reference_use_starts_new_ai_media_from_the_upload(db, settings, facts, house_logo, offline):  # noqa: F811
    with TestClient(create_app(settings)) as client:
        planned = plan(
            client,
            facts,
            [("files", ("start.jpg", jpeg(), "image/jpeg"))],
            prompt="A 10-second video of a pergola at dusk",
            upload_use="reference",
        )
        assert planned.status_code == 200, planned.text
        upload = db.scalar(select(Asset).where(Asset.original_name == "start.jpg"))
        assert "Clips, in viewing order" in planned.text and 'name="as_uploaded"' not in planned.text
        created = create(
            client,
            facts,
            [upload.id],
            prompt="A 10-second video of a pergola at dusk",
            upload_use="reference",
            as_uploaded="",
            operation="video",
            duration="10",
            shot_setting=["terrace at dusk"],
            shot_system=[""],
        )
        assert created.status_code == 200, created.text
    job = db.scalar(select(MediaJob))
    assert job.brief["reference_ids"] == [upload.id] and not job.brief["as_uploaded"]


def test_files_are_kept_when_the_plan_needs_a_change(db, settings, facts, offline):  # noqa: F811
    with TestClient(create_app(settings)) as client:
        refused = plan(
            client,
            facts,
            [
                ("files", ("one.jpg", jpeg(), "image/jpeg")),
                ("files", ("two.jpg", jpeg(800, 800), "image/jpeg")),
            ],
        )
        assert refused.status_code == 400 and "add one photo or one video" in refused.text
        saved = db.scalars(select(Asset).where(Asset.original_name.in_(["one.jpg", "two.jpg"]))).all()
        assert len(saved) == 2
        for asset in saved:
            assert f'name="upload_ids" value="{asset.id}" checked' in refused.text
        # Unticking one file and planning again doesn't upload anything twice.
        again = plan(client, facts, [], upload_ids=[saved[0].id])
        assert again.status_code == 200 and "Your photo, posted as it is" in again.text
        assert len(db.scalars(select(Asset)).all()) == 2

        unreadable = plan(client, facts, [("files", ("notes.txt", b"not media", "text/plain"))])
        assert unreadable.status_code == 400 and "notes.txt:" in unreadable.text
        assert len(db.scalars(select(Asset)).all()) == 2


def test_upload_on_the_idea_page_in_browser(db, settings, facts, tmp_path):  # noqa: F811
    import asyncio
    import os
    from pathlib import Path

    from test_studio_ui import serve_studio_request

    playwright = pytest.importorskip("playwright.async_api")
    capture = Path(os.getenv("UI_CAPTURE_DIR") or tmp_path)
    capture.mkdir(parents=True, exist_ok=True)

    async def exercise(client):
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(
                executable_path=os.environ["BROWSER_BINARY"], headless=True
            )
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            page.set_default_timeout(8000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.route("**/*", lambda route: serve_studio_request(route, client))
            await page.goto("http://studio.test/admin/create")
            form = page.locator('form[action="/forms/ideas/plan"]')
            await form.get_by_label("Your idea").fill(IDEA)
            await form.get_by_label("Add files").set_input_files(
                [{"name": "terrace.jpg", "mimeType": "image/jpeg", "buffer": jpeg()}]
            )
            await page.locator("#idea-media").scroll_into_view_if_needed()
            await page.screenshot(path=str(capture / "idea-upload-desktop.png"))
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.locator("#idea-media").scroll_into_view_if_needed()
            await page.screenshot(path=str(capture / "idea-upload-mobile.png"))
            await page.set_viewport_size({"width": 1440, "height": 1000})
            async with page.expect_navigation():
                await form.get_by_role("button", name="Plan this idea").click()
            await playwright.expect(page.locator(".plan-summary")).to_have_text(
                "Your photo, posted as it is, for Instagram."
            )
            section = page.locator("#uploaded-plan")
            await playwright.expect(section).to_contain_text("terrace.jpg · photo")
            await page.screenshot(path=str(capture / "idea-upload-plan-desktop.png"), full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.screenshot(path=str(capture / "idea-upload-plan-mobile.png"), full_page=True)
            await page.set_viewport_size({"width": 1440, "height": 1000})
            async with page.expect_navigation():
                await page.get_by_role("button", name="Create now").click()
            await playwright.expect(page.locator("#media-queue")).to_contain_text(
                "Your own photo, posted as it is and sized for Instagram."
            )
            assert not errors
            await browser.close()

    with TestClient(create_app(settings), base_url="http://studio.test") as client:
        asyncio.run(exercise(client))
    assert db.scalar(select(MediaJob)).brief["as_uploaded"]
