"""A definitive Runway music timeout can recover once through Seed Audio, retaining paid visuals."""

import copy
import hashlib

import numpy as np
import pytest
from fastapi.testclient import TestClient
from test_music_retry import MusicProvider, clock, music_job  # noqa: F401
from test_video_rules import tone

from social.app import create_app
from social.media_studio import process_jobs
from social.models import Asset, Control, Post
from social.recovery import auto_retry, retry_option, retry_step, runway_failure
from social.renditions import audio_samples
from social.runway import job_settings
from social.storage import storage


class TimeoutProvider(MusicProvider):
    def task(self, task_id):
        task = super().task(task_id)
        if task["status"] == "FAILED":
            task["failure"] = "Music generation timed out after 80s"
        return task

    def download(self, url):
        return tone(2)


def test_timeout_switch_survives_reload_and_fits_short_audio_through_logo(
    db, settings, music_job, monkeypatch, clock  # noqa: F811
):
    provider = TimeoutProvider(recover=True)
    clips = copy.deepcopy(music_job.brief["video_segments"])
    history, sources = list(music_job.task_history), list(music_job.source_asset_ids)
    process_jobs(db, settings, provider)
    assert music_job.state == "queued" and not music_job.task_id and music_job.submitted_at is None
    assert not provider.submitted and "runway_retry_at" not in music_job.brief
    switch = music_job.brief["audio_fallback"]
    assert switch["from_model"] == "eleven_music_v2_5" and switch["to_model"] == "seed_audio"
    assert switch["reason"] == "generation_timeout" and switch["automatic"]
    assert switch["failed_task_id"] == "original-music"
    assert "timed out after 80 seconds" in music_job.brief["failed_tasks"][-1]["error"]
    db.expire_all()
    process_jobs(db, settings, provider)
    assert music_job.state == "running"
    assert provider.submitted == [{
        "model": "seed_audio", "promptText": "Calm instrumental piano.", "outputFormat": "mp3",
    }]
    process_jobs(db, settings, provider)
    assert music_job.state == "rendering", music_job.error
    assert music_job.brief["video_segments"] == clips and music_job.task_history[:-1] == history
    assert music_job.source_asset_ids[:-1] == sources
    audio = db.get(Asset, music_job.source_asset_ids[-1])
    original = storage(settings).read(audio.storage_key)
    assert original == tone(2) and audio.generator == "runway/seed_audio"
    assert audio.prompt == provider.submitted[0]["promptText"] and audio.tags["audio_fallback"] == switch
    assert music_job.task_history[-1]["model"] == "seed_audio"

    # Only visual summarization is stubbed; audio decoding, fitting, muxing and asset provenance are real.
    monkeypatch.setattr("social.visual_context.ensure_visual_summary", lambda *args: None)
    process_jobs(db, settings, provider)
    assert music_job.state == "complete", music_job.error
    result = db.get(Asset, music_job.output_asset_id)
    fitted = result.tags["soundtrack_fit"]
    assert fitted["asset_id"] == audio.id and fitted["checksum"] == hashlib.sha256(original).hexdigest()
    assert fitted["looped"] and fitted["seconds"] == 12 and fitted["source_seconds"] == 2
    assert result.tags["audio_fallback"] == switch and result.tags["end_card"]["seconds"] == 3
    samples = audio_samples(storage(settings).read(result.storage_key), duration=12)
    assert abs(result.media["duration"] - 12) < 0.2
    for second in range(12):
        assert np.sqrt(np.mean(samples[:, second * 48000:(second + 1) * 48000] ** 2)) > 0.01
    assert storage(settings).read(audio.storage_key) == original
    post = db.get(Post, music_job.post_id)
    assert post.status == "draft" and post.human_review_required and not post.approved_digest
    assert len(provider.submitted) == 1


def test_legacy_failed_music_manual_switch_uses_edited_prompt_and_rejects_double_submit(
    db, settings, music_job  # noqa: F811
):
    settings.media_auto_retries = 0
    process_jobs(db, settings, MusicProvider())
    # Jobs from the older build did not pin their audio models.
    music_job.brief = {k: v for k, v in music_job.brief.items() if k not in {"runway_music_model", "runway_audio_model"}}
    db.commit()
    with TestClient(create_app(settings)) as client:
        token = client.get("/api/session").json()["csrf"]
        page = client.get("/admin/media-queue").text
        assert 'name="audio_model" value="seed_audio"' in page and "Try Seed Audio through Runway" in page
        data = {"csrf": token, "audio_model": "seed_audio", "prompt": "Gentle acoustic guitar music."}
        response = client.post(f"/forms/media-jobs/{music_job.id}/resubmit", data=data)
        assert response.status_code == 200 and "Music model switched to Seed Audio" in response.text
        repeated = client.post(f"/forms/media-jobs/{music_job.id}/resubmit", data=data)
        assert repeated.status_code == 400
    db.refresh(music_job)
    assert music_job.brief["audio_prompt"] == data["prompt"]
    assert not music_job.brief["audio_fallback"]["automatic"]
    assert "runway_retry_at" not in music_job.brief
    assert job_settings(settings, music_job.brief).runway_audio_model == "seed_audio"
    provider = MusicProvider()
    process_jobs(db, settings, provider)
    assert provider.submitted[0]["model"] == "seed_audio" and provider.submitted[0]["promptText"] == data["prompt"]


