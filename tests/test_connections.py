from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from social.app import create_app
from social.connections import begin, callback, refresh_account, save_meta, store_tokens
from social.db import now
from social.models import Account, Alert, OAuthState
from social.security import Vault, digest


def configured(settings):
    return settings.model_copy(
        update={
            "app_base_url": "https://social.example.org",
            "x_client_id": "test-client",
            "x_client_secret": settings.token_encryption_key,
        }
    )


def test_oauth_pkce_state_is_bound_expiring_and_one_use(db, settings):
    settings = configured(settings)
    url = begin(db, settings, "x", "browser-a")
    params = parse_qs(urlparse(url).query)
    assert params["code_challenge_method"] == ["S256"]
    assert "code_verifier" not in params
    state = params["state"][0]
    with pytest.raises(ValueError, match="browser session"):
        callback(db, settings, "x", state, "code", "browser-b")
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path.endswith("token"):
            assert "code_verifier=" in request.content.decode()
            return httpx.Response(
                200, json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 7200}
            )
        return httpx.Response(200, json={"data": {"id": "company-x"}})

    callback(
        db, settings, "x", state, "code", "browser-a", httpx.Client(transport=httpx.MockTransport(handler))
    )
    account = db.get(Account, "x")
    assert account.external_id == "company-x"
    assert "new-access" not in account.credentials
    assert Vault(settings).decrypt(account.credentials)["refresh_token"] == "new-refresh"
    with pytest.raises(ValueError, match="already used"):
        callback(db, settings, "x", state, "code", "browser-a")


@pytest.mark.parametrize(("consumed", "message"), [(True, "already used"), (False, "link expired")])
def test_stale_oauth_callback_returns_browser_to_connections(db, settings, consumed, message):
    settings = configured(settings)
    with TestClient(create_app(settings), base_url=settings.app_base_url) as browser:
        csrf = browser.get("/api/session").json()["csrf"]
        handoff = browser.post("/oauth/x/connect", data={"csrf": csrf})
        state = parse_qs(urlparse(handoff.context["authorize_url"]).query)["state"][0]
        record = db.get(OAuthState, digest(state))
        record.consumed = consumed
        record.expires_at = now() - timedelta(seconds=1)
        db.commit()

        params = {"code": "private-fixture-code", "state": state}
        response = browser.get("/oauth/x/callback", params=params, headers={"Accept": "text/html"})
        assert response.history[0].status_code == 303
        assert response.history[0].headers["referrer-policy"] == "no-referrer"
        assert response.url == settings.app_base_url + "/admin/connections"
        assert response.status_code == 200 and message in response.text
        assert state not in response.text and params["code"] not in response.text
        assert db.get(Account, "x").validated_at is None

        browser.cookies.clear()
        unmatched = browser.get("/oauth/x/callback", params=params, headers={"Accept": "application/json"})
        assert unmatched.status_code == 400 and "browser session" in unmatched.json()["detail"]


def test_x_rotating_refresh_token_persisted_encrypted(db, settings):
    settings = configured(settings)
    account = db.get(Account, "x")
    store_tokens(
        db,
        settings,
        account,
        {"access_token": "old-access", "refresh_token": "old-refresh", "expires_in": 10},
    )
    db.commit()

    def handler(request):
        assert "old-refresh" in request.content.decode()
        assert request.headers["authorization"].startswith("Basic ")
        return httpx.Response(
            200, json={"access_token": "new-access", "refresh_token": "rotated-refresh", "expires_in": 7200}
        )

    assert refresh_account(db, settings, "x", httpx.Client(transport=httpx.MockTransport(handler)))
    assert Vault(settings).decrypt(account.credentials)["refresh_token"] == "rotated-refresh"
    assert "rotated-refresh" not in account.credentials


def test_linkedin_refresh_expiry_not_extended(db, settings):
    a = db.get(Account, "linkedin")
    a.refresh_expires_at = now() + timedelta(days=10)
    previous = a.refresh_expires_at
    store_tokens(
        db,
        settings,
        a,
        {"access_token": "new", "expires_in": 100, "refresh_token_expires_in": 365 * 86400},
        refreshing=True,
    )
    assert a.refresh_expires_at == previous


def test_refresh_failure_pauses_alerts_preserves_token(db, settings):
    a = db.get(Account, "tiktok")
    store_tokens(db, settings, a, {"access_token": "old", "refresh_token": "old-refresh", "expires_in": 10})
    db.commit()
    old = a.credentials
    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(401, json={"error": "invalid_grant"}))
    )
    assert not refresh_account(db, settings, "tiktok", client)
    assert a.paused and a.credentials == old and a.auth_error
    assert db.scalar(select(Alert).where(Alert.kind == "auth_failure"))


def test_meta_validates_linkage_before_saving(db, settings):
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "id": "page",
                    "access_token": "page-token",
                    "instagram_business_account": {"id": "wrong"},
                },
            )
        )
    )
    with pytest.raises(ValueError):
        save_meta(db, settings, "system-token", "page", "ig", client)
    assert not db.get(Account, "facebook").credentials


def test_missing_config_cannot_start_oauth(db, settings):
    with pytest.raises(ValueError):
        begin(db, settings, "linkedin", "nonce")
