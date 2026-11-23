from unittest.mock import patch

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_campaign_edit import campaign_fixture  # noqa: F401
from test_live_controls import authenticated_client, connected
from test_media_queue_cleanup import job

from social.adapters import adapter
from social.adapters.base import PublishError
from social.app import create_app
from social.campaigns import update_campaign
from social.db import now
from social.facts import content_digest
from social.gate import run_gate
from social.media_studio import CreativeBrief, _complete_job
from social.models import MediaJob, Post, PostRepost, RequestRecord, Transition
from social.pipeline import draft, generate, publish_due
from social.retries import retry_post
from social.scheduler import schedule


def test_private_job_schedules_and_simulates_on_live_platform(db, settings, campaign, post, assets):
    account = connected(db, settings)
    account.live_enabled = True
    batch = job(db, campaign, state="rendering")
    batch.platform = "facebook"
    batch.brief = batch.brief | {"facts_id": post.facts_id, "product": post.product}
    _complete_job(db, batch, assets[0])  # The media itself can even have public usage rights.
    private = db.get(Post, batch.post_id)
    generate(db, settings, private)
    run_gate(db, settings, private)
    assert private.status == "approved" and private.simulation_only
    with authenticated_client(settings) as client:
        assert (
            client.get("/admin/published-posts").context["published_groups"]["simulated"]["facebook"][0].id
            == private.id
        )
        schedule(db, settings, private)
        assert private.dry_run and "Simulation scheduled" in client.get("/admin/published-posts").text

        def unexpected_request(request):
            pytest.fail("Simulated delivery attempted platform HTTP")

        with httpx.Client(transport=httpx.MockTransport(unexpected_request)) as transport:
            live = adapter(settings, db, private, transport)
            assert not live.dry  # The connected account is genuinely eligible for live posting.
            with pytest.raises(PublishError, match="Not public"):
                live.request("publish", "POST", "https://graph.facebook.com/test", irreversible=True)
            db.rollback()
            publish_due(db, settings, at=private.scheduled_at, client=transport)
        assert private.status == "published" and private.dry_run
        records = db.scalars(select(RequestRecord).where(RequestRecord.post_id == private.id)).all()
        assert records and all(r.dry_run for r in records)
        assert "Simulation complete" in client.get("/admin/published-posts").text


def test_existing_private_reservations_are_corrected_and_live_evidence_is_preserved(
    db, settings, campaign, post
):
    def existing(status, *, owned=True):
        row = draft(db, "linkedin", "Same campaign name", post.asset_ids, post.facts_id)
        row.status, row.dry_run, row.scheduled_at = status, False, now()
        if owned:
            job(db, campaign, post=row)
        db.commit()
        return row

    safe = existing("scheduled")
    slot = safe.scheduled_at
    started = existing("publishing")
    uncertain = existing("scheduled")
    db.add(
        RequestRecord(
            post_id=uncertain.id,
            step="publish",
            request={},
            state="pending",
            irreversible=True,
            dry_run=False,
        )
    )
    delivered = existing("published")
    delivered.remote_id, delivered.live_url, delivered.published_at = (
        "real-id",
        "https://example.test/post",
        now(),
    )
    simulated_again = existing("scheduled", owned=False)
    db.add(PostRepost(original_post_id=delivered.id, repost_post_id=simulated_again.id, actor="test"))
    public = existing("scheduled", owned=False)  # Same name and asset, different job.
    db.commit()

    config = Config("alembic.ini")
    with patch("social.config.Settings", return_value=settings):
        command.stamp(config, "head")
        command.downgrade(config, "e3ab61c84570")
        command.upgrade(config, "head")
    db.expire_all()
    assert safe.status == "scheduled" and safe.dry_run and safe.simulation_only and safe.scheduled_at == slot
    assert simulated_again.simulation_only and simulated_again.dry_run
    assert all(
        row.status == "held" and not row.dry_run and row.error.startswith("ambiguous:")
        for row in (started, uncertain)
    )
    assert delivered.status == "published" and not delivered.dry_run and delivered.remote_id == "real-id"
    assert public.status == "scheduled" and not public.dry_run and not public.simulation_only
    assert db.scalar(
        select(Transition.reason).where(Transition.post_id == safe.id, Transition.actor == "campaign privacy")
    )
    with TestClient(create_app(settings)) as client:
        groups = client.get("/admin/published-posts").context["published_groups"]
        assert {p.id for p in groups["real"]["linkedin"]} == {public.id, delivered.id}
        assert {p.id for p in groups["simulated"]["linkedin"]} == {safe.id, simulated_again.id}


def test_tightening_campaign_restricts_existing_reservations_and_replacements(
    db, settings, campaign, post, assets
):
    campaign.brief = campaign.brief | {"rights": "public"}
    post.status, post.dry_run = "scheduled", False
    original = job(db, campaign, post=post)
    child = job(db, campaign, state="rendering", replaces=original)
    child.brief = child.brief | {"rights": "public"}
    plan = CreativeBrief.model_validate(campaign.brief)
    update_campaign(db, campaign, plan.model_copy(update={"rights": "not_public"}))
    db.commit()
    assert post.simulation_only and post.dry_run
    update_campaign(db, campaign, plan)
    _complete_job(db, child, assets[0])
    assert db.get(Post, child.post_id).simulation_only
    assert db.get(MediaJob, original.id).brief["rights"] == "public"


def test_private_retry_uses_simulation_checks_and_public_posts_still_reserve_live(db, settings, post, assets):
    live = connected(db, settings)
    live.live_enabled = True
    post.status, post.platform, post.simulation_only, post.dry_run = "failed", "facebook", True, False
    db.commit()
    retry = retry_post(db, settings, post, "reviewer")
    assert retry.simulation_only and retry.dry_run
    run_gate(db, settings, retry)
    with authenticated_client(settings) as client:
        page = client.get(f"/posts/{retry.id}")
        assert "schedules a simulated post" in page.text
        approved = client.post(
            f"/forms/posts/{retry.id}/approve",
            data={
                "csrf": page.context["csrf"],
                "reviewed_digest": content_digest(retry, assets),
            },
        )
        assert approved.status_code == 200
    db.refresh(retry)
    assert retry.status == "scheduled" and retry.dry_run
    public = draft(
        db, "facebook", "Independent campaign", post.asset_ids, post.facts_id, human_review_required=False
    )
    public.status = "approved"
    db.commit()
    schedule(db, settings, public)
    assert public.status == "scheduled" and not public.dry_run


def test_worker_recovers_private_reservation_created_without_policy_flag(db, settings, campaign, post):
    job(db, campaign, post=post)
    post.status, post.dry_run, post.scheduled_at = "scheduled", False, now()
    db.commit()
    publish_due(db, settings, at=post.scheduled_at)
    assert post.status == "scheduled" and post.simulation_only and post.dry_run
    assert not db.scalar(select(RequestRecord.id).where(RequestRecord.post_id == post.id))