@pytest.mark.parametrize("state,code", [
    ("running", "THIRD_PARTY.RATE_LIMITED"), ("uncertain", "THIRD_PARTY.RATE_LIMITED"),
    ("failed", "SAFETY.INPUT.TEXT"), ("failed", "INTERNAL.BAD_OUTPUT.CODE01"),
])
def test_model_switch_requires_known_terminal_music_failure(db, settings, music_job, state, code):  # noqa: F811
    music_job.state = state
    music_job.error = runway_failure(music_job, {"status": "FAILED", "failureCode": code})
    db.commit()
    with pytest.raises(ValueError):
        retry_step(db, music_job, "", audio_model="seed_audio")
    assert not music_job.brief.get("audio_fallback") and music_job.task_id == "original-music"


def test_switch_does_not_override_uploaded_audio_or_accept_arbitrary_models(db, settings, music_job):  # noqa: F811
    settings.media_auto_retries = 0
    process_jobs(db, settings, TimeoutProvider())
    with pytest.raises(ValueError):
        retry_step(db, music_job, "", audio_model="unapproved-model")
    music_job.brief = music_job.brief | {"audio_reference_ids": ["user-audio"]}
    db.commit()
    assert "alternate_audio_model" not in retry_option(music_job)
    with pytest.raises(ValueError):
        retry_step(db, music_job, "", audio_model="seed_audio")


def test_timeout_details_are_recognized_without_echoing_arbitrary_provider_text(music_job):  # noqa: F811
    task = {"status": "FAILED", "failureCode": "THIRD_PARTY.RATE_LIMITED"}
    assert "timed out after 80 seconds" in runway_failure(music_job, task | {"failure": "Music generation timed out after 80s"})
    message = runway_failure(music_job, task | {"failure": "Music generation timed out after 80s https://secret.test/token"})
    assert "secret.test" not in message and "timed out" not in message
    cancelled = runway_failure(music_job, task | {"status": "CANCELED", "failure": "Music generation timed out after 80s"})
    assert "cancelled" in cancelled and "timed out" not in cancelled


def test_timeout_auto_switch_can_be_disabled_and_seed_failure_stops(db, settings, music_job):  # noqa: F811
    settings.media_auto_retries = 0
    provider = TimeoutProvider()
    process_jobs(db, settings, provider)
    assert music_job.state == "failed" and not music_job.brief.get("audio_fallback")
    settings.media_auto_retries = 3
    assert auto_retry(db, settings, music_job)
    process_jobs(db, settings, provider)
    process_jobs(db, settings, provider)
    assert music_job.state == "failed" and len(provider.submitted) == 1
    assert "Automatic music recovery stopped after the Seed Audio attempt" in music_job.error
    assert not auto_retry(db, settings, music_job)
    assert "alternate_audio_model" not in retry_option(music_job)


def test_seed_fallback_respects_pause_before_submission(db, settings, music_job):  # noqa: F811
    provider = TimeoutProvider()
    process_jobs(db, settings, provider)
    control = db.get(Control, 1)
    control.killed = True
    db.commit()
    process_jobs(db, settings, provider)
    assert music_job.state == "queued" and not provider.submitted
    control.killed = False
    db.commit()
    process_jobs(db, settings, provider)
    assert music_job.state == "running" and provider.submitted[0]["model"] == "seed_audio"


def test_alternate_model_button_submits_selection_in_browser(db, settings, music_job):  # noqa: F811
    import asyncio
    import os

    from test_studio_ui import serve_studio_request

    playwright = pytest.importorskip("playwright.async_api")
    settings.media_auto_retries = 0
    process_jobs(db, settings, TimeoutProvider())

    async def exercise(client):
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(executable_path=os.environ["BROWSER_BINARY"], headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            page.set_default_timeout(5000)
            page.on("dialog", lambda dialog: asyncio.ensure_future(dialog.accept()))
            await page.route("**/*", lambda route: serve_studio_request(route, client))
            await page.goto("http://studio.test/admin/media-queue")
            card = page.locator(f'[data-job-id="{music_job.id}"]')
            await playwright.expect(card).to_contain_text("timed out after 80 seconds")
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            async with page.expect_navigation():
                await card.get_by_role("button", name="Try Seed Audio through Runway").click()
            await playwright.expect(card).to_contain_text("Music model switched to Seed Audio")
            await playwright.expect(card.locator(".loading-spinner")).to_be_visible()
            await browser.close()

    with TestClient(create_app(settings), base_url="http://studio.test") as client:
        asyncio.run(exercise(client))
    db.refresh(music_job)
    assert music_job.state == "queued" and music_job.brief["soundtrack_model"] == "seed_audio"
    assert len(music_job.brief["failed_tasks"]) == 1
