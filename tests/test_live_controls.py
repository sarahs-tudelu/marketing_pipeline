from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from social.adapters import ADAPTERS
from social.adapters.base import PublishError
from social.app import create_app
from social.db import now
from social.models import Account
from social.pipeline import publish_due
from social.security import Vault


def authenticated_client(settings):
    client = TestClient(create_app(settings), base_url="https://studio.test")
    client.auth = ("owner", "test-only")
    return client


def connected(db, settings):
    settings.auth_mode = "basic"
    settings.admin_username, settings.admin_password = "owner", SecretStr("test-only")
    settings.app_base_url, settings.media_base_url = "https://studio.test", "https://media.test"
    account = db.get(Account, "facebook")
    account.external_id, account.validated_at = "page-123", now()
    account.credentials = Vault(settings).encrypt({"access_token": "test-only"})
    db.commit()
    return account


def test_live_toggle_persists_and_overrides_environment_without_converting_posts(db, settings, post):
    account = connected(db, settings)
    post.platform = "facebook"
    db.commit()
    with authenticated_client(settings) as client:
        token = client.get("/api/session").json()["csrf"]
        assert client.post("/forms/live-platform", data={"platform": "facebook", "enabled": "true"}).status_code == 403
        response = client.post("/forms/live-platform", data={"csrf": token, "platform": "facebook", "enabled": "true"})
        assert response.status_code == 200 and 'aria-checked="true"' in response.text
        db.refresh(account)
        assert account.live_enabled is True and settings.wants_live("facebook", account)
        adapter = ADAPTERS["facebook"](settings, db, post)
        assert not adapter.dry
        db.refresh(post)
        assert post.dry_run  # Existing simulated posts are not promoted to public delivery.
        settings.live_platforms = "facebook"
        response = client.post("/forms/live-platform", data={"csrf": token, "platform": "facebook", "enabled": "false"})
        assert response.status_code == 200
        db.refresh(account)
        assert account.live_enabled is False and not settings.wants_live("facebook", account)
        assert ADAPTERS["facebook"](settings, db, post).dry
        with pytest.raises(PublishError, match="switched off"):
            adapter.guard()  # An adapter created before the switch also stops its next live request.
    with authenticated_client(settings) as client:
        assert client.get("/admin/connections").status_code == 200
    db.refresh(account)
    assert account.live_enabled is False


def test_live_cannot_be_enabled_without_connection_or_authenticated_app(db, settings):
    with TestClient(create_app(settings)) as client:
        token = client.get("/api/session").json()["csrf"]
        response = client.post("/forms/live-platform", data={"csrf": token, "platform": "facebook", "enabled": "true"})
        assert response.status_code == 400 and "authenticated" in response.text
    connected(db, settings)
    with authenticated_client(settings) as client:
        token = client.get("/api/session").json()["csrf"]
        response = client.post("/forms/live-platform", data={"csrf": token, "platform": "instagram", "enabled": "true"})
        assert response.status_code == 400 and "Connect and validate" in response.text
    assert db.get(Account, "instagram").live_enabled is None


def test_disabled_live_platform_keeps_scheduled_live_post_pending(db, settings, post):
    account = connected(db, settings)
    account.live_enabled = False
    post.platform, post.status, post.dry_run = "facebook", "scheduled", False
    post.scheduled_at = now() - timedelta(minutes=1)
    db.commit()
    publish_due(db, settings)
    db.refresh(post)
    assert post.status == "scheduled" and post.attempts == 0 and not post.error


def test_stale_schedule_form_displays_worker_result(db, settings, post):
    post.status = "scheduled"
    post.scheduled_at = now() + timedelta(hours=1)
    db.commit()
    with TestClient(create_app(settings)) as client:
        token = client.get("/api/session").json()["csrf"]
        response = client.post(f"/forms/posts/{post.id}/schedule", data={"csrf": token})
        assert response.status_code == 200 and 'role="alert"' not in response.text
        assert "scheduled" in response.text
    db.refresh(post)
    assert post.status == "scheduled"
