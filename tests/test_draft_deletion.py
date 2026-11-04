from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from social.app import create_app
from social.facts import load_facts
from social.lifecycle import PostConflict
from social.models import Asset, Post, Transition
from social.pipeline import delete_draft, draft, generate
from social.storage import storage


def new_draft(db, settings, assets):
    facts = load_facts(db, settings.facts_path)
    return draft(db, "linkedin", "Delete test", [a.id for a in assets], facts.id, product="demo")


@pytest.mark.parametrize("via_form", [False, True])
def test_delete_hides_draft_and_retains_assets_and_audit(db, settings, assets, via_form):
    post = new_draft(db, settings, assets)
    original = storage(settings).read(assets[0].storage_key)
    with TestClient(create_app(settings)) as client:
        assert "Delete draft" in client.get(f"/posts/{post.id}").text
        csrf = client.get("/api/session").json()["csrf"]
        prefix = "forms" if via_form else "api"
        path = f"/{prefix}/posts/{post.id}/delete"
        assert client.post(path).status_code == 403
        response = client.post(
            path,
            data={"csrf": csrf} if via_form else None,
            headers={} if via_form else {"x-csrf-token": csrf},
            follow_redirects=False,
        )
        assert response.status_code == (303 if via_form else 200)
        if via_form:
            assert response.headers["location"] == "/"
        assert client.get(f"/posts/{post.id}").status_code == 404
        assert post.id not in client.get("/").text
        assert all(row["id"] != post.id for row in client.get("/api/posts").json())
        assert client.post(path, headers={"x-csrf-token": csrf}).status_code == 404
    db.refresh(post)
    assert post.status == "deleted"
    assert db.get(Asset, assets[0].id) is not None
    assert storage(settings).read(assets[0].storage_key) == original
    history = db.scalars(
        select(Transition).where(Transition.post_id == post.id).order_by(Transition.id)
    ).all()
    assert [row.to_status for row in history] == ["draft", "deleted"]
    assert history[-1].actor


@pytest.mark.parametrize("status", ["generated", "held", "approved", "scheduled", "publishing", "published"])
def test_only_drafts_can_be_deleted(db, settings, assets, status):
    post = new_draft(db, settings, assets)
    post.status = status
    db.commit()
    with TestClient(create_app(settings)) as client:
        csrf = client.get("/api/session").json()["csrf"]
        response = client.post(f"/api/posts/{post.id}/delete", headers={"x-csrf-token": csrf})
        assert response.status_code == 409
        assert "Only draft posts" in response.text
        assert "Delete draft" not in client.get(f"/posts/{post.id}").text
    db.refresh(post)
    assert post.status == status


def test_stale_generator_cannot_revive_deleted_draft(db, settings, assets):
    post = new_draft(db, settings, assets)
    with Session(db.get_bind(), expire_on_commit=False) as stale:
        selected = stale.get(Post, post.id)
        delete_draft(db, post, "test")
        provider = Mock()
        with pytest.raises(PostConflict):
            generate(stale, settings, selected, providers=provider)
        provider.caption.assert_not_called()
    db.refresh(post)
    assert post.status == "deleted"


def test_stale_delete_cannot_remove_generated_post(db, settings, assets):
    post = new_draft(db, settings, assets)
    with Session(db.get_bind(), expire_on_commit=False) as stale:
        selected = stale.get(Post, post.id)
        generate(db, settings, post)
        with pytest.raises(PostConflict):
            delete_draft(stale, selected, "test")
    db.refresh(post)
    assert post.status == "generated"
    assert post.caption
