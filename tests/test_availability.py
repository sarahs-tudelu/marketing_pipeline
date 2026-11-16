import importlib
import json
from datetime import timedelta

import httpx
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select

from social.adapters import adapter
from social.adapters.base import BaseAdapter
from social.app import create_app
from social.availability import check_post, collect, observe
from social.connections import requested_scopes
from social.db import now
from social.models import Account, AvailabilityCheck, Post, PostAvailability, RequestRecord
from social.security import Vault


@pytest.fixture
def published(db, settings, post, monkeypatch):
    monkeypatch.setattr(BaseAdapter, "connected", lambda self: True)
    post.status, post.dry_run = "published", False
    post.remote_id, post.account_id = "12345", "owner"
    post.published_at = now() - timedelta(days=1)
    for platform in ("linkedin", "instagram", "facebook", "x", "tiktok", "pinterest"):
        account = db.get(Account, platform)
        account.external_id, account.validated_at = "owner", now()
        account.credentials = Vault(settings).encrypt({"access_token": "secret-test", "scope": "video.list"})
    db.commit()
    return post


def mock_platform(monkeypatch, handler):
    def selected(settings, db, post, client=None):
        return adapter(settings, db, post, client or httpx.Client(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr("social.availability.adapter", selected)


@pytest.mark.parametrize("platform", ["instagram", "facebook", "linkedin", "x", "pinterest", "tiktok"])
def test_platform_lookup_contract_and_saved_history(db, settings, published, monkeypatch, platform):
    published.platform = platform
    db.commit()
    calls = []

    def handle(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer secret-test"
        if platform == "tiktok":
            body = json.loads(request.content)
            assert request.method == "POST"
            if request.url.path.endswith("/status/fetch/"):
                assert body == {"publish_id": "12345"}
                return httpx.Response(
                    200,
                    json={"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": ["98765"]}},
                )
            assert request.url.path.endswith("/video/query/")
            assert body == {"filters": {"video_ids": ["98765"]}}
            return httpx.Response(200, json={"data": {"videos": [{"id": "98765"}]}, "error": {"code": "ok"}})
        assert request.method == "GET"
        if platform in {"facebook", "instagram"}:
            assert dict(request.url.params) == {"fields": "id"}
        if platform == "linkedin":
            assert request.headers["LinkedIn-Version"] == settings.linkedin_api_version
        return httpx.Response(200, json={"data": {"id": "12345"}} if platform == "x" else {"id": "12345"})

    mock_platform(monkeypatch, handle)
    state = check_post(db, settings, published)
    assert state.state == "available" and state.last_seen_at and state.next_check_at
    assert published.status == "published" and not published.dry_run
    assert not db.scalar(select(RequestRecord.id))
    assert db.scalar(select(AvailabilityCheck)).state == "available"
    if platform == "tiktok":
        assert state.public_ids == ["98765"]
        check_post(db, settings, published)
        assert sum(c.url.path.endswith("/status/fetch/") for c in calls) == 1


@pytest.mark.parametrize(
    "status,body,state",
    [
        (404, {}, "unavailable"),
        (410, {}, "unavailable"),
        (401, {"error": {"code": 190}}, "unknown"),
        (403, {}, "unknown"),
        (429, {}, "unknown"),
        (503, {}, "unknown"),
        (400, {"error": {"code": 100}}, "unknown"),
        (200, {}, "unknown"),
        (200, {"id": "different"}, "unknown"),
    ],
)
def test_missing_is_distinct_from_auth_permissions_service_and_malformed(
    db, settings, published, monkeypatch, status, body, state
):
    mock_platform(monkeypatch, lambda _: httpx.Response(status, json=body, headers={"retry-after": "30000"}))
    result = check_post(db, settings, published)
    assert result.state == state
    if status == 429:
        assert result.next_check_at >= result.checked_at + timedelta(seconds=30000)


@pytest.mark.parametrize("valid_account", [True, False])
def test_meta_missing_requires_original_account_access_and_does_not_claim_deleted(
    db, settings, published, monkeypatch, valid_account
):
    published.platform = "instagram"
    db.commit()

    def handle(request):
        if request.url.path.endswith("/12345"):
            return httpx.Response(400, json={"error": {"code": 100, "error_subcode": 33}})
        return (
            httpx.Response(200, json={"id": "owner"})
            if valid_account
            else httpx.Response(401, json={"error": {"code": 190}})
        )

    mock_platform(monkeypatch, handle)
    result = check_post(db, settings, published)
    assert result.state == ("unavailable" if valid_account else "unknown")
    assert result.reason != "Deleted"


def test_x_200_resource_not_found_is_unavailable(db, settings, published, monkeypatch):
    published.platform = "x"
    db.commit()
    mock_platform(
        monkeypatch,
        lambda _: httpx.Response(
            200,
            json={
                "errors": [
                    {"type": "https://api.x.com/2/problems/resource-not-found", "resource_id": "12345"}
                ]
            },
        ),
    )
    assert check_post(db, settings, published).state == "unavailable"


def test_tiktok_empty_query_is_unavailable_but_missing_scope_and_ids_are_unknown(
    db, settings, published, monkeypatch
):
    published.platform = "tiktok"
    row = PostAvailability(post_id=published.id, public_ids=["98765"])
    db.add(row)
    db.commit()
    mock_platform(
        monkeypatch, lambda _: httpx.Response(200, json={"data": {"videos": []}, "error": {"code": "ok"}})
    )
    assert check_post(db, settings, published).state == "unavailable"
    account = db.get(Account, "tiktok")
    account.credentials = Vault(settings).encrypt({"access_token": "secret-test", "scope": "video.upload"})
    db.commit()
    assert check_post(db, settings, published).state == "unknown"
    assert "video.list" in row.reason
    assert "video.list" in requested_scopes(settings, "tiktok")


def test_simulations_and_changed_accounts_never_query_platform(db, settings, published, monkeypatch):
    def forbidden(_):
        pytest.fail("Unexpected platform lookup")

    mock_platform(monkeypatch, forbidden)
    published.dry_run = True
    assert observe(db, settings, published).state == "simulated"
    published.dry_run = False
    published.account_id = "old-owner"
    assert observe(db, settings, published).state == "unknown"


def test_due_tracking_includes_old_posts_and_restoration_preserves_history(
    db, settings, published, monkeypatch
):
    published.published_at = now() - timedelta(days=400)
    db.commit()
    status = [200]
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status[0], json={"id": "12345"} if status[0] == 200 else {})

    mock_platform(monkeypatch, handle)
    collect(db, settings)
    row = db.get(PostAvailability, published.id)
    assert row.state == "available"
    last_seen = row.last_seen_at
    collect(db, settings)
    assert len(calls) == 1
    status[0] = 404
    row.next_check_at = now() - timedelta(seconds=1)
    db.commit()
    collect(db, settings)
    assert row.state == "unavailable" and row.unavailable_since and row.last_seen_at == last_seen
    status[0] = 200
    check_post(db, settings, published, actor="human")
    assert row.state == "available" and row.unavailable_since is None
    assert len(db.scalars(select(AvailabilityCheck)).all()) == 3
    assert published.status == "published"


def test_timeout_never_records_sensitive_error_or_triggers_repost(db, settings, published, monkeypatch):
    def handle(request):
        raise httpx.ReadTimeout("secret-test in https://api.example/?token=secret-test", request=request)

    mock_platform(monkeypatch, handle)
    row = check_post(db, settings, published)
    assert row.state == "unknown" and "secret-test" not in row.reason
    check = db.scalar(select(AvailabilityCheck))
    assert "secret-test" not in json.dumps(check.details)
    assert len(db.scalars(select(Post)).all()) == 1


def test_manual_check_form_api_and_attention_queue(db, settings, published, monkeypatch):
    mock_platform(monkeypatch, lambda _: httpx.Response(404, json={}))
    with TestClient(create_app(settings)) as client:
        before = client.get(f"/api/posts/{published.id}/activity").json()["revision"]
        token = client.get("/api/session").json()["csrf"]
        path = f"/forms/posts/{published.id}/availability"
        assert client.post(path).status_code == 403
        response = client.post(path, data={"csrf": token})
        assert response.status_code == 200
        assert "Unavailable on platform" in response.text and "Prepare repost for review" in response.text
        assert published.id in client.get("/admin/queues").text
        row = client.get("/api/posts").json()[0]
        assert row["status"] == "published" and row["availability"]["state"] == "unavailable"
        assert client.get(f"/api/posts/{published.id}/activity").json()["revision"] > before


def test_availability_migration_roundtrip_preserves_posts(db, published):
    migration = importlib.import_module("migrations.versions.d8e4b2c61709_post_availability")
    with Operations.context(MigrationContext.configure(db.connection())):
        migration.downgrade()
        migration.upgrade()
    db.commit()
    assert {"post_availability", "availability_checks", "post_reposts"} <= set(
        inspect(db.get_bind()).get_table_names()
    )
    assert db.get(Post, published.id).status == "published"
