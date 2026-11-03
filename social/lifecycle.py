from sqlalchemy import update

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


def transition(db, post, target, reason, actor="pipeline"):
    before = post.status
    if target not in ALLOWED[before] or (target == "awaiting_manual" and post.platform != "tiktok"):
        raise ValueError(f"Illegal lifecycle transition {before} -> {target}")
    changed = db.execute(update(Post).where(Post.id == post.id, Post.status == before).values(status=target))
    if changed.rowcount != 1:
        raise ValueError("Post changed concurrently; refresh and retry")
    post.status = target
    db.add(Transition(post_id=post.id, from_status=before, to_status=target, reason=reason, actor=actor))
    db.flush()
