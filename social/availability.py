"""Read-only platform lookups; an unavailable object is not proof of deletion."""

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, or_, select

from .adapters import adapter
from .adapters.base import PublishError
from .db import now
from .lifecycle import PostConflict, lock_post
from .models import AvailabilityCheck, Post, PostAvailability
from .monitoring import event

LABELS = {
    "unchecked": "Not checked yet",
    "available": "Available on platform",
    "unavailable": "Unavailable on platform",
    "unknown": "Couldn't verify",
    "simulated": "Simulated",
}


@dataclass
class Observation:
    state: str
    reason: str
    details: dict = field(default_factory=dict)
    public_ids: list = field(default_factory=list)
    retry_after: int = 0


def unknown(reason, **details):
    return Observation("unknown", reason, details)


def unavailable(
    reason="The platform no longer returns this post. It may have been deleted or made private.", **details
):
    return Observation("unavailable", reason, details)


def _lookup(selected, post, public_ids):
    if post.platform == "tiktok":
        scope = selected.credentials.get("scope", "")
        scopes = set(scope.replace(",", " ").split()) if isinstance(scope, str) else set(scope)
        if "video.list" not in scopes:
            return unknown("Reconnect TikTok with video.list permission to check published videos.")
        if not public_ids:
            status = selected.get_status(post.remote_id)
            public_ids = [str(i) for i in status.get("data", {}).get("publicaly_available_post_id", [])]
        if not public_ids:
            return unknown(
                "TikTok has not returned the public video IDs. Inbox delivery alone does not confirm a public post."
            )
        response = selected.direct(
            "POST",
            f"{selected.base}/video/query/",
            params={"fields": "id"},
            json={"filters": {"video_ids": public_ids}},
        )
        videos = response.get("data", {}).get("videos")
        if not isinstance(videos, list):
            return unknown("TikTok returned an incomplete video lookup.")
        found = {str(v.get("id")) for v in videos}
        if found.intersection(public_ids):
            return Observation("available", "TikTok returned the published video.", public_ids=public_ids)
        return Observation(
            "unavailable",
            "TikTok no longer returns these videos; they may be deleted or private.",
            public_ids=public_ids,
        )

    # Minimal object fields avoid treating a missing optional permalink as a missing post.
    if post.platform in {"instagram", "facebook"}:
        result = selected.direct("GET", f"{selected.base}/{post.remote_id}", params={"fields": "id"})
    else:
        result = selected.get_status(post.remote_id)
    if not isinstance(result, dict):
        return unknown("The platform returned an incomplete lookup.")
    if post.platform == "x":
        if str((result.get("data") or {}).get("id", "")) == post.remote_id:
            return Observation("available", "The platform returned the published post.")
        if any(
            str(e.get("type", "")).endswith("/resource-not-found")
            and str(e.get("resource_id", e.get("value", ""))) == post.remote_id
            for e in result.get("errors", [])
        ):
            return unavailable(problem="resource-not-found")
        result = result.get("data") or {}
    if str(result.get("id", "")) != post.remote_id:
        return unknown("The platform did not return the requested post ID.")
    if post.platform == "linkedin" and result.get("lifecycleState", "PUBLISHED") != "PUBLISHED":
        return unavailable("LinkedIn reports that this post is no longer published.")
    return Observation("available", "The platform returned the published post.")


