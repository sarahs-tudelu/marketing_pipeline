import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_runway_media import brief_for

from social.app import create_app
from social.media_studio import activate_planned_jobs, process_jobs, queue_jobs, replenish_campaigns
from social.models import Account, Asset, Control, MediaCampaign, MediaJob, Post
from social.pipeline import draft
from social.worker import advance_queue


def submit_idea(client, brief, mode):
    token = client.get("/api/session").json()["csrf"]
    return client.post("/forms/media-studio", data={
        "csrf": token, "mode": mode, "campaign": brief.campaign, "prompt": brief.prompt,
        "operation": brief.operation, "platforms": brief.platforms, "facts_id": brief.facts_id,
        "product": brief.product, "kind": brief.kind, "rights": brief.rights, "create_posts": "on",
        "reference_ids": brief.reference_ids,
    })


def test_next_post_idea_waits_for_automation_and_is_consumed_once(db, settings):
    control = db.get(Control, 1)
    control.automation_enabled = False
    db.commit()
    brief = brief_for(db, settings)
    db.commit()
    with TestClient(create_app(settings)) as client:
        assert 'href="/admin/create"' in client.get("/").text
        assert 'id="new-idea"' in client.get("/admin/create").text
        response = submit_idea(client, brief, "next")
        assert response.status_code == 200 and "Saved for the next automatic post" in response.text
        job = db.scalar(select(MediaJob))
        assert job.state == "planned" and job.brief["prompt"] == brief.prompt
        activate_planned_jobs(db, settings)
        process_jobs(db, settings)
        assert job.state == "planned" and not job.source_asset_ids
        token = client.get("/api/session").json()["csrf"]
        client.post("/forms/automation", data={"csrf": token, "enabled": "true"})
        activate_planned_jobs(db, settings)
        for _ in range(3):
            process_jobs(db, settings)
            advance_queue(db, settings)
        db.refresh(job)
        assert job.state == "complete" and job.post_id
        assert db.get(Post, job.post_id).status == "scheduled"
        activate_planned_jobs(db, settings)
        assert len(db.scalars(select(MediaJob)).all()) == 1
        assert len(db.scalars(select(Post)).all()) == 1
        assert client.get("/api/media-jobs").json()[0]["post_status"] == "scheduled"


def test_create_now_runs_full_workflow_with_automation_off_but_leaves_other_drafts(db, settings, assets, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Offline workflow must not contact external services")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    settings.auto_advance = False
    brief = brief_for(db, settings, reference_ids=[assets[0].id])
    unrelated = draft(db, "instagram", "Manual draft", [assets[0].id], brief.facts_id, product="demo")
    with TestClient(create_app(settings)) as client:
        response = submit_idea(client, brief, "once")
        assert response.status_code == 200
    job = db.scalar(select(MediaJob))
    assert job.brief["workflow"] == "immediate" and job.state == "queued"
    for _ in range(3):
        process_jobs(db, settings)
        advance_queue(db, settings)
    db.refresh(unrelated)
    assert unrelated.status == "draft"
    result = db.get(Post, job.post_id)
    assert result.status == "scheduled" and result.caption and result.dry_run
    assert db.get(Asset, job.output_asset_id).media["visual_summary"]
    assert not result.remote_id and result.published_at is None


def test_immediate_media_takes_priority_over_routine_queue(db, settings):
    routine = queue_jobs(db, settings, brief_for(db, settings))[0]
    immediate = queue_jobs(db, settings, brief_for(db, settings, workflow="immediate"))[0]
    db.commit()
    settings.media_jobs_per_tick = 1
    process_jobs(db, settings)
    assert immediate.state == "rendering" and routine.state == "queued"


def test_next_idea_can_be_cancelled_and_reserves_campaign_priority(db, settings):
    brief = brief_for(db, settings, workflow="next")
    job = queue_jobs(db, settings, brief)[0]
    campaign = MediaCampaign(name="Routine", brief=brief.model_copy(update={"workflow": "automatic"}).model_dump(), enabled=True)
    db.add(campaign)
    db.commit()
    settings.ai_mode = "live"
    replenish_campaigns(db, settings)
    assert campaign.sequence == 0 and "admin's requested post" in campaign.error
    settings.ai_mode = "fixture"
    with TestClient(create_app(settings)) as client:
        token = client.get("/api/session").json()["csrf"]
        assert client.post(f"/forms/media-jobs/{job.id}/cancel", data={"csrf": token}).status_code == 200
    db.refresh(job)
    activate_planned_jobs(db, settings)
    process_jobs(db, settings)
    assert job.state == "cancelled" and not job.source_asset_ids


@pytest.mark.parametrize("pause", ["global", "platform"])
def test_create_now_respects_global_and_platform_pauses(db, settings, pause):
    job = queue_jobs(db, settings, brief_for(db, settings, workflow="immediate"))[0]
    db.commit()
    settings.auto_advance = False
    for _ in range(2):
        process_jobs(db, settings)
    assert job.post_id
    if pause == "global":
        db.get(Control, 1).killed = True
    else:
        db.get(Account, job.platform).paused = True
    db.commit()
    advance_queue(db, settings)
    assert db.get(Post, job.post_id).status == "draft"
