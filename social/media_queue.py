"""Current media attempts shown in Studio and used to gate recurring campaign batches."""

import hashlib
import json

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import load_only

from .db import now
from .human_review import UNPUBLISHED_STATES, delivery_reported, possible_delivery_requests
from .idea_plan import _platform_names
from .lifecycle import PostConflict
from .models import MediaCampaign, MediaJob, Post, PostRetry

DELIVERY_STATES = {"approved", "scheduled", "publishing", "awaiting_manual", "published"}
ATTENTION_STATES = {"held", "failed", "uncertain"}


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


def replaced_posts():
    original = MediaJob.brief[("replaces", "post_id")].as_string()
    return select(original).where(MediaJob.state != "cancelled", original.is_not(None)).correlate(None)


def current_attempts(*columns):
    return (
        select(*columns)
        .select_from(MediaJob)
        .outerjoin(Post, Post.id == MediaJob.post_id)
        .where(
            MediaJob.dismissed_at.is_(None),
            MediaJob.state != "cancelled",
            Post.status.is_(None) | (Post.status != "deleted"),
            MediaJob.post_id.is_(None) | MediaJob.post_id.not_in(replaced_posts()),
        )
    )


def unfinished():
    # Completed library-only work has no post. TikTok inbox delivery completes Studio's part.
    return (MediaJob.state != "complete") | Post.status.not_in(["published", "awaiting_manual", "deleted"])


def standalone_posts():
    return (
        Post.id.not_in(select(MediaJob.post_id).where(MediaJob.post_id.is_not(None)))
        & Post.id.not_in(replaced_posts())
        & ~select(PostRetry.original_post_id).where(PostRetry.original_post_id == Post.id).exists()
    )


def in_progress_count(db):
    jobs = current_attempts(func.count(MediaJob.id)).where(
        unfinished(), Post.status.is_(None) | Post.status.not_in(DELIVERY_STATES)
    )
    posts = select(func.count(Post.id)).where(
        standalone_posts(), Post.status.not_in(DELIVERY_STATES | {"deleted"})
    )
    return db.scalar(select(jobs.scalar_subquery() + posts.scalar_subquery()))


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


def producing_platforms(db, campaign):
    """Keep a stopped media attempt for recovery while scheduling the other destinations."""
    platforms = campaign.brief["platforms"]
    if len(platforms) == 1:
        return platforms
    stopped = set(db.scalars(current_attempts(MediaJob.platform).where(
        campaign_attempts(campaign.id), MediaJob.state.in_(["failed", "uncertain"])
    )))
    return [p for p in platforms if p not in stopped]


def batch_wait_reason(db, campaign_id, platforms=None):
    query = current_attempts(MediaJob.platform, MediaJob.state, Post.status).where(
        campaign_attempts(campaign_id), unfinished()
    )
    if platforms:
        # Skip only stopped renderers on other destinations. Jobs still running under an older
        # campaign configuration, and posts awaiting a delivery decision, keep their usual gates.
        query = query.where(MediaJob.platform.in_(platforms) | MediaJob.state.not_in(["failed", "uncertain"]))
    campaign = db.get(MediaCampaign, campaign_id)
    if campaign and campaign.brief.get("rights") == "not_public":
        # Review and delivery of a simulation can continue independently of the next media batch.
        query = query.where((MediaJob.state != "complete") | Post.simulation_only.is_(False))
    blocker = db.execute(query.order_by(MediaJob.created_at, MediaJob.id).limit(1)).first()
    if not blocker:
        return ""
    platform, state, post_status = blocker
    name = _platform_names([platform])
    if state != "complete":
        return f"The {name} media job in the previous batch is {state}."
    if post_status == "failed":
        return f"The {name} post in the previous batch failed. Retry it or close the failed attempt."
    return f"The {name} post in the previous batch is {post_status.replace('_', ' ')}."


def campaign_wait_reason(db, campaign, platforms=None):
    if platforms is None:
        platforms = producing_platforms(db, campaign)
    reason = batch_wait_reason(db, campaign.id, platforms)
    if reason:
        return reason
    idea = db.execute(
        current_attempts(MediaJob.platform, MediaJob.state)
        .where(
            MediaJob.platform.in_(platforms),
            MediaJob.brief["workflow"].as_string().in_(["next", "immediate"]),
            MediaJob.state != "complete",
        )
        .order_by(MediaJob.created_at, MediaJob.id)
        .limit(1)
    ).first()
    if idea:
        return f"Waiting for the admin's requested post idea on {_platform_names([idea.platform])} ({idea.state})."
    return ""


