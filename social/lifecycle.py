from sqlalchemy import select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm.attributes import set_committed_value

from .models import Post, Transition

ALLOWED = {
    "draft": {"generated", "held", "deleted"},
    "generated": {"checked", "held"},
    "checked": {"approved", "held"},
    "held": {"draft", "generated", "failed"},
    "approved": {"scheduled", "held"},
    "scheduled": {"publishing", "held"},
    "publishing": {"published", "failed", "awaiting_manual", "held"},
    "awaiting_manual": {"published", "failed"},
    "failed": set(),
    "published": set(),
    "deleted": set(),
}


class PostConflict(ValueError):
    """Another transaction is processing this post or has already advanced it."""


class PostBusy(PostConflict):
    """Another transaction is already performing work on this post."""


def lock_busy(exc):
    code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    sqlite_code = getattr(exc.orig, "sqlite_errorcode", 0) or 0
    return code == "55P03" or sqlite_code & 0xFF in {5, 6}


def post_processing(engine, post_id):
    """Probe the existing operation lock without waiting or modifying the post."""
    with engine.connect() as connection:
        sqlite = engine.dialect.name == "sqlite"
        timeout = connection.scalar(text("PRAGMA busy_timeout")) if sqlite else None
        try:
            if sqlite:
                connection.execute(text("PRAGMA busy_timeout=0"))
                connection.execute(update(Post).where(Post.id == post_id).values(status=Post.status))
            else:
                connection.execute(select(Post.id).where(Post.id == post_id).with_for_update(nowait=True))
            return False
        except OperationalError as exc:
            if lock_busy(exc):
                return True
            raise
        finally:
            connection.rollback()
            if sqlite:
                connection.execute(text(f"PRAGMA busy_timeout={int(timeout)}"))


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
        if lock_busy(exc):
            db.rollback()
            raise PostBusy(
                "This post is being processed. Its progress will update here automatically."
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
