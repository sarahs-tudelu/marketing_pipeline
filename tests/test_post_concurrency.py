from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from social.app import create_app
from social.facts import load_facts
from social.gate import run_gate
from social.lifecycle import PostConflict, lock_post, transition
from social.models import Alert, Post, Transition
from social.pipeline import draft, generate
from social.providers import Providers
from social.worker import advance_queue


def make_draft(db, settings, assets):
    facts = load_facts(db, settings.facts_path)
    return draft(db, "linkedin", "concurrent-test", [a.id for a in assets], facts.id, product="demo")


def test_two_generators_call_provider_once_and_do_not_hold_winner(db, settings, assets):
    post = make_draft(db, settings, assets)
    engine, post_id = db.get_bind(), post.id
    entered, release, contender_started = Event(), Event(), Event()
    calls = []

    class Slow(Providers):
        def caption(self, context):
            calls.append(context)
            entered.set()
            assert release.wait(5)
            return super().caption(context)

    def perform(contender=False):
        with Session(engine, expire_on_commit=False) as session:
            selected = session.get(Post, post_id)
            if contender:
                contender_started.set()
            generate(session, settings, selected, providers=Slow(settings))

    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(perform)
        assert entered.wait(5)
        contender = pool.submit(perform, True)
        try:
            assert contender_started.wait(5)
        finally:
            release.set()
        winner.result(timeout=5)
        with pytest.raises(PostConflict):
            contender.result(timeout=5)
    db.refresh(post)
    assert post.status == "generated"
    assert len(calls) == 1
    history = db.scalars(
        select(Transition).where(Transition.post_id == post.id).order_by(Transition.id)
    ).all()
    assert [row.to_status for row in history] == ["draft", "generated"]


def test_stale_transition_rolls_back_pending_caption_instead_of_overwriting(db, settings, assets):
    post = make_draft(db, settings, assets)
    with Session(db.get_bind(), expire_on_commit=False) as stale:
        outdated = stale.get(Post, post.id)
        generate(db, settings, post)
        winning_caption = post.caption
        outdated.caption = "Stale request must never replace the winning caption"
        with pytest.raises(PostConflict):
            transition(stale, outdated, "held", "Stale operation")
        stale.commit()  # Even a later commit cannot leak the losing pending changes.
    db.refresh(post)
    assert post.status == "generated"
    assert post.caption == winning_caption
    assert not db.scalar(
        select(Transition.id).where(Transition.post_id == post.id, Transition.to_status == "held")
    )


def test_stale_generation_is_rejected_before_paid_call(db, settings, assets):
    post = make_draft(db, settings, assets)
    with Session(db.get_bind(), expire_on_commit=False) as stale:
        outdated = stale.get(Post, post.id)
        generate(db, settings, post)
        provider = Mock()
        with pytest.raises(PostConflict):
            generate(stale, settings, outdated, providers=provider)
        provider.caption.assert_not_called()
    db.refresh(post)
    assert post.status == "generated"


def test_stale_gate_does_not_review_or_change_an_approved_post(db, settings, post):
    with Session(db.get_bind(), expire_on_commit=False) as stale:
        outdated = stale.get(Post, post.id)
        run_gate(db, settings, post)
        provider = Mock()
        with pytest.raises(PostConflict):
            run_gate(stale, settings, outdated, providers=provider)
        provider.review.assert_not_called()
    db.refresh(post)
    assert post.status == "approved"


def test_repeated_generate_form_shows_current_post_and_api_returns_conflict(db, settings, assets):
    post = make_draft(db, settings, assets)
    with TestClient(create_app(settings)) as client:
        csrf = client.get("/api/session").json()["csrf"]
        path = f"/forms/posts/{post.id}/generate"
        first = client.post(path, data={"csrf": csrf}, follow_redirects=False)
        assert first.status_code == 303
        repeated = client.post(path, data={"csrf": csrf})
        assert repeated.status_code == 409
        assert repeated.headers["content-type"].startswith("text/html")
        assert "already moved beyond draft" in repeated.text
        assert 'role="alert"' in repeated.text
        assert "Post workspace" in repeated.text
        assert "Generate caption</button>" not in repeated.text
        assert (
            client.post(f"/api/posts/{post.id}/generate", headers={"x-csrf-token": csrf}).status_code == 409
        )
    db.refresh(post)
    assert post.status == "generated"


def test_worker_skips_busy_post_without_false_scheduling_alert(db, settings, assets, monkeypatch):
    make_draft(db, settings, assets)
    monkeypatch.setattr("social.worker.generate", Mock(side_effect=PostConflict("Busy")))
    advance_queue(db, settings)
    assert db.scalars(select(Alert)).all() == []


@pytest.mark.parametrize("sqlstate", ["55P03", "08006"])
def test_postgresql_nowait_lock_conflict_is_distinct_from_database_failure(sqlstate):
    class DatabaseError(Exception):
        pass

    original = DatabaseError("Database error")
    original.sqlstate = sqlstate
    error = OperationalError("SELECT", {}, original)
    session = Mock()
    session.no_autoflush = nullcontext()
    session.get_bind.return_value.dialect.name = "postgresql"
    session.scalar.side_effect = error
    with pytest.raises(PostConflict if sqlstate == "55P03" else OperationalError):
        lock_post(session, SimpleNamespace(id="test-post", status="draft"))
    statement = session.scalar.call_args.args[0]
    assert "FOR UPDATE NOWAIT" in str(statement.compile(dialect=postgresql.dialect()))
    assert session.rollback.called == (sqlstate == "55P03")
