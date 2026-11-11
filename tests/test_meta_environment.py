import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from social.app import create_app
from social.connections import import_meta_from_env
from social.models import Account, Alert
from social.security import Vault


def configured(settings, **changes):
    return settings.model_copy(
        update={
            "meta_system_user_token": SecretStr("system-secret"),
            "meta_page_id": "page",
            "instagram_user_id": "instagram",
        }
        | changes
    )


def meta_response(request):
    assert request.method == "GET"
    assert "access_token" not in request.url.params
    if "instagram_business_account" in request.url.params.get("fields", ""):
        assert request.headers["authorization"] == "Bearer system-secret"
        return httpx.Response(
            200,
            json={
                "id": "page",
                "access_token": "page-secret",
                "instagram_business_account": {"id": "instagram"},
            },
        )
    assert request.headers["authorization"] == "Bearer page-secret"
    return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[1]})


def test_startup_import_is_encrypted_idempotent_and_preserves_pauses(db, settings):
    settings = configured(settings)
    db.get(Account, "facebook").paused = True
    db.commit()
    calls = []

    def handler(request):
        calls.append(request)
        return meta_response(request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert import_meta_from_env(db, settings, client)
        first = db.get(Account, "facebook").credentials
        assert import_meta_from_env(db, settings, client)
    assert len(calls) == 3
    assert db.get(Account, "facebook").credentials == first
    assert db.get(Account, "facebook").paused
    assert settings.live_platforms == ""
    for platform in ("facebook", "instagram"):
        a = db.get(Account, platform)
        assert a.validated_at and not a.auth_error
        assert "secret" not in a.credentials
        assert Vault(settings).decrypt(a.credentials)["access_token"] == "page-secret"


def test_changed_environment_revalidates_both_accounts(db, settings):
    settings = configured(settings)
    with httpx.Client(transport=httpx.MockTransport(meta_response)) as client:
        assert import_meta_from_env(db, settings, client)
    settings.meta_system_user_token = SecretStr("rotated-system-secret")
    calls = []

    def rotated(request):
        calls.append(request)
        if "instagram_business_account" in request.url.params.get("fields", ""):
            assert request.headers["authorization"] == "Bearer rotated-system-secret"
            return httpx.Response(
                200,
                json={
                    "id": "page",
                    "access_token": "new-page-secret",
                    "instagram_business_account": {"id": "instagram"},
                },
            )
        assert request.headers["authorization"] == "Bearer new-page-secret"
        return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[1]})

    with httpx.Client(transport=httpx.MockTransport(rotated)) as client:
        assert import_meta_from_env(db, settings, client)
    assert len(calls) == 3
    assert (
        Vault(settings).decrypt(db.get(Account, "instagram").credentials)["system_token"]
        == "rotated-system-secret"
    )


@pytest.mark.parametrize("failure", ["rejected", "linkage", "network", "malformed"])
def test_failed_auto_import_is_visible_redacted_and_does_not_use_old_account(db, settings, failure):
    settings = configured(settings)
    with httpx.Client(transport=httpx.MockTransport(meta_response)) as client:
        assert import_meta_from_env(db, settings, client)
    settings.meta_page_id = "changed-page"
    old_credentials = db.get(Account, "facebook").credentials

    def fail(request):
        if failure == "network":
            raise httpx.ConnectError("system-secret private failure")
        if failure == "rejected":
            return httpx.Response(401, json={"error": "system-secret private failure"})
        if failure == "malformed":
            return httpx.Response(200, content=b"system-secret private failure")
        return httpx.Response(
            200,
            json={
                "id": "changed-page",
                "access_token": "page-secret",
                "instagram_business_account": {"id": "wrong"},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(fail)) as client:
        assert not import_meta_from_env(db, settings, client)
    assert db.get(Account, "facebook").credentials == old_credentials
    for platform in ("facebook", "instagram"):
        account = db.get(Account, platform)
        assert not account.validated_at and account.auth_error
        assert "secret" not in account.auth_error and "private failure" not in account.auth_error
    alert = db.scalar(select(Alert).where(Alert.kind == "meta_connection"))
    assert alert and "system-secret" not in alert.message


def test_empty_partial_and_disconnected_configuration_do_not_contact_meta(db, settings):
    def no_network(request):
        raise AssertionError("Should not contact Meta")

    with httpx.Client(transport=httpx.MockTransport(no_network)) as client:
        assert not import_meta_from_env(db, settings, client)
        assert not db.scalar(select(Alert))
        partial = configured(settings, instagram_user_id="")
        assert not import_meta_from_env(db, partial, client)
        assert "INSTAGRAM_USER_ID" in db.get(Account, "facebook").auth_error
        db.get(Account, "facebook").auth_error = "Disconnected"
        db.commit()
        assert not import_meta_from_env(db, configured(settings), client)
        assert db.get(Account, "facebook").auth_error == "Disconnected"


def test_web_startup_imports_and_retry_button_uses_environment(db, settings, monkeypatch):
    import social.connections as connections

    settings = configured(settings)
    real_save = connections.save_meta
    calls = []
    with httpx.Client(transport=httpx.MockTransport(meta_response)) as upstream:

        def save(db, settings, token, page_id, instagram_id, client=None):
            calls.append(True)
            return real_save(db, settings, token, page_id, instagram_id, client=upstream)

        monkeypatch.setattr(connections, "save_meta", save)
        with TestClient(create_app(settings)) as client:
            db.expire_all()
            assert db.get(Account, "facebook").validated_at
            assert db.get(Account, "instagram").validated_at
            page = client.get("/admin/connections")
            assert page.status_code == 200 and "Connect from environment" in page.text
            assert "system-secret" not in page.text and "page-secret" not in page.text
            assert client.post("/forms/import-meta").status_code == 403
            response = client.post(
                "/forms/import-meta", data={"csrf": client.get("/api/session").json()["csrf"]}
            )
            assert response.status_code == 200
    assert len(calls) == 2
