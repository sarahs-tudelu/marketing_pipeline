"""Current media attempts shown in Studio and used to gate recurring campaign batches."""

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import load_only

from .db import now
from .human_review import UNPUBLISHED_STATES, delivery_reported, possible_delivery_requests
from .idea_plan import _platform_names
from .lifecycle import PostConflict
from .models import MediaJob, Post


def dismiss_completed(db, job, post_id):
    # A delivery retry can reassign this job to a new post while the old card is open.
    closed_posts = select(Post.id).where(Post.status.in_(["failed", "published"]))
    changed = db.execute(
        update(MediaJob)
        .where(
            MediaJob.id == job.id,
            MediaJob.state == "complete",
            MediaJob.post_id == post_id,
            MediaJob.post_id.is_(None) | MediaJob.post_id.in_(closed_posts),
        )
        .values(dismissed_at=job.dismissed_at or now()),
        execution_options={"synchronize_session": False},
    )
    if not changed.rowcount:
        raise PostConflict("This job or its post has changed. Review its current status before removing it.")


def current_attempts(*columns):
    original = MediaJob.brief[("replaces", "post_id")].as_string()
    replaced = select(original).where(MediaJob.state != "cancelled", original.is_not(None)).correlate(None)
    return (
        select(*columns)
        .select_from(MediaJob)
        .outerjoin(Post, Post.id == MediaJob.post_id)
        .where(
            MediaJob.dismissed_at.is_(None),
            MediaJob.state != "cancelled",
            Post.status.is_(None) | (Post.status != "deleted"),
            MediaJob.post_id.is_(None) | MediaJob.post_id.not_in(replaced),
        )
    )


def unfinished():
    # Completed library-only work has no post. TikTok inbox delivery completes Studio's part.
    return (MediaJob.state != "complete") | Post.status.not_in(["published", "awaiting_manual", "deleted"])


def unfinished_job_count(db):
    return db.scalar(current_attempts(func.count(MediaJob.id)).where(unfinished()))


def campaign_attempts(campaign_id):
    # Revisions are separate jobs without a campaign_id. Follow the post being replaced,
    # since several platforms can share one rendering job's output.
    attempts = (
        select(MediaJob.id, MediaJob.post_id).where(MediaJob.campaign_id == campaign_id).cte(recursive=True)
    )
    children = (
        select(MediaJob.id, MediaJob.post_id)
        .join(attempts, MediaJob.brief[("replaces", "post_id")].as_string() == attempts.c.post_id)
        .where(MediaJob.state != "cancelled")
    )
    attempts = attempts.union(children)
    return MediaJob.id.in_(select(attempts.c.id))


def batch_wait_reason(db, campaign_id):
    blocker = db.execute(
        current_attempts(MediaJob.platform, MediaJob.state, Post.status)
        .where(campaign_attempts(campaign_id), unfinished())
        .order_by(MediaJob.created_at, MediaJob.id)
        .limit(1)
    ).first()
    if not blocker:
        return ""
    platform, state, post_status = blocker
    name = _platform_names([platform])
    if state != "complete":
        return f"The {name} media job in the previous batch is {state}."
    if post_status == "failed":
        return f"The {name} post in the previous batch failed. Retry it or close the failed attempt."
    return f"The {name} post in the previous batch is {post_status.replace('_', ' ')}."


def campaign_wait_reason(db, campaign):
    reason = batch_wait_reason(db, campaign.id)
    if reason:
        return reason
    idea = db.execute(
        current_attempts(MediaJob.platform, MediaJob.state)
        .where(
            MediaJob.platform.in_(campaign.brief["platforms"]),
            MediaJob.brief["workflow"].as_string().in_(["next", "immediate"]),
            MediaJob.state != "complete",
        )
        .order_by(MediaJob.created_at, MediaJob.id)
        .limit(1)
    ).first()
    if idea:
        return f"Waiting for the admin's requested post idea on {_platform_names([idea.platform])} ({idea.state})."
    return ""


def recent_media_jobs(db, campaign_id=None):
    query = current_attempts(MediaJob, Post, Post.id.in_(possible_delivery_requests())).options(
        load_only(Post.status, Post.remote_id, Post.published_at, Post.live_url, Post.error)
    )
    if campaign_id:
        query = query.where(campaign_attempts(campaign_id), unfinished()).order_by(MediaJob.created_at)
    else:
        query = query.order_by(case((unfinished(), 0), else_=1), MediaJob.created_at.desc())
    query = query.order_by(MediaJob.id).limit(100)
    return [
        (
            job,
            post.status if post else None,
            bool(
                job.state == "complete"
                and post
                and post.status in UNPUBLISHED_STATES
                and not delivery_reported(post)
                and not delivery_request
            ),
        )
        for job, post, delivery_request in db.execute(query)
    ]
