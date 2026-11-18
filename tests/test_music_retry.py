"""Music provider backoff keeps paid visuals and never repeats ambiguous submissions."""

import copy
import io
import wave
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_runway_media import brief_for

from social.app import create_app
from social.db import now, uid
from social.media_studio import process_jobs, queue_jobs
from social.models import Account, Control, MediaJob
from social.recovery import auto_retry, retry_option, retry_step, runway_failure
from social.seed import seed_media
from social.storage import ingest


@pytest.fixture
def music_job(db, settings, house_logo):
    job = queue_jobs(db, settings, brief_for(db, settings, operation="video", duration=12))[0]
    clip = ingest(db, settings, seed_media(duration=6)[1], "saved-clip.mp4", rights="public")
    job.brief = job.brief | {
        "execution_mode": "live", "active_generation": "audio", "soundtrack_model": "music",
        "audio_prompt": "Calm instrumental piano.",
        "video_segments": [s | {"output_asset_id": clip.id} for s in job.brief["video_segments"]],
    }
    job.source_asset_ids = [clip.id]
    job.task_history = [{"stage": "visual", "task_id": "saved-video", "output_asset_id": clip.id}]
    job.stage, job.state, job.task_id, job.submitted_at = "audio", "running", "original-music", now()
    settings.ai_mode, settings.runway_api_key = "live", SecretStr("test")
    db.commit()
    return job


@pytest.fixture
def clock(monkeypatch):
    at = [now()]
    monkeypatch.setattr("social.recovery.now", lambda: at[0])
    monkeypatch.setattr("social.media_studio.now", lambda: at[0])
    return at


class MusicProvider:
    def __init__(self, code="THIRD_PARTY.RATE_LIMITED", *, recover=False):
        self.code, self.recover, self.submitted = code, recover, []

    def task(self, task_id):
        if self.recover and task_id != "original-music":
            return {"status": "SUCCEEDED", "output": ["https://output.test/music"]}
        return {"status": "FAILED", "failureCode": self.code}

    def reference(self, asset):
        pytest.fail("Music retry must not upload or regenerate saved visuals")

    def submit(self, endpoint, payload):
        assert (endpoint, payload["model"]) in {
            ("text_to_music", "eleven_music_v2_5"), ("sound_effect", "seed_audio")
        }
        self.submitted.append(payload)
        return f"music-retry-{len(self.submitted)}"

    def download(self, url):
        stream = io.BytesIO()
        with wave.open(stream, "wb") as track:
            track.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
            track.writeframes(b"\0\0" * 48000 * 12)
        return stream.getvalue()


def test_music_backoff_survives_reload_and_recovers_without_regenerating_video(db, settings, music_job, clock):
    provider = MusicProvider(recover=True)
    clips = copy.deepcopy(music_job.brief["video_segments"])
    history, sources = list(music_job.task_history), list(music_job.source_asset_ids)
    process_jobs(db, settings, provider)
    assert music_job.state == "queued" and music_job.task_id == ""
    assert datetime.fromisoformat(music_job.brief["runway_retry_at"]) == clock[0] + timedelta(minutes=2)
    assert music_job.brief["failed_tasks"][-1]["task_id"] == "original-music"
    assert music_job.brief["failed_tasks"][-1]["automatic"]
    with TestClient(create_app(settings)) as client:
        page = client.get("/admin/media-queue").text
        assert "Waiting to retry music" in page and "Saved video clips are kept" in page
    db.expire_all()
    clock[0] += timedelta(seconds=119)
    process_jobs(db, settings, provider)
    assert not provider.submitted
    clock[0] += timedelta(seconds=1)
    process_jobs(db, settings, provider)
    assert music_job.state == "running" and len(provider.submitted) == 1
    assert "runway_retry_at" not in music_job.brief
    assert provider.submitted[0]["promptText"] == "Calm instrumental piano."
    process_jobs(db, settings, provider)
    assert music_job.state == "rendering"
    assert music_job.brief["video_segments"] == clips
    assert music_job.task_history[:-1] == history
    assert music_job.source_asset_ids[:-1] == sources
    assert music_job.task_history[-1]["stage"] == "audio"


def test_music_rate_limit_retries_are_bounded_then_switch_once(db, settings, music_job, clock):
    settings.media_auto_retries = 3
    provider = MusicProvider()
    for delay in (120, 240, 480):
        process_jobs(db, settings, provider)
        assert music_job.state == "queued"
        assert datetime.fromisoformat(music_job.brief["runway_retry_at"]) == clock[0] + timedelta(seconds=delay)
        clock[0] += timedelta(seconds=delay)
        process_jobs(db, settings, provider)
        assert music_job.state == "running"
    process_jobs(db, settings, provider)
    assert music_job.state == "queued" and music_job.brief["soundtrack_model"] == "seed_audio"
    assert "runway_retry_at" not in music_job.brief
    for _ in range(3):
        process_jobs(db, settings, provider)
    assert len(provider.submitted) == 4 and provider.submitted[-1]["model"] == "seed_audio"
    assert music_job.state == "failed" and "rate limiting" in music_job.error
    assert [f.get("retry_delay_seconds") for f in music_job.brief["failed_tasks"]] == [120, 240, 480, None]
    assert retry_option(music_job)["rate_limited"]
    retry_step(db, music_job, "")  # Manual retry retains Seed Audio, without starting another automatic cycle.
    clock[0] += timedelta(minutes=2)
    process_jobs(db, settings, provider)
    process_jobs(db, settings, provider)
    assert music_job.state == "failed" and len(provider.submitted) == 5
    assert music_job.brief["failed_tasks"][-1]["retry_delay_seconds"] == 120


