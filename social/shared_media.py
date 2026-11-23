"""Share a finished rendition within one Studio batch, keeping one post per destination."""

import json
from collections import Counter, defaultdict

from sqlalchemy import case, or_, select
from sqlalchemy.orm import aliased

from .config import REAL_MEDIA_ONLY
from .db import now
from .models import Account, Asset, MediaJob

VISUAL_DIRECTION = ("concept", "direction", "use_case", "seed")


def group_renders(db, settings, jobs, profiles):
    db.flush()
    if len(jobs) < 2:
        return jobs
    groups = defaultdict(list)
    for job in jobs:
        if job.brief["operation"] == "audio":
            continue
        # The batch supplies one idea. Platform history supplies alternative visual directions
        # and seeds; use the rendering job's direction for the shared asset.
        inputs = {
            k: v
            for k, v in job.brief.items()
            if k != "platforms" and k not in VISUAL_DIRECTION
        }
        key = (
            json.dumps(inputs, sort_keys=True),
            settings.wants_live(job.platform, db.get(Account, job.platform)),
        )
        groups[key].append(job)
    for group in groups.values():
        if len(group) < 2:
            continue
        brief = group[0].brief
        video = brief["operation"] in {"video", "video_edit"} or brief.get("real_format") == "video"
        sizes = {}
        for job in group:
            profile = profiles[job.platform]
            sizes[job.id] = (
                (profile.video_width, profile.video_height)
                if video
                else (profile.image_width, profile.image_height)
            )
        counts = Counter(sizes.values())
        # Prefer the framing most destinations request. For a tie, Reels' stricter minimum
        # dimensions make Facebook a good source for uploaded videos as well.
        ordered = sorted(
            group, key=lambda j: (-counts[sizes[j.id]], not (video and j.platform == "facebook"))
        )
        peers = [j.id for j in ordered]
        for job in group:
            job.brief = job.brief | {"render_peers": peers}
    return jobs


def runnable_jobs(db, limit, at):
    """Apply the tick limit only to jobs that can make progress."""
    source = aliased(MediaJob)
    retry_at = MediaJob.brief["runway_retry_at"].as_string()
    priority = case((MediaJob.brief["workflow"].as_string() == "immediate", 0), else_=1)
    query = (
        select(MediaJob)
        .join(Account, Account.platform == MediaJob.platform)
        .outerjoin(source, source.id == MediaJob.brief["shared_render_from"].as_string())
        .where(
            Account.paused.is_(False),
            MediaJob.state.in_(["queued", "submitting", "running", "rendering"]),
            or_(source.id.is_(None), source.state.in_(["complete", "cancelled"])),
            or_(MediaJob.state != "queued", retry_at.is_(None), retry_at <= at.isoformat()),
        )
        .order_by(priority, MediaJob.created_at)
        .limit(limit)
    )
    seen = set()
    for job in db.scalars(query).all():
        if job.state == "queued" and job.brief.get("render_peers"):
            job = _next_shared_job(db, job)
        if job and job.id not in seen:
            seen.add(job.id)
            yield job


def _next_shared_job(db, job):
    """The single publishing worker elects an active renderer before any paid work.

    Persist the choice across ticks and restarts. A failed or uncertain source keeps its
    place for recovery. Cancelling it lets the remaining queued destinations elect another.
    """
    source_id = job.brief.get("shared_render_from")
    if source_id:
        source = db.get(MediaJob, source_id)
        if source and source.state != "cancelled":
            return job if source.state == "complete" else None
    peers = job.brief["render_peers"]
    query = (
        select(MediaJob, Account.paused)
        .join(Account, Account.platform == MediaJob.platform)
        .where(MediaJob.id.in_(peers), MediaJob.state.in_(["planned", "queued"]))
        .order_by(MediaJob.id)
        .with_for_update(of=MediaJob)
        .execution_options(populate_existing=True)
    )
    members = db.execute(query).all()
    active = {member.id: member for member, paused in members if member.state == "queued" and not paused}
    source = next((active[i] for i in peers if i in active), None)
    if source is None:
        db.rollback()
        return None
    direction = {k: source.brief[k] for k in VISUAL_DIRECTION if k in source.brief}
    for member, _ in members:
        if member.id == source.id:
            member.brief = {
                k: v
                for k, v in member.brief.items()
                if k not in {"render_peers", "shared_render_from", "shared_render_platform"}
            }
        else:
            member.brief = member.brief | direction | {
                "shared_render_from": source.id,
                "shared_render_platform": source.platform,
            }
    db.commit()
    return source


def compatible_render(db, job, source, profile):
    from .gate import media_errors
    from .provenance import real_drive_media_error

    asset = db.get(Asset, source.output_asset_id)
    if not asset or asset.removed_at or (asset.rights_expire_at and asset.rights_expire_at <= now()):
        raise ValueError("The shared media is missing, removed, or its usage rights have expired.")
    if job.platform in REAL_MEDIA_ONLY and (error := real_drive_media_error(db, [asset])):
        raise ValueError(error)
    if not media_errors([asset], profile, job.platform):
        return source, asset
    # A previous destination may already have made the alternate size this one needs.
    others = set(job.brief["render_peers"]) - {job.id, source.id}
    if others:
        query = (
            select(MediaJob, Asset)
            .join(Asset, Asset.id == MediaJob.output_asset_id)
            .where(
                MediaJob.id.in_(others),
                MediaJob.state == "complete",
                MediaJob.brief["shared_render_from"].as_string().is_(None),
                Asset.removed_at.is_(None),
                or_(Asset.rights_expire_at.is_(None), Asset.rights_expire_at > now()),
            )
        )
        for peer, alternate in db.execute(query):
            if (not media_errors([alternate], profile, job.platform)
                    and (job.platform not in REAL_MEDIA_ONLY or not real_drive_media_error(db, [alternate]))):
                return peer, alternate
    return None
