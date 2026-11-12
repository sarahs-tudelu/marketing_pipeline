from types import SimpleNamespace
from unittest.mock import patch

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from social.app import create_app
from social.db import make_engine
from social.gate import run_gate
from social.models import Control, MediaCampaign, MediaJob
from social.pipeline import draft, generate, publish_due
from social.worker import advance_queue


def test_automation_switch_persists_and_overrides_environment(db, settings, caplog):
    caplog.set_level("INFO", logger="social")
    control = db.get(Control, 1)
    assert control.automation_enabled is None and settings.automates_posts(control)
    with TestClient(create_app(settings)) as client:
        assert 'id="automation-controls"' in client.get("/").text
        assert client.post("/forms/automation", data={"enabled": "false"}).status_code == 403
        token = client.get("/api/session").json()["csrf"]
        assert client.post("/forms/automation", data={"csrf": token, "enabled": "invalid"}).status_code == 400
        response = client.post("/forms/automation", data={"csrf": token, "enabled": "false"})
        assert response.status_code == 200 and "Enable automatic posts" in response.text
        db.refresh(control)
        assert control.automation_enabled is False and not settings.automates_posts(control)
    with TestClient(create_app(settings)) as client:
        assert "Enable automatic posts" in client.get("/").text
        settings.auto_advance = False
        token = client.get("/api/session").json()["csrf"]
        assert client.post("/forms/automation", data={"csrf": token, "enabled": "true"}).status_code == 200
    db.refresh(control)
    assert settings.automates_posts(control) and settings.auto_advance is False
    assert "automation_changed" in caplog.text


@pytest.mark.parametrize("stage", ["caption", "checks"])
def test_switch_off_between_worker_stages_prevents_further_progress(db, settings, assets, stage, monkeypatch):
    from social.facts import load_facts

    facts = load_facts(db, settings.facts_path)
    post = draft(db, "linkedin", "Automatic toggle", [assets[0].id], facts.id, product="demo")
    if stage == "checks":
        generate(db, settings, post)
    original = generate if stage == "caption" else run_gate

    def finish_then_switch_off(*args, **kwargs):
        result = original(*args, **kwargs)
        with Session(db.get_bind()) as other:
            other.get(Control, 1).automation_enabled = False
            other.commit()
        return result

    monkeypatch.setattr("social.worker.generate" if stage == "caption" else "social.gate.run_gate", finish_then_switch_off)
    advance_queue(db, settings)
    db.refresh(post)
    assert post.status == ("generated" if stage == "caption" else "approved")
    assert post.scheduled_at is None
    advance_queue(db, settings)
    assert post.scheduled_at is None


def test_automation_off_allows_manual_steps_and_existing_scheduled_delivery(db, settings, post):
    control = db.get(Control, 1)
    control.automation_enabled = False
    db.commit()
    advance_queue(db, settings)
    assert post.status == "generated"
    with TestClient(create_app(settings)) as client:
        token = client.get("/api/session").json()["csrf"]
        assert client.post(f"/forms/posts/{post.id}/check", data={"csrf": token}).status_code == 200
        db.refresh(post)
        assert post.status == "approved"
        assert client.post(f"/forms/posts/{post.id}/schedule", data={"csrf": token}).status_code == 200
    db.refresh(post)
    assert post.status == "scheduled" and post.dry_run
    publish_due(db, settings, at=post.scheduled_at)
    assert post.status == "published" and post.dry_run


def test_global_stop_still_blocks_automation(db, settings, post):
    control = db.get(Control, 1)
    control.automation_enabled, control.killed = True, True
    db.commit()
    advance_queue(db, settings)
    assert post.status == "generated"
    assert settings.automates_posts(control)  # Preference is retained while the global stop is active.


def test_recurring_batches_respect_ui_override_but_queued_media_still_finishes(db, settings, monkeypatch):
    from test_runway_media import brief_for

    from social.adapters import ADAPTERS
    from social.facts import save_facts
    from social.media_studio import process_jobs, queue_jobs, replenish_campaigns

    job = queue_jobs(db, settings, brief_for(db, settings))[0]
    control = db.get(Control, 1)
    control.automation_enabled = False
    db.commit()
    for _ in range(3):
        process_jobs(db, settings)
    assert job.state == "complete"
    sheet = save_facts(db, {"version": "automation-test", "brand_voice": "Simple", "hashtags": {}, "facts": [{"id": "name", "product": "company", "text": "Test brand."}]})
    brief = brief_for(db, settings).model_copy(update={"facts_id": sheet.id})
    campaign = MediaCampaign(name="Automation campaign", brief=brief.model_dump(), enabled=True)
    db.add(campaign)
    db.commit()
    monkeypatch.setitem(ADAPTERS, "instagram", lambda *args: SimpleNamespace(dry=False))
    settings.ai_mode, settings.live_platforms = "live", "instagram"
    settings.runway_api_key = SecretStr("test-only")
    replenish_campaigns(db, settings)
    assert campaign.sequence == 0
    control.automation_enabled = True
    settings.auto_advance = False
    db.commit()
    replenish_campaigns(db, settings)
    assert campaign.sequence == 1, campaign.error
    assert len(db.scalars(select(MediaJob).where(MediaJob.campaign_id == campaign.id)).all()) == 1


def test_migration_preserves_global_control_and_environment_default(settings):
    config = Config("alembic.ini")
    with patch("social.config.Settings", return_value=settings):
        command.upgrade(config, "b7d2e9a40316")
        engine = make_engine(settings.database_dsn)
        try:
            with engine.begin() as connection:
                connection.execute(text("INSERT INTO controls (id, killed, revision) VALUES (1, 1, 7)"))
            command.upgrade(config, "head")
            with engine.connect() as connection:
                assert connection.execute(text("SELECT killed, revision, automation_enabled FROM controls")).one() == (1, 7, None)
            command.check(config)
        finally:
            engine.dispose()