def recent_media_jobs(db, campaign_id=None, *, include_review=True):
    excluded = DELIVERY_STATES if include_review else DELIVERY_STATES | {"awaiting_review"}
    query = (
        current_attempts(MediaJob, Post, Post.id.in_(possible_delivery_requests()))
        .options(load_only(Post.status, Post.remote_id, Post.published_at, Post.live_url, Post.error))
        .where(Post.status.is_(None) | Post.status.not_in(excluded))
    )
    if campaign_id:
        query = query.where(campaign_attempts(campaign_id), unfinished()).order_by(MediaJob.created_at)
    else:
        query = query.order_by(case((unfinished(), 0), else_=1), MediaJob.created_at.desc())
        if not include_review:
            library_history = (
                select(MediaJob.id)
                .where(
                    MediaJob.state == "complete", MediaJob.post_id.is_(None), MediaJob.dismissed_at.is_(None)
                )
                .order_by(MediaJob.created_at.desc(), MediaJob.id)
                .limit(100)
            )
            query = query.where(unfinished() | MediaJob.id.in_(library_history))
    query = query.order_by(MediaJob.id)
    if include_review:
        query = query.limit(100)
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


def queue_posts(*columns, campaign_id=None, review_only=False):
    """Every reviewable post, plus drafts without media jobs that need their own card."""
    current = current_attempts(MediaJob.post_id)
    if campaign_id:
        current = current.where(campaign_attempts(campaign_id))
    visible = (Post.status == "awaiting_review") & Post.id.in_(current)
    if not campaign_id:
        states = (
            Post.status == "awaiting_review"
            if review_only
            else Post.status.not_in(DELIVERY_STATES | {"deleted"})
        )
        visible |= standalone_posts() & states
    return select(*columns).where(visible).order_by(Post.created_at, Post.id)


def job_progress(job, post_status, can_delete):
    return {
        "id": job.id,
        "platform": job.platform,
        "state": job.state,
        "stage": job.stage,
        "generation_phase": job.brief.get("active_generation", ""),
        "shared_render_from": job.brief.get("shared_render_from", ""),
        "task_id": job.task_id,
        "retry_count": len(job.brief.get("failed_tasks", [])),
        "saved_scenes": sum(h.get("stage") == "scene" for h in job.task_history),
        "error": job.error,
        "asset_id": job.output_asset_id,
        "post_id": job.post_id,
        "post_status": post_status,
        "can_delete_draft": can_delete,
        "completed_clips": sum(bool(s.get("output_asset_id")) for s in job.brief.get("video_segments", [])),
    }


def queue_contents(db, campaign_id=None):
    jobs = recent_media_jobs(db, campaign_id, include_review=False)
    post_query = queue_posts(
        Post.id,
        Post.campaign,
        Post.platform,
        Post.status,
        Post.remote_id,
        Post.published_at,
        Post.live_url,
        Post.error,
        Post.id.in_(possible_delivery_requests()).label("delivery_request"),
        campaign_id=campaign_id,
    )
    sections = {name: {"jobs": [], "posts": []} for name in ("review", "attention", "generation")}
    for job, status, can_delete in jobs:
        section = "attention" if job.state in ATTENTION_STATES or status in ATTENTION_STATES else "generation"
        sections[section]["jobs"].append((job, status, can_delete))
    post_updates = []
    for post in db.execute(post_query):
        section = (
            "review"
            if post.status == "awaiting_review"
            else "attention"
            if post.status in ATTENTION_STATES
            else "generation"
        )
        can_delete = post.status in UNPUBLISHED_STATES and not (
            delivery_reported(post) or post.delivery_request
        )
        sections[section]["posts"].append((post, can_delete))
        post_updates.append((post.id, post.status, post.campaign, post.platform, post.error, can_delete))
    waiting = 0
    if campaign_id:
        waiting = db.scalar(
            current_attempts(func.count(MediaJob.id)).where(
                campaign_attempts(campaign_id), Post.status.in_(DELIVERY_STATES), unfinished()
            )
        )
    snapshot = [[job_progress(*row) for row in jobs], post_updates, waiting]
    return {
        "queue_sections": sections,
        "visible_ids": {job.id for job, _, _ in jobs},
        "queue_revision": hashlib.sha256(json.dumps(snapshot).encode()).hexdigest(),
        "waiting_delivery": waiting,
    }
