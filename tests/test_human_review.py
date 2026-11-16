import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_runway_media import brief_for

from social.app import create_app
from social.db import now
from social.facts import content_digest
from social.gate import run_gate
from social.human_review import approve, stop
from social.lifecycle import PostConflict
from social.media_studio import process_jobs, queue_jobs
from social.models import MediaCampaign, MediaJob, Post, RequestRecord, Transition
from social.pipeline import delete_draft, publish_due, rewrite_caption
from social.providers import Providers
from social.scheduler import schedule
from social.worker import advance_queue


def ready(db, settings, post):
    post.human_review_required = True
    db.commit()
    run_gate(db, settings, post)
    assert post.status == "awaiting_review"


def test_manual_waits_and_explicit_approval_schedules_current_preview(db, settings, post, assets):
    ready(db, settings, post)
    settings.auto_advance = True
    advance_queue(db, settings)
    assert post.status == "awaiting_review" and not post.scheduled_at
    with pytest.raises(PostConflict):
        schedule(db, settings, post)
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/posts/{post.id}")
        assert 'Media to be posted' in page.text and assets[0].storage_key in page.text
        assert 'Caption to be posted' in page.text and 'Approve &amp; schedule' in page.text
        token = client.get('/api/session').json()['csrf']
        digest = re.search(r'name="reviewed_digest" value="([a-f0-9]+)"', page.text)[1]
        response = client.post(f'/forms/posts/{post.id}/approve', data={'csrf': token, 'reviewed_digest': digest})
        assert response.status_code == 200
    db.refresh(post)
    assert post.status == 'scheduled'
    assert post.human_approved_digest == content_digest(post, assets)
    assert db.scalar(select(Transition.actor).where(Transition.post_id == post.id, Transition.to_status == 'approved'))


def test_old_preview_and_failed_checks_cannot_authorize(db, settings, post, assets):
    ready(db, settings, post)
    with pytest.raises(ValueError, match='changed since'):
        approve(db, settings, post, 'human', 'outdated')
    db.rollback()
    assets[0].rights = 'Not public'
    db.commit()
    approve(db, settings, post, 'human', content_digest(post, assets))
    # Internal-only media is permitted in dry runs, so use a definite factual defect.
    if post.status == 'approved':
        post.status = 'awaiting_review'
        post.fact_ids = ['DOES_NOT_EXIST']
        post.parts = [{'text': 'Invented claim', 'fact_ids': ['DOES_NOT_EXIST']}]
        db.commit()
        approve(db, settings, post, 'human', content_digest(post, assets))
    assert post.status == 'held' and not post.human_approved_digest


def test_edits_cannot_bypass_schedule_or_publisher(db, settings, post, assets):
    ready(db, settings, post)
    approve(db, settings, post, 'human', content_digest(post, assets))
    post.caption += ' changed'
    db.commit()
    with pytest.raises(PostConflict, match='Review and approve'):
        schedule(db, settings, post)
    assert post.status == 'awaiting_review'
    # Even a stale/manual scheduled row cannot bypass the independent delivery gate.
    post.status = 'scheduled'
    post.scheduled_at = now() - timedelta(minutes=1)
    post.approved_digest = content_digest(post, assets)
    db.commit()
    publish_due(db, settings, at=post.scheduled_at)
    assert post.status == 'held'
    assert not db.scalar(select(RequestRecord.id).where(RequestRecord.post_id == post.id))


def test_automatic_schedule_can_be_stopped_and_cannot_auto_resume(db, settings, post):
    run_gate(db, settings, post)
    assert post.status == 'approved' and not post.human_review_required
    schedule(db, settings, post)
    due = post.scheduled_at
    stop(db, post, 'human')
    assert post.status == 'awaiting_review' and not post.scheduled_at
    assert post.human_review_required
    settings.auto_advance = True
    advance_queue(db, settings)
    publish_due(db, settings, at=due)
    assert post.status == 'awaiting_review'
    assert not db.scalar(select(RequestRecord.id))


def test_caption_feedback_reaches_provider_and_requires_review(db, settings, post, monkeypatch):
    ready(db, settings, post)
    previous = post.caption
    seen = []
    original = Providers.caption

    def capture(self, context):
        seen.append(context)
        return original(self, context)

    monkeypatch.setattr(Providers, 'caption', capture)
    rewrite_caption(db, settings, post, 'human', 'Use a warmer opening')
    assert seen[0]['revision_request'] == 'Use a warmer opening'
    assert seen[0]['previous_caption'] == previous
    assert post.status == 'awaiting_review' and not post.human_approved_digest


