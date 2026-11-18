"""Data used by the Studio pages and their polling endpoints."""

import json

import yaml
from fastapi import HTTPException
from sqlalchemy import func, select

from . import connections, crm_access, reposts
from .analytics import report
from .asset_edit import form_values as asset_edit_values
from .availability import LABELS
from .clip_replace import clip_options, needs_runway_music, source_job
from .config import PLATFORMS, REAL_MEDIA_ONLY
from .drive_import import panel as drive_panel
from .facts import content_digest
from .idea_plan import (
    END_CARD_SECONDS,
    FORMATS,
    MAX_CLIPS,
    STYLES,
    logo_assets,
    plan_summary,
    shot_seconds,
    work_summary,
)
from .models import (
    Account,
    Alert,
    Asset,
    AssetEdit,
    AvailabilityCheck,
    CheckResult,
    Control,
    FactSheet,
    MediaCampaign,
    MediaJob,
    Post,
    PostAvailability,
    PostRetry,
    RequestRecord,
    Transition,
)
from .recovery import held_reason, retry_option, task_failed
from .retries import block_reason, original_post
from .rights import RIGHTS, rights_label
from .storage import load_assets
from .usage import dashboard as usage_dashboard
from .video_rules import music_tracks

# One photo or video and a soundtrack to post as they are, or up to three images
# and three audio references for new AI media.
MAX_IDEA_UPLOADS = 6


def recent_posts(db, *, actor=None, needs_attention=False, limit=200):
    query = select(Post).where(Post.status != "deleted")
    if actor is not None:
        query = query.where(Post.prompted_by == actor)
    if needs_attention:
        unavailable = select(PostAvailability.post_id).where(
            PostAvailability.state.in_(["unavailable", "unknown"])
        )
        query = query.where(
            Post.status.in_(["held", "awaiting_review", "awaiting_manual", "failed"])
            | ((Post.status == "published") & Post.id.in_(unavailable))
        )
    return db.scalars(query.order_by(Post.created_at.desc()).limit(limit)).all()


def availability_by_post(db, posts):
    if not posts:
        return {}
    query = select(PostAvailability).where(PostAvailability.post_id.in_([p.id for p in posts]))
    return {row.post_id: row for row in db.scalars(query)}


def recent_media_jobs(db):
    return db.execute(
        select(MediaJob, Post.status)
        .outerjoin(Post, Post.id == MediaJob.post_id)
        .where(Post.status.is_(None) | (Post.status != "deleted"))
        .order_by(MediaJob.created_at.desc())
        .limit(100)
    ).all()


def post_revision(db, post_id):
    transition = select(func.max(Transition.id)).where(Transition.post_id == post_id).scalar_subquery()
    check = (
        select(func.max(AvailabilityCheck.id)).where(AvailabilityCheck.post_id == post_id).scalar_subquery()
    )
    return db.scalar(select(func.coalesce(transition, 0) + func.coalesce(check, 0)))


def _connection_states(db, settings):
    states = []
    live_available = connections.live_controls_available(settings)
    for account in db.scalars(select(Account)):
        connected = connections.account_connected(settings, account)
        live_enabled = settings.wants_live(account.platform, account)
        states.append(
            {
                "account": account,
                "connected": connected,
                "dry": not (connected and live_enabled),
                "oauth_ready": connections.ready(settings, account.platform),
                "live_enabled": live_enabled,
                "live_available": live_available,
            }
        )
    return states


def post_context(db, settings, post, *, editor):
    assets = load_assets(db, post.asset_ids)
    context = {"post": post, "posting_assets": [asset for asset in assets if asset is not None]}
    if editor:
        return context
    if post.status == "awaiting_review":
        context["reviewed_digest"] = content_digest(post, assets) if None not in assets else ""
        context["post_wants_live"] = settings.wants_live(post.platform, db.get(Account, post.platform))
    if post.status == "published":
        context["availability"] = db.get(PostAvailability, post.id)
        if not post.dry_run:
            context["repost_existing"] = reposts.existing(db, post)
            context["availability_history"] = db.scalars(
                select(AvailabilityCheck)
                .where(AvailabilityCheck.post_id == post.id)
                .order_by(AvailabilityCheck.id.desc())
                .limit(20)
            ).all()
    else:
        context["repost_sources"] = reposts.sources(db, post)
    if post.status == "failed":
        retry = db.get(PostRetry, post.id)
        context.update(
            retry_existing=retry, retry_block_reason="" if retry else block_reason(db, settings, post)
        )
    else:
        context["retry_original"] = original_post(db, post)
    return context | {
        "post_revision": post_revision(db, post.id),
        "held": held_reason(db, post),
        "clip_fix": clip_options(db, post),
        "finish_runway": post.status == "held" and needs_runway_music(db, post),
        "checks": db.scalars(
            select(CheckResult).where(CheckResult.post_id == post.id).order_by(CheckResult.id.desc())
        ).all(),
        "history": db.scalars(
            select(Transition).where(Transition.post_id == post.id).order_by(Transition.id)
        ).all(),
        "requests": db.scalars(select(RequestRecord).where(RequestRecord.post_id == post.id)).all(),
    }


