from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from social import crm_access
from social.app import create_app
from social.media_studio import process_jobs
from social.models import FactSheet, MediaJob, Post


def test_crm_editor_workflow_and_isolation(db, settings, assets, monkeypatch):
    settings.auth_mode = "crm"
    settings.crm_access_url = "https://crm.example/api/auth/social-studio/access"
    members = {"alice": (12, "Alice", "editor"), "bob": (24, "Bob", "editor"), "admin": (33, "Ari", "admin")}

    async def verify(_client, url, *, cookies):
        assert url == settings.crm_access_url and set(cookies) == {"TUDELU_TOKEN"}
        user = members.get(cookies["TUDELU_TOKEN"])
        return httpx.Response(200 if user else 403, request=httpx.Request("GET", url), json=(
            dict(zip(("id", "label", "role"), user)) if user else {}))

    monkeypatch.setattr(httpx.AsyncClient, "get", verify)
    with TestClient(create_app(settings)) as c:
        assert c.get("/admin/create", headers={"X-Authenticated-User": "admin"}).status_code == 401
        def switch(name):
            c.cookies.set("TUDELU_TOKEN", name)
            return c.get("/api/session").json()["csrf"]

        form = {"prompt": "Canopy idea from Alice", "platforms": "instagram", "rights": "public",
                "facts_id": db.scalar(select(FactSheet.id)), "campaign": "Alice canopy", "operation": "image",
                "prompted_by": "crm:24", "prompted_by_label": "Bob", "mode": "once", "plan_kind": "promotional"}
        form["csrf"] = switch("alice")
        assert c.get("/").url.path == "/admin/create"
        plan = c.post("/forms/ideas/plan", data=form)
        assert plan.status_code == 200 and "Review your idea" in plan.text
        submitted = c.post("/forms/ideas/create", data=form)
        assert submitted.status_code == 200 and "Alice canopy" in submitted.text
        job = db.scalar(select(MediaJob))
        assert job.brief["prompted_by"] == "crm:12"
        for _ in range(3):
            process_jobs(db, settings)
        db.refresh(job)
        post = db.get(Post, job.post_id)
        assert post and (post.prompted_by, post.prompted_by_label) == ("crm:12", "Alice")
        detail = f"/posts/{post.id}"
        asset = detail + "/media/" + post.asset_ids[0]
        assert c.get(asset).status_code == 200
        assert "Prompted by Alice" in c.get(detail).text
        assert [p["id"] for p in c.get("/api/posts").json()] == [post.id]
        assert c.post("/forms/posts/" + post.id + "/approve", data=form).status_code == 403
        for url in ("/admin/assets", "/admin/connections", "/api/media-jobs", "/admin/media-queue"):
            assert c.get(url).status_code == 403
        form["csrf"] = switch("bob")
        assert "Alice canopy" not in c.get("/admin/create").text
        assert c.get("/api/posts").json() == []
        for url in (detail, asset, "/admin/create?from_post=" + post.id):
            assert c.get(url).status_code == 404
        for url in ("/forms/ideas/plan", "/forms/ideas/create"):
            assert c.post(url, data=form | {"reference_ids": post.asset_ids[0]}).status_code == 404
        del members["bob"]
        assert c.get("/admin/create").status_code == 403
        switch("admin")
        assert c.get("/admin/connections").status_code == 200
        assert "Prompted by Alice" in c.get(detail).text


def test_scope_shared_assets():
    private_upload = SimpleNamespace(tags={"uploaded_with_idea": True, "submitted_by": "crm:3"},
                                     source="company", rights="public")
    assert crm_access.editor_asset(private_upload, "crm:3")
    assert not crm_access.editor_asset(private_upload, "crm:4")


@pytest.mark.parametrize("body", [{"id": "3", "role": "admin", "label": "a"}, {"role": "admin"}, []])
def test_invalid_crm_identity_fails_closed(settings, monkeypatch, body):
    import asyncio

    from fastapi import HTTPException

    async def invalid(_client, url, **kwargs):
        return httpx.Response(200, json=body, request=httpx.Request("GET", "https://crm.example"))

    monkeypatch.setattr(httpx.AsyncClient, "get", invalid)
    with pytest.raises(HTTPException) as error:
        asyncio.run(crm_access.identity(settings, "session"))
    assert error.value.status_code == 503
