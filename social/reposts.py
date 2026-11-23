"""A requested replacement is a new reviewed post, never a replay of a public write."""

from copy import deepcopy

from sqlalchemy import select

from .availability import check_post, observe
from .delivery_privacy import requires_simulation
from .human_review import lock_delivery
from .lifecycle import lock_post, transition
from .models import Post, PostRepost, PostRetry


def existing(db, original):
    return db.scalar(
        select(Post)
        .join(PostRepost, PostRepost.repost_post_id == Post.id)
        .where(
            PostRepost.original_post_id == original.id,
            Post.status != "deleted",
        )
        .order_by(PostRepost.id.desc())
        .limit(1)
    )


def sources(db, post):
    """Follow explicit repost/retry links; unrelated similar posts are never excluded."""
    result, seen, current = [], {post.id}, post
    while True:
        repost = db.scalar(select(PostRepost).where(PostRepost.repost_post_id == current.id))
        retry = db.scalar(select(PostRetry).where(PostRetry.retry_post_id == current.id))
        original_id = repost.original_post_id if repost else retry.original_post_id if retry else None
        if not original_id:
            return result
        if original_id in seen or len(seen) > 100:
            raise ValueError("Invalid repost history")
        seen.add(original_id)
        current = db.get(Post, original_id)
        if current is None:
            raise ValueError("Original post is missing from the audit history")
        if repost:
            result.append(current)


def create(db, settings, original, actor, confirmed_removed=False, client=None):
    lock_delivery(db)
    lock_post(db, original)
    db.refresh(original, ["simulation_only"])
    original.simulation_only = requires_simulation(db, original)
    if prior := existing(db, original):
        return prior
    if original.status != "published" or original.dry_run or not original.remote_id:
        raise ValueError("Only a confirmed live publication can be reposted. Use Retry for failed delivery.")
    if not confirmed_removed:
        raise ValueError(
            "Open the original post and confirm it is no longer available before preparing a repost."
        )
    current = check_post(db, settings, original, actor, client, commit=False)
    if current.state != "unavailable":
        db.commit()  # Keep the fresh evidence, including an account or network failure.
        raise ValueError("The original post is available or could not be verified. A repost was not created.")
    from .pipeline import draft

    post = draft(
        db,
        original.platform,
        original.campaign,
        deepcopy(original.asset_ids),
        original.facts_id,
        original.product,
        original.kind,
        commit=False,
        human_review_required=True,
    )
    for name in ("prompted_by", "prompted_by_label", "caption", "parts", "fact_ids", "ai_disclosure", "account_id", "simulation_only"):
        setattr(post, name, deepcopy(getattr(original, name)))
    post.dry_run = original.dry_run or original.simulation_only
    db.add(PostRepost(original_post_id=original.id, repost_post_id=post.id, actor=actor))
    transition(
        db,
        post,
        "generated",
        f"Repost requested for unavailable post {original.id}; original removal confirmed by user; fresh checks and human approval required",
        actor,
    )
    db.commit()
    return post


def exclusions(db, settings, post, client=None):
    """Fresh read before every gate, including delivery; never exempt a restored original."""
    allowed, observations = set(), []
    for original in sources(db, post):
        if original.platform != post.platform or original.account_id != post.account_id:
            observations.append(
                {"post_id": original.id, "state": "unknown", "reason": "Original account changed"}
            )
            continue
        result = observe(db, settings, original, client)
        observations.append({"post_id": original.id, "state": result.state, "reason": result.reason})
        if result.state == "unavailable":
            allowed.add(original.id)
    return allowed, observations