def test_existing_failed_music_can_be_retried_from_form_with_delay(db, settings, music_job, clock):
    music_job.state = "failed"
    music_job.error = (
        "Runway couldn't make the music. Runway reported that the task failed. "
        "Generating it again usually works. Runway code: THIRD_PARTY.RATE_LIMITED."
    )
    db.commit()
    with TestClient(create_app(settings)) as client:
        token = client.get("/api/session").json()["csrf"]
        page = client.get("/admin/media-queue").text
        assert "You can keep this prompt" in page and "Generate the music again" in page
        result = client.post(f"/forms/media-jobs/{music_job.id}/resubmit", data={"csrf": token})
        assert result.status_code == 200 and "Waiting to retry music" in result.text
    db.refresh(music_job)
    assert music_job.state == "queued"
    assert datetime.fromisoformat(music_job.brief["runway_retry_at"]) == clock[0] + timedelta(minutes=2)
    assert not music_job.brief["failed_tasks"][-1].get("automatic")
    process_jobs(db, settings, MusicProvider())
    assert music_job.state == "queued" and not music_job.task_id


def test_delayed_music_does_not_take_the_ready_job_limit(db, settings, music_job, clock):
    provider = MusicProvider()
    process_jobs(db, settings, provider)
    settings.media_jobs_per_tick = 1
    brief = {k: v for k, v in music_job.brief.items() if k not in {"runway_retry_at", "failed_tasks"}}
    ready = MediaJob(batch_key=uid(), platform=music_job.platform, brief=brief, state="queued", stage="audio",
                     source_asset_ids=list(music_job.source_asset_ids), task_history=list(music_job.task_history))
    db.add(ready)
    db.commit()
    process_jobs(db, settings, provider)
    assert ready.state == "running" and music_job.state == "queued" and len(provider.submitted) == 1


@pytest.mark.parametrize("global_pause", [False, True])
def test_due_music_retry_still_respects_pauses(db, settings, music_job, clock, global_pause):
    provider = MusicProvider()
    process_jobs(db, settings, provider)
    clock[0] += timedelta(minutes=2)
    record = db.get(Control, 1) if global_pause else db.get(Account, music_job.platform)
    field = "killed" if global_pause else "paused"
    setattr(record, field, True)
    db.commit()
    process_jobs(db, settings, provider)
    assert music_job.state == "queued" and not provider.submitted
    setattr(record, field, False)
    db.commit()
    process_jobs(db, settings, provider)
    assert music_job.state == "running" and len(provider.submitted) == 1


@pytest.mark.parametrize("code", ["SAFETY.INPUT.TEXT", "INTERNAL.BAD_OUTPUT.CODE01", "THIRD_PARTY.UNAVAILABLE"])
def test_other_provider_failures_do_not_start_music_retries(db, settings, music_job, clock, code):
    provider = MusicProvider(code)
    process_jobs(db, settings, provider)
    assert music_job.state == "failed" and music_job.task_id == "original-music"
    assert "runway_retry_at" not in music_job.brief and not provider.submitted


def test_music_retry_can_be_disabled_and_uncertain_submission_is_never_repeated(db, settings, music_job, clock):
    settings.media_auto_retries = 0
    process_jobs(db, settings, MusicProvider())
    assert music_job.state == "failed" and not music_job.brief.get("failed_tasks")
    settings.media_auto_retries = 3
    music_job.state, music_job.task_id = "uncertain", ""
    db.commit()
    assert not auto_retry(db, settings, music_job)


def test_cancelled_music_is_not_restarted_even_with_a_stale_rate_limit_code(db, settings, music_job, clock):
    provider = MusicProvider()
    provider.task = lambda _: {"status": "CANCELED", "failureCode": "THIRD_PARTY.RATE_LIMITED"}
    process_jobs(db, settings, provider)
    assert music_job.state == "failed" and "cancelled" in music_job.error
    assert not provider.submitted and "runway_retry_at" not in music_job.brief


def test_visual_rate_limit_is_not_treated_as_music(db, settings, music_job):
    music_job.stage, music_job.state = "visual", "failed"
    music_job.brief = music_job.brief | {"active_generation": "scene"}
    music_job.error = runway_failure(music_job, {"status": "FAILED", "failureCode": "THIRD_PARTY.RATE_LIMITED"})
    db.commit()
    assert not auto_retry(db, settings, music_job)
