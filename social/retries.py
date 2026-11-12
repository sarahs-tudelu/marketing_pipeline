"""Explicit retries preserve the failed attempt and never replay uncertain delivery."""

from copy import deepcopy

from sqlalchemy import select, update

from .lifecycle import lock_post, transition
from .models import Account, Control, MediaJob, Post, PostRetry, RequestRecord
from .monitoring import event


def original_post(db, post):
    return db.scalar(select(Post).join(PostRetry, PostRetry.original_post_id == Post.id).where(
        PostRetry.retry_post_id == post.id,
    ))


def block_reason(db, settings, post):
    if post.status != "failed":
        return "Only failed posts can be retried."
    if post.remote_id or post.published_at or post.live_url:
        return "The platform has a delivery record. Check its status before taking further action; retry could duplicate it."
    if post.error.startswith("ambiguous:"):
        return "Publication is unconfirmed. Reconcile the platform result before retrying to avoid a duplicate."
    records = db.scalars(select(RequestRecord).where(RequestRecord.post_id == post.id)).all()
    if any((r.irreversible or r.step in {"publish", "inbox-init"}) and r.state not in {"rejected", "retryable"} for r in records):
        return "A publication or inbox delivery may already have been accepted. Reconcile it with the platform before retrying."
    if not post.caption or not post.parts or not post.asset_ids:
        return "This post needs a caption and media before it can be retried. Create a new draft with the corrected content."
    control = db.get(Control, 1)
    account = db.get(Account, post.platform)
    if control.killed:
        return "Resume the pipeline before retrying."
    if account.paused:
        return "Correct the platform connection or policy issue, then resume this platform in Overview before retrying."
    if not post.dry_run:
        if post.account_id != account.external_id:
            return "The connected account changed. Reconnect the original account or create a new post for the new account."
        from .adapters import adapter

        selected = adapter(settings, db, post)
        try:
            if selected.dry:
                return "Reconnect the original account and enable live posting before retrying this live post."
        finally:
            selected.client.close()
    return ""


def retry_post(db, settings, post, actor):
    lock_post(db, post)
    previous = db.get(PostRetry, post.id)
    if previous:
        return db.get(Post, previous.retry_post_id)
    reason = block_reason(db, settings, post)
    if reason:
        raise ValueError(reason)
    from .pipeline import draft

    retry = draft(db, post.platform, post.campaign, deepcopy(post.asset_ids), post.facts_id,
                  post.product, post.kind, commit=False)
    for field in ("caption", "parts", "fact_ids", "ai_disclosure", "dry_run", "account_id"):
        setattr(retry, field, deepcopy(getattr(post, field)))
    db.add(PostRetry(original_post_id=post.id, retry_post_id=retry.id, actor=actor))
    transition(db, retry, "generated", f"Retry requested for failed post {post.id}; reused caption and media, fresh checks required", actor)
    # Campaigns and Media Studio follow the new attempt while the original keeps its audit.
    db.execute(update(MediaJob).where(MediaJob.post_id == post.id).values(post_id=retry.id))
    db.commit()
    event("post_retry_queued", original_post_id=post.id, retry_post_id=retry.id, actor=actor)
    return retry
