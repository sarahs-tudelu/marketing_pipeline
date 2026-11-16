"""Human decisions bind to the exact media, caption, facts and usage permissions."""

from sqlalchemy import select, update

from .facts import content_digest
from .lifecycle import lock_post, transition
from .models import Account, Asset, Control, RequestRecord


def human_approval_current(db, post):
    return not post.human_review_required or (
        bool(post.human_approved_digest)
        and post.human_approved_digest == content_digest(post, [db.get(Asset, i) for i in post.asset_ids])
    )


def lock_delivery(db):
    # Same lock order as the scheduler and publisher: control first, then post.
    db.execute(update(Control).where(Control.id == 1).values(revision=Control.revision + 1))


def require_unpublished(db, post):
    if post.status not in {"draft", "generated", "held", "awaiting_review", "approved", "scheduled"}:
        raise ValueError("This post has started delivery or is closed; it cannot be stopped or deleted here.")
    if post.remote_id or post.published_at or post.live_url or post.error.startswith("ambiguous:"):
        raise ValueError("Delivery may have started. Reconcile the platform result before changing this post.")
    records = db.scalars(select(RequestRecord).where(RequestRecord.post_id == post.id)).all()
    if any((r.irreversible or r.step in {"publish", "inbox-init"}) and r.state not in {"rejected", "retryable"} for r in records):
        raise ValueError("Delivery may have started. Reconcile its request record before changing this post.")


def require_no_replacement(db, post):
    from .clip_replace import replacement

    if replacement(db, post):
        raise ValueError("A replacement is already in progress. Review that result, or cancel its generation first.")


def approve(db, settings, post, actor, reviewed_digest):
    from .gate import run_gate

    lock_post(db, post)
    if post.status != "awaiting_review":
        raise ValueError("Run the automated checks first, then review the completed media and caption.")
    require_unpublished(db, post)
    require_no_replacement(db, post)
    if not reviewed_digest or reviewed_digest != content_digest(post, [db.get(Asset, i) for i in post.asset_ids]):
        raise ValueError("This post changed since you opened it. Refresh and review the current media and caption.")
    transition(db, post, "generated", "Human requested approval; rerunning checks", actor)
    return run_gate(db, settings, post, human_actor=actor,
                    live=settings.wants_live(post.platform, db.get(Account, post.platform)))


def stop(db, post, actor):
    lock_delivery(db)
    lock_post(db, post)
    require_unpublished(db, post)
    if post.status not in {"approved", "scheduled"}:
        raise ValueError("Only approved or scheduled posts need delivery stopped.")
    post.human_review_required, post.human_approved_digest = True, ""
    post.scheduled_at, post.next_attempt_at = None, None
    transition(db, post, "awaiting_review", "Stopped by user before scheduled delivery; human approval required to resume", actor)
    db.commit()