def _idea_from_post(db, source_id, actor):
    post = db.get(Post, source_id)
    if not post or (actor is not None and post.prompted_by != actor):
        raise HTTPException(404)
    source = source_job(db, post)
    brief = source.brief if source else {}
    return {
        "prompt": brief.get("prompt", post.campaign),
        "platforms": [post.platform],
        "product": post.product,
        "kind": post.kind,
        "rights": brief.get("rights", "public"),
        "owner": brief.get("owner", ""),
        "reference_ids": brief.get("reference_ids", [] if source else post.asset_ids),
    }


def _idea_context(db, settings, assets, form, plan):
    upload_ids = form.get("upload_ids", [])
    by_id = {asset.id: asset for asset in assets} if plan or upload_ids else {}
    context = {
        "formats": FORMATS,
        "styles": STYLES,
        "idea_form": form,
        "idea_uploads": [by_id[key] for key in upload_ids if key in by_id],
        "max_idea_uploads": MAX_IDEA_UPLOADS,
        "logos": logo_assets(assets),
    }
    if plan is not None:
        platforms, model = form.get("platforms", []), settings.runway_video_model
        context.update(
            plan=plan,
            plan_assets=by_id,
            plan_summary=plan_summary(plan, platforms, model),
            work_summary=work_summary(plan, platforms, model),
            clip_seconds=shot_seconds(plan, model),
            max_clips=MAX_CLIPS,
            end_card_seconds=END_CARD_SECONDS,
            music_tracks=music_tracks(db),
        )
    return context


def page_context(
    request,
    db,
    settings,
    view,
    *,
    asset_form=None,
    upload_form=None,
    bucket_form=None,
    drive_form=None,
    editing_asset=None,
    media_edit_values=None,
    idea_form=None,
    plan=None,
):
    editor = request.state.editor
    actor = request.state.actor if editor else None
    context = {
        "view": view,
        "editor": editor,
        "settings": settings,
        "json": json,
        "yaml": yaml,
        "media_base_url": (settings.media_base_url or "/media").rstrip("/"),
        "availability_labels": LABELS,
        "platforms": PLATFORMS,
        "real_media_only": REAL_MEDIA_ONLY,
        "rights_choices": RIGHTS,
        "rights_label": rights_label,
        "asset_form": asset_form or {},
    }
    if not editor or view == "create":
        states = _connection_states(db, settings)
        context.update(connections=states, any_live=any(not state["dry"] for state in states))
    if not editor:
        control = db.get(Control, 1)
        context.update(control=control, automation_enabled=settings.automates_posts(control))
        context["active_jobs"] = db.scalar(
            select(func.count(MediaJob.id)).where(
                MediaJob.state.in_(["planned", "queued", "submitting", "running", "rendering"])
            )
        )

    if view in {"assets", "compose", "media-studio", "create", "plan"}:
        assets = db.scalars(select(Asset).order_by(Asset.created_at.desc()))
        context["assets"] = [a for a in assets if not editor or crm_access.editor_asset(a, actor)]
    if view in {"facts", "compose", "media-studio", "create"}:
        facts = db.scalars(select(FactSheet).order_by(FactSheet.created_at.desc())).all()
        context["facts"] = facts
        if view == "create":
            context["default_facts_id"] = next(
                (f.id for f in facts if not f.document.get("demo_only")), facts[0].id if facts else ""
            )

    if view in {"dashboard", "queues"}:
        posts = recent_posts(db, actor=actor, needs_attention=view == "queues")
        context.update(
            posts=posts,
            availability_by_post=availability_by_post(db, posts),
            alerts=db.scalars(select(Alert).order_by(Alert.created_at.desc()).limit(15)).all(),
        )
    elif view in {"create", "plan"}:
        form = idea_form or {}
        if view == "create" and not form and (source_id := request.query_params.get("from_post")):
            form = _idea_from_post(db, source_id, actor)
        context.update(_idea_context(db, settings, context["assets"], form, plan))
        if editor and view == "create":
            context["posts"] = recent_posts(db, actor=actor)
            context["my_jobs"] = db.scalars(
                select(MediaJob)
                .where(MediaJob.brief["prompted_by"].as_string() == actor, MediaJob.post_id.is_(None))
                .order_by(MediaJob.created_at.desc())
                .limit(100)
            ).all()
    elif view == "media-queue":
        context.update(media_jobs=recent_media_jobs(db), retry_option=retry_option, task_failed=task_failed)
    elif view == "media-studio":
        context["media_campaigns"] = db.scalars(
            select(MediaCampaign).order_by(MediaCampaign.created_at.desc())
        ).all()
    elif view == "assets":
        context.update(drive=drive_panel(db, settings), drive_form=drive_form)
    elif view == "upload":
        context.update(upload_form=upload_form or {}, bucket_form=bucket_form or {})
    elif view == "asset-edit":
        context.update(
            editing_asset=editing_asset,
            media_edit_values=asset_edit_values(editing_asset)
            if media_edit_values is None
            else media_edit_values,
            asset_history=db.scalars(
                select(AssetEdit)
                .where(AssetEdit.asset_id == editing_asset.id)
                .order_by(AssetEdit.created_at.desc(), AssetEdit.id.desc())
                .limit(30)
            ).all(),
        )
    elif view == "analytics":
        context["analytics"] = report(db, settings)
    elif view == "usage":
        days = request.query_params.get("days", "30")
        context["usage"] = usage_dashboard(db, settings, int(days) if days.isdigit() else 30)
    return context