@pytest.mark.parametrize('automated', [False, True])
def test_media_origin_controls_human_review(db, settings, automated):
    brief = brief_for(db, settings, workflow='immediate')
    campaign = None
    if automated:
        campaign = MediaCampaign(name='Recurring', brief=brief.model_dump())
        db.add(campaign)
        db.flush()
    job = queue_jobs(db, settings, brief, campaign=campaign)[0]
    db.commit()
    settings.auto_advance = True
    for _ in range(3):
        process_jobs(db, settings)
        advance_queue(db, settings)
    post = db.get(Post, job.post_id)
    assert post.human_review_required is not automated
    assert post.status == ('scheduled' if automated else 'awaiting_review')


def test_delete_restart_keeps_assets_and_reopens_idea(db, settings, post, assets):
    ready(db, settings, post)
    with TestClient(create_app(settings)) as client:
        token = client.get('/api/session').json()['csrf']
        response = client.post(f'/forms/posts/{post.id}/restart', data={'csrf': token})
        assert response.status_code == 200
        assert response.url.path == '/admin/create'
        assert post.campaign in response.text
    db.refresh(post)
    assert post.status == 'deleted'
    assert assets[0].id in post.asset_ids


def test_deleted_draft_disappears_from_generation_queue(db, settings, post):
    job = queue_jobs(db, settings, brief_for(db, settings))[0]
    job.state, job.post_id = 'complete', post.id
    db.commit()
    with TestClient(create_app(settings)) as client:
        token = client.get('/api/session').json()['csrf']
        assert f'data-job-id="{job.id}"' in client.get('/admin/media-queue').text
        response = client.post(f'/forms/posts/{post.id}/delete', data={'csrf': token})
        assert response.status_code == 200
        assert f'data-job-id="{job.id}"' not in client.get('/admin/media-queue').text
        assert job.id not in [row['id'] for row in client.get('/api/media-jobs').json()]
        assert client.get(f'/posts/{post.id}').status_code == 404
    db.refresh(post)
    assert post.status == 'deleted'
    assert db.get(MediaJob, job.id)  # Hidden from the workspace; delivery/retry audit remains intact.


@pytest.mark.parametrize('action', [stop, delete_draft])
def test_uncertain_delivery_cannot_be_stopped_or_deleted(db, settings, post, action):
    post.status = 'held'
    post.error = 'ambiguous: delivery not confirmed'
    db.commit()
    with pytest.raises(ValueError, match='Delivery may have started'):
        action(db, post, 'human')


def test_permission_change_invalidates_human_review(db, settings, post, assets):
    from test_asset_edit import save

    ready(db, settings, post)
    approve(db, settings, post, 'human', content_digest(post, assets))
    save(db, assets[0], owner='Updated rights owner')
    assert post.status == 'held' and not post.human_approved_digest
    assert not post.approved_digest


def test_review_media_revision_queues_replacement_and_keeps_review_required(db, settings):
    from social.clip_replace import clip_options, replace_part

    job = queue_jobs(db, settings, brief_for(db, settings))[0]
    db.commit()
    for _ in range(3):
        process_jobs(db, settings)
        advance_queue(db, settings)
    post = db.get(Post, job.post_id)
    assert post.status == 'awaiting_review'
    assert clip_options(db, post)['parts']
    replacement = replace_part(db, post, 'visual', 'Warm evening light on the entrance', 'human')
    assert post.status == 'held'
    assert replacement.brief['generation_prompt'] == 'Warm evening light on the entrance'
    with pytest.raises(ValueError, match='replacement is already'):
        delete_draft(db, post, 'human')
    db.rollback()
    for _ in range(3):
        process_jobs(db, settings)
        advance_queue(db, settings)
    revised = db.get(Post, replacement.post_id)
    assert revised.human_review_required and revised.status == 'awaiting_review'
    assert revised.asset_ids != post.asset_ids


def test_migration_backfills_manual_schedules_without_changing_campaign_schedules(db, settings, post):
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    from social.models import MediaJob
    from social.pipeline import draft

    automatic = draft(db, post.platform, 'Recurring', post.asset_ids, post.facts_id, human_review_required=False)
    campaign = MediaCampaign(name='Recurring', brief={})
    db.add(campaign)
    db.flush()
    db.add(MediaJob(campaign_id=campaign.id, batch_key='migration-test', platform=post.platform, brief={}, post_id=automatic.id))
    for row in [post, automatic]:
        row.status, row.scheduled_at = 'scheduled', now()
    db.commit()
    migration = importlib.import_module('migrations.versions.c6f3a7b95247_human_post_review')
    with Operations.context(MigrationContext.configure(db.connection())) as operations:
        operations.drop_column('posts', 'human_approved_digest')
        operations.drop_column('posts', 'human_review_required')
        migration.upgrade()
    db.commit()
    db.expire_all()
    assert post.human_review_required and post.status == 'awaiting_review' and post.scheduled_at is None
    assert not automatic.human_review_required and automatic.status == 'scheduled' and automatic.scheduled_at
    assert db.scalar(text("SELECT COUNT(*) FROM transitions WHERE to_status='awaiting_review' AND actor='migration'")) == 1
