from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm.attributes import set_committed_value

from .models import Post, Transition

ALLOWED = {
    "draft": {"generated", "held"},
    "generated": {"checked", "held"},
    "checked": {"approved", "held"},
    "held": {"draft", "generated", "failed"},
    "approved": {"scheduled", "held"},
    "scheduled": {"publishing", "held"},
    "publishing": {"published", "failed", "awaiting_manual", "held"},
    "awaiting_manual": {"published", "failed"},
    "failed": set(),
    "published": set(),
}


class PostConflict(ValueError):
    """Another transaction is processing this post or has already advanced it."""


def lock_post(db, post):
    """Own this post until commit/rollback, before any provider call or pending edit is flushed."""
    post_id, expected = post.id, post.status
    try:
        with db.no_autoflush:
            if db.get_bind().dialect.name == "sqlite":
                # SQLite ignores FOR UPDATE; a no-op write obtains its transaction write lock.
                db.execute(
                    update(Post).where(Post.id == post_id).values(status=Post.status),
                    execution_options={"synchronize_session": False},
                )
                current = db.scalar(select(Post.status).where(Post.id == post_id))
            else:
                current = db.scalar(
                    select(Post.status).where(Post.id == post_id).with_for_update(nowait=True)
                )
    except OperationalError as exc:
        code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
        sqlite_code = getattr(exc.orig, "sqlite_errorcode", 0) or 0
        if code == "55P03" or sqlite_code & 0xFF in {5, 6}:
            db.rollback()
            raise PostConflict(
                "This post is already being processed. Refresh its page in a moment."
            ) from None
        raise
    if current != expected:
        db.rollback()
        raise PostConflict("This post has changed. Refresh its page to see the latest status.")


def transition(db, post, target, reason, actor="pipeline"):
    before = post.status
    if target not in ALLOWED[before] or (target == "awaiting_manual" and post.platform != "tiktok"):
        raise ValueError(f"Illegal lifecycle transition {before} -> {target}")
    with db.no_autoflush:
        changed = db.execute(
            update(Post).where(Post.id == post.id, Post.status == before).values(status=target),
            execution_options={"synchronize_session": False},
        )
    if changed.rowcount != 1:
        db.rollback()
        raise PostConflict("This post has changed. Refresh its page to see the latest status.")
    set_committed_value(post, "status", target)
    db.add(Transition(post_id=post.id, from_status=before, to_status=target, reason=reason, actor=actor))
    db.flush()
