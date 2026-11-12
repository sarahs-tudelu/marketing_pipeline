import json

import pytest
from sqlalchemy import select
from test_runway_media import brief_for

from social.media_studio import process_jobs, queue_jobs, resume_job
from social.models import Asset, Post
from social.pipeline import draft, generate
from social.providers import Providers
from social.seed import seed_media
from social.storage import ingest, storage
from social.visual_context import current_summary, ensure_visual_summary


@pytest.mark.parametrize("kind", ["image", "video"])
def test_summary_observes_output_bytes_and_caches_checksum(db, settings, monkeypatch, kind):
    settings.ai_mode = "live"
    data = seed_media()[kind == "video"]
    asset = ingest(db, settings, data, "output.jpg" if kind == "image" else "output.mp4",
                   source="ai_edited", generator="runway/test", prompt="PROMPT_IS_NOT_EVIDENCE",
                   rights="Internal testing")
    calls = []

    def describe(self, endpoint, payload):
        calls.append(payload)
        assert endpoint == "responses" and payload["store"] is False
        assert "PROMPT_IS_NOT_EVIDENCE" not in json.dumps(payload)
        images = payload["input"][0]["content"][1:]
        assert all(i["type"] == "input_image" and i["image_url"].startswith("data:image/jpeg;base64,") for i in images)
        assert len(images) == 1 if kind == "image" else 1 < len(images) <= 12
        return {"status": "completed", "output": [{"type": "message", "content": [{
            "type": "output_text", "text": json.dumps({
                "summary": "A green abstract scene with a light central panel.",
                "visible_text": [], "limitations": [],
            }),
        }]}]}

    monkeypatch.setattr("social.providers.OpenAI.call", describe)
    summary = ensure_visual_summary(db, settings, asset)
    db.commit()
    db.refresh(asset)
    assert summary == current_summary(asset, settings)
    assert summary["asset_checksum"] == asset.checksum and not summary["simulated"]
    if kind == "video":
        assert summary["basis"] == "sampled_video_frames" and summary["limitations"]
        assert summary["sampled_timestamps_seconds"][-1] > 3
    assert storage(settings).read(asset.storage_key) == data
    assert ensure_visual_summary(db, settings, asset) == summary and len(calls) == 1
    asset.checksum = "changed"
    assert current_summary(asset, settings) is None
    with pytest.raises(ValueError, match="integrity"):
        ensure_visual_summary(db, settings, asset)
    assert len(calls) == 1


def test_runway_summary_reaches_caption_context_and_library(db, settings):
    brief = brief_for(db, settings)
    job = queue_jobs(db, settings, brief)[0]
    db.commit()
    for _ in range(3):
        process_jobs(db, settings)
    asset = db.get(Asset, job.output_asset_id)
    assert current_summary(asset, settings)
    assert current_summary(db.get(Asset, job.source_asset_ids[0]), settings)
    captured = []

    class Captions(Providers):
        def caption(self, context):
            captured.append(context)
            return super().caption(context)

    post = db.get(Post, job.post_id) if job.post_id else draft(
        db, job.platform, "Caption test", [asset.id], brief.facts_id, product="demo")
    generate(db, settings, post, providers=Captions(settings))
    assert captured[0]["visual_context"][0]["summary"] == asset.media["visual_summary"]["summary"]
    assert captured[0]["visual_context"][0]["asset_id"] == asset.id
    assert all("SIMULATED visual" not in f["text"] for f in captured[0]["facts"])


def test_summary_failure_resumes_saved_rendition_without_regenerating(db, settings, monkeypatch):
    job = queue_jobs(db, settings, brief_for(db, settings))[0]
    db.commit()
    process_jobs(db, settings)
    original = ensure_visual_summary

    def fail_rendition(db, settings, asset):
        if asset.tags.get("rendition"):
            raise ValueError("Summary provider temporarily unavailable")
        return original(db, settings, asset)

    monkeypatch.setattr("social.visual_context.ensure_visual_summary", fail_rendition)
    process_jobs(db, settings)
    assert job.state == "failed" and job.output_asset_id and not job.post_id
    output_id = job.output_asset_id
    asset_ids = set(db.scalars(select(Asset.id)))
    history = list(job.task_history)
    monkeypatch.setattr("social.visual_context.ensure_visual_summary", original)
    resume_job(job)
    db.commit()
    process_jobs(db, settings)
    assert job.state == "complete" and job.output_asset_id == output_id
    assert set(db.scalars(select(Asset.id))) == asset_ids
    assert job.task_history == history
    assert current_summary(db.get(Asset, output_id), settings)
