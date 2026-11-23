from unittest.mock import patch

import pytest
from sqlalchemy import select
from test_runway_media import brief_for

from social import media_studio
from social.clip_replace import replace_part, source_job
from social.config import profiles
from social.facts import content_digest
from social.gate import media_errors, run_gate
from social.human_review import approve
from social.media_changes import request_changes
from social.media_studio import process_jobs, queue_jobs, resume_job
from social.models import Account, Asset, MediaJob, Post
from social.pipeline import generate, publish_due
from social.scheduler import schedule


def finish(db, settings, jobs):
    for _ in range(12):
        if all(j.state == "complete" for j in jobs):
            return
        process_jobs(db, settings)
        db.expire_all()
    assert all(j.state == "complete" for j in jobs), [(j.platform, j.state, j.error) for j in jobs]


def test_one_video_three_independently_reviewable_posts(db, settings, house_logo):
    brief = brief_for(db, settings, operation="video", platforms=["linkedin", "instagram", "facebook"])
    jobs = queue_jobs(db, settings, brief)
    db.commit()
    settings.media_jobs_per_tick = 1
    with patch.object(media_studio, "video_sequence", wraps=media_studio.video_sequence) as render:
        finish(db, settings, jobs)
    assert render.call_count == 1
    assert len({j.output_asset_id for j in jobs}) == 1
    assert len({j.post_id for j in jobs}) == 3
    origin = next(j for j in jobs if j.task_history)
    assert origin.platform == "facebook"
    assert [h["stage"] for h in origin.task_history] == ["visual", "audio"]
    assert sum(len(j.task_history) for j in jobs) == 2
    asset = db.get(Asset, origin.output_asset_id)
    assert (asset.media["width"], asset.media["height"]) == (720, 1280)
    posts = [db.get(Post, j.post_id) for j in jobs]
    for post in posts:
        assert post.asset_ids == [asset.id] and post.human_review_required
        assert not media_errors([asset], profiles(settings)[post.platform], post.platform)
        assert source_job(db, post).id == origin.id
        generate(db, settings, post)
        run_gate(db, settings, post)
        assert post.status == "awaiting_review"
    assert len({p.caption for p in posts}) == 3
    linkedin, instagram, facebook = posts
    approve(db, settings, facebook, "admin", content_digest(facebook, [asset]))
    schedule(db, settings, facebook)
    assert facebook.dry_run
    publish_due(db, settings, at=facebook.scheduled_at)
    assert facebook.status == "published" and linkedin.status == instagram.status == "awaiting_review"
    replacement = replace_part(db, linkedin, "music", "Calmer music", "admin")
    assert replacement.platform == "linkedin" and replacement.brief["platforms"] == ["linkedin"]
    assert replacement.brief["replaces"]["media_job_id"] == origin.id
    finish(db, settings, [replacement])
    assert db.get(Post, replacement.post_id).platform == "linkedin"
    assert replacement.output_asset_id != asset.id
    assert facebook.asset_ids == instagram.asset_ids == [asset.id]
    assert facebook.status == "published" and instagram.status == "awaiting_review"


def test_incompatible_image_size_reuses_generated_source(db, settings, monkeypatch):
    configured = profiles(settings)
    configured["facebook"] = configured["facebook"].model_copy(update={"image_height": 1350})
    for platform in ("linkedin", "x"):
        configured[platform] = configured[platform].model_copy(update={"image_min_ratio": 1})
    monkeypatch.setattr(media_studio, "profiles", lambda _: configured)
    jobs = queue_jobs(
        db, settings, brief_for(db, settings, platforms=["instagram", "facebook", "linkedin", "x"])
    )
    db.commit()
    finish(db, settings, jobs)
    instagram, facebook, linkedin, x = jobs
    assert instagram.output_asset_id == facebook.output_asset_id
    assert x.output_asset_id == linkedin.output_asset_id
    assert instagram.output_asset_id != linkedin.output_asset_id
    assert len(instagram.task_history) == 1 and not linkedin.task_history
    assert linkedin.source_asset_ids == instagram.source_asset_ids
    output = db.get(Asset, linkedin.output_asset_id)
    assert not media_errors([output], configured["linkedin"], "linkedin")
    assert source_job(db, db.get(Post, linkedin.post_id)).id == linkedin.id


def test_planned_real_posts_share_an_edit_and_revise_only_one_destination(db, settings, assets):
    brief = brief_for(
        db,
        settings,
        platforms=["linkedin", "facebook", "pinterest"],
        operation="real",
        real_format="photo",
        reference_ids=[assets[0].id],
        workflow="next",
    )
    assets[0].tags = assets[0].tags | {"google_drive_file_id": "project-photo"}
    jobs = queue_jobs(db, settings, brief)
    first, later, pin = jobs
    first.state = "queued"
    db.commit()
    finish(db, settings, [first])
    assert later.state == pin.state == "planned" and not later.post_id and not pin.post_id
    assets[0].tags = {"product": "demo"}
    later.state = pin.state = "queued"
    db.commit()
    process_jobs(db, settings)
    assert pin.state == "failed" and "Google Drive" in pin.error and not pin.post_id
    assets[0].tags = assets[0].tags | {"google_drive_file_id": "project-photo"}
    resume_job(pin)
    db.commit()
    finish(db, settings, jobs)
    assert first.output_asset_id == later.output_asset_id == pin.output_asset_id
    assert not later.brief.get("real_edit") and first.brief["real_edit"]
    post = db.get(Post, later.post_id)
    post.status = "awaiting_review"
    db.commit()
    revised = request_changes(db, settings, post, "Brighten the photo", "admin")
    assert revised.platform == "facebook" and revised.brief["platforms"] == ["facebook"]
    assert revised.brief["revision"]["previous_edit"]["items"] == first.brief["real_edit"]["items"]


@pytest.mark.parametrize("action", ["resume", "cancel"])
def test_failed_source_waits_without_blocking_other_jobs(db, settings, action):
    settings.media_jobs_per_tick = 1
    jobs = queue_jobs(db, settings, brief_for(db, settings, platforms=["instagram", "linkedin", "facebook"]))
    # A paused account doesn't have to start the batch for eligible platforms.
    db.get(Account, "facebook").paused = True
    [independent] = queue_jobs(db, settings, brief_for(db, settings, platforms=["x"]))
    db.commit()
    with patch.object(media_studio, "write_prompt", side_effect=ValueError("Test failure")):
        process_jobs(db, settings)
    failed = next(j for j in jobs if j.state == "failed")
    assert failed.platform == "linkedin"
    assert jobs[0].brief["shared_render_from"] == failed.id
    finish(db, settings, [independent])
    assert all(not j.post_id for j in jobs)
    if action == "resume":
        resume_job(failed)
    else:
        failed.state = "cancelled"
    db.get(Account, "facebook").paused = False
    db.commit()
    remaining = [j for j in jobs if j.state != "cancelled"]
    finish(db, settings, remaining)
    assert len({j.output_asset_id for j in remaining}) == 1
    assert len(db.scalars(select(MediaJob).where(MediaJob.state == "complete")).all()) == len(remaining) + 1