def observe(db, settings, post, client=None):
    """No database mutations or publication requests. Safe to use in the delivery gate."""
    if post.dry_run:
        return Observation("simulated", "This was a simulated delivery; there is no platform post to check.")
    if post.status != "published" or not post.remote_id:
        return unknown("Public publication has not been confirmed.")
    selected = None
    try:
        selected = adapter(settings, db, post, client)
        if post.account_id != selected.account.external_id:
            return unknown("The connected account differs from the account that published this post.")
        if not selected.connected():
            return unknown("Connect the original platform account to check this post.")
        saved = db.get(PostAvailability, post.id)
        return _lookup(selected, post, list(saved.public_ids or []) if saved else [])
    except PublishError as exc:
        details = {
            "http_status": getattr(exc, "http_status", None),
            "platform_code": getattr(exc, "platform_code", None),
            "platform_subcode": getattr(exc, "platform_subcode", None),
        }
        if exc.kind in {"auth", "policy"}:
            return unknown(
                "The platform denied access. Check the account login and read permissions.", **details
            )
        if exc.kind == "transient":
            return Observation(
                "unknown",
                "The platform is temporarily unavailable or rate limited; a later check will retry.",
                details,
                retry_after=exc.retry_after,
            )
        if details["http_status"] in {404, 410}:
            return unavailable(**details)
        if (
            post.platform in {"instagram", "facebook"}
            and details["platform_code"] == 100
            and details["platform_subcode"] == 33
        ):
            # Meta also uses this code for object-specific access restrictions. Do not call it deleted.
            try:
                account = selected.validate_credentials()
                if str(account.get("id", "")) != post.account_id:
                    return unknown("The original account could not be verified.", **details)
            except Exception:
                return unknown("Meta could not verify access to the original account.", **details)
            return unavailable(
                "Meta cannot return this post. It may be deleted, private, or inaccessible with the current permissions.",
                **details,
            )
        return unknown(
            "The platform could not verify this post; check account permissions and API configuration.",
            **details,
        )
    except Exception as exc:
        # Never store raw transport messages, URLs or platform response bodies (they may contain tokens).
        return unknown(
            "The availability check could not complete. Try again later.", error_type=type(exc).__name__
        )
    finally:
        if selected is not None and client is None:
            selected.client.close()


def record(db, settings, post, observation, actor, at=None):
    at = at or now()
    current = db.get(PostAvailability, post.id)
    if current is None:
        current = PostAvailability(post_id=post.id)
        db.add(current)
    current.state, current.reason, current.checked_at = observation.state, observation.reason, at
    current.next_check_at = at + max(
        timedelta(hours=settings.post_availability_interval_hours), timedelta(seconds=observation.retry_after)
    )
    if observation.public_ids:
        current.public_ids = observation.public_ids
    if observation.state == "available":
        current.last_seen_at, current.unavailable_since = at, None
    elif observation.state == "unavailable" and not current.unavailable_since:
        current.unavailable_since = at
    db.add(
        AvailabilityCheck(
            post_id=post.id,
            state=observation.state,
            reason=observation.reason,
            details=observation.details,
            actor=actor,
            checked_at=at,
        )
    )
    db.flush()
    return current


def check_post(db, settings, post, actor="monitor", client=None, *, commit=True):
    lock_post(db, post)
    if post.status != "published":
        raise ValueError("Availability checks are for confirmed published posts.")
    current = record(db, settings, post, observe(db, settings, post, client), actor)
    if commit:
        db.commit()
    return current


def collect(db, settings, client=None):
    if not settings.post_availability_enabled:
        return
    due = db.scalars(
        select(Post)
        .outerjoin(PostAvailability, PostAvailability.post_id == Post.id)
        .where(
            Post.status == "published",
            Post.dry_run.is_(False),
            or_(PostAvailability.next_check_at.is_(None), PostAvailability.next_check_at <= now()),
        )
        .order_by(func.coalesce(PostAvailability.next_check_at, Post.published_at), Post.id)
        .limit(settings.post_availability_batch_size)
    ).all()
    for post in due:
        try:
            # Recheck the due time under the lock if a user just checked this post.
            lock_post(db, post)
            current = db.get(PostAvailability, post.id, populate_existing=True)
            if current and current.next_check_at and current.next_check_at > now():
                db.rollback()
                continue
            check_post(db, settings, post, client=client)
        except PostConflict:
            db.rollback()
        except Exception as exc:
            db.rollback()
            event("availability_check_failed", post_id=post.id, error_type=type(exc).__name__)


def tick(factory, settings):
    # Separate scheduler job so slow availability reads never delay scheduled publishing.
    with factory() as db:
        collect(db, settings)
