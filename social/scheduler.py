import math
from datetime import time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from .config import profiles
from .db import now
from .lifecycle import PostConflict, transition
from .models import Account, Control, Post

COUNTED = {"scheduled", "publishing", "published", "awaiting_manual"}


def in_window(at, profile):
    local = at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(profile.timezone))
    return any(
        time.fromisoformat(a) <= local.time().replace(tzinfo=None) < time.fromisoformat(b)
        for a, b in profile.windows
    )


def permitted(at, post, others, profile, settings, respect_target=True):
    relevant = [
        p
        for p in others
        if p.platform == post.platform and p.account_id == post.account_id and p.status in COUNTED
    ]
    moments = [(p.published_at or p.scheduled_at) for p in relevant if p.published_at or p.scheduled_at]
    if not in_window(at, profile):
        return False
    if any(abs((at - t).total_seconds()) < profile.minimum_spacing_minutes * 60 for t in moments):
        return False
    # Check every affected trailing window, including reservations after this candidate.
    for end in [at] + [t for t in moments if at <= t < at + timedelta(hours=24)]:
        count = sum(end - timedelta(hours=24) < t <= end for t in moments)
        if count + 1 > profile.hard_cap:
            return False
    tz = ZoneInfo(profile.timezone)
    date = at.replace(tzinfo=timezone.utc).astimezone(tz).date()
    if (
        respect_target
        and sum(t.replace(tzinfo=timezone.utc).astimezone(tz).date() == date for t in moments)
        >= profile.target_per_day
    ):
        return False
    if post.platform == "tiktok" and sum(p.status == "awaiting_manual" for p in relevant) >= 4:
        return False
    history = [p for p in relevant if at - timedelta(days=7) < (p.published_at or p.scheduled_at) <= at]
    promotions = sum(p.kind == "promotional" for p in history) + (post.kind == "promotional")
    if promotions > math.ceil((len(history) + 1) * profile.promotional_ratio):
        return False
    for previous in others:
        stamp = previous.published_at or previous.scheduled_at
        if (
            previous.campaign == post.campaign
            and previous.platform != post.platform
            and stamp
            and previous.status in COUNTED
        ):
            if abs((at - stamp).total_seconds()) < settings.campaign_stagger_minutes * 60:
                return False
    return True


def schedule(db, settings, post, earliest=None):
    # Acquires a database write lock, serializing reservations in SQLite/PostgreSQL.
    db.execute(update(Control).where(Control.id == 1).values(revision=Control.revision + 1))
    db.refresh(post)
    control, account = db.get(Control, 1), db.get(Account, post.platform)
    if control.killed or account.paused:
        raise ValueError("Publishing is paused")
    if post.status != "approved":
        db.rollback()
        raise PostConflict("This post has already changed status. Refresh its page to see its schedule.")
    from .adapters import adapter

    selected = adapter(settings, db, post)
    post.dry_run = selected.dry
    post.account_id = account.external_id or "dry-" + post.platform
    profile = profiles(settings)[post.platform]
    candidate = max(earliest or now(), now()).replace(second=0, microsecond=0) + timedelta(minutes=1)
    others = db.scalars(select(Post).where(Post.id != post.id, Post.status.in_(COUNTED))).all()
    for _ in range(60 * 24 * 31):
        if permitted(candidate, post, others, profile, settings):
            post.scheduled_at = candidate
            transition(db, post, "scheduled", f"Reserved {candidate.isoformat()} UTC; dry_run={post.dry_run}")
            db.commit()
            return candidate
        candidate += timedelta(minutes=1)
    db.rollback()
    raise ValueError("No slot available within 31 days: caps, spacing, pending TikTok inbox, or content mix")
