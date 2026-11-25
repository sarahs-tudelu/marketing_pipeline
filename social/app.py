import base64
import hashlib
import json
import math
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import yaml
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.middleware.sessions import SessionMiddleware

from . import connections, crm_access
from .campaigns import campaign_revision, lock_campaign, update_campaign, validate_campaign
from .clip_replace import needs_runway_music, replace_part
from .config import PLATFORMS, REAL_MEDIA_ONLY, get_settings, profiles
from .db import make_engine, now, schema_gaps, sessions
from .facts import Facts, caption_fact_ids, load_facts, render, save_facts
from .gate import run_gate
from .idea_plan import (
    END_CARD_SECONDS,
    MAX_CLIPS,
    MIN_CLIP_SECONDS,
    IdeaPlan,
    Shot,
    default_title,
    logo_assets,
    plan_idea,
    read_signals,
    uploaded_plan,
)
from .lifecycle import PostBusy, PostConflict, lock_post, post_processing, transition
from .media_changes import request_changes
from .media_queue import DELIVERY_STATES, dismiss_completed, job_progress, queue_contents, recent_media_jobs
from .media_studio import (
    CreativeBrief,
    finish_video,
    platform_copy,
    queue_jobs,
    resume_job,
    website_snapshot,
)
from .models import (
    Account,
    Asset,
    Control,
    FactSheet,
    MediaCampaign,
    MediaJob,
    Post,
)
from .monitoring import event, set_kill, setup_logging
from .pipeline import bootstrap, delete_draft, draft, generate, rewrite_caption
from .post_views import list_location, next_review, published_location, source_query, workspace_navigation
from .provenance import real_drive_media_error, requires_ai_review
from .provider_errors import ProviderError
from .providers import Providers
from .real_edit import plan_real_idea, room_for
from .recovery import retry_step, task_failed
from .retries import retry_post
from .rights import owner_value, usage_rights
from .scheduler import schedule
from .storage import ingest, load_assets, storage
from .studio_pages import (
    MAX_IDEA_PICKS,
    MAX_IDEA_UPLOADS,
    availability_by_post,
    page_context,
    post_context,
    post_revision,
    recent_posts,
)
from .upload_errors import describe_upload_error, upload_error_details
from .usage import PERIODS as USAGE_PERIODS
from .usage import flush as flush_usage
from .usage import sync as sync_usage
from .video_rules import MUSIC_TAG, has_sound


class DraftInput(BaseModel):
    platform: str
    campaign: str
    asset_ids: list[str]
    facts_id: str
    product: str = "company"
    kind: str = "promotional"


def money(value):
    """$1,234 / $12.34 / $0.042, or a dash when unknown."""
    if value is None:
        return "—"
    if abs(value) >= 1000:
        return f"${value:,.0f}"
    if abs(value) >= 1 or value == 0:
        return f"${value:,.2f}"
    return f"${value:.3f}"


def compact(value):
    """1,284 / 12.9K / 4.2M."""
    if value is None:
        return "—"
    for size, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (10_000, "K")):
        if abs(value) >= size:
            return f"{value / size:.1f}".rstrip("0").rstrip(".") + suffix
    return f"{value:,.0f}"


def percent(value, digits=1):
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def create_app(settings=None):
    settings = settings or get_settings()
    engine = make_engine(settings.database_dsn)
    factory = sessions(engine)
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    templates.env.filters["platform"] = lambda name: {"linkedin": "LinkedIn", "x": "X", "tiktok": "TikTok"}.get(
        name, str(name).title()
    )
    from .video_repair import REPAIR_LABELS
    templates.env.globals["repair_labels"] = REPAIR_LABELS
    templates.env.globals["source_query"] = source_query
    templates.env.filters["money"] = money
    templates.env.filters["compact"] = compact
    templates.env.filters["percent"] = percent
    static_dir = Path(__file__).parent / "static"
    static_version = hashlib.sha256(
        (static_dir / "studio.js").read_bytes() + (static_dir / "studio.css").read_bytes()
    ).hexdigest()[:12]
    settings.media_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app):
        setup_logging()
        with factory() as db:
            bootstrap(db)
            load_facts(db, settings.facts_path)
            db.commit()
            await run_in_threadpool(connections.import_meta_from_env, db, settings)
        yield
        if settings.storage_backend == "s3":
            app.state.media_store.client.close()
        engine.dispose()

    app = FastAPI(title="Tudelu Social Studio", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings, app.state.factory = settings, factory

    @app.middleware("http")
    async def auth(request: Request, call_next):
        path = request.url.path
        public_media = path.startswith("/media/") and (
            settings.auth_mode != "crm" or (
                settings.media_base_url and request.url.hostname == urlsplit(settings.media_base_url).hostname
                and request.url.hostname != urlsplit(settings.app_base_url).hostname
            )
        )
        if path == "/health" or path.startswith("/static/") or public_media:
            response = await call_next(request)
            if path.startswith("/media/"):
                response.headers["Cache-Control"] = (
                    "public, max-age=31536000, immutable"
                    if response.status_code in {200, 206, 304}
                    else "no-store"
                )
                response.headers["X-Content-Type-Options"] = "nosniff"
            return response
        if settings.auth_mode != "crm" and path.startswith("/oauth/") and path.endswith("/callback"):
            return await call_next(request)  # Bound state + signed session checked at callback.
        user = "local-developer"
        valid = False
        request.state.editor = False
        if settings.auth_mode == "crm":
            try:
                user, label, role = await crm_access.identity(settings, request.cookies.get(settings.crm_session_cookie))
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers={"Cache-Control": "no-store"})
            if request.session.get("crm_actor") != user:
                request.session.clear()
                request.session["crm_actor"] = user
            request.state.editor = role == "editor"
            if request.state.editor and not crm_access.editor_route(request.method, path):
                return JSONResponse({"detail": "Access denied"}, status_code=403, headers={"Cache-Control": "no-store"})
            if request.state.editor and path == "/":
                return RedirectResponse("/admin/create", status_code=303, headers={"Cache-Control": "no-store"})
            valid = True
        elif settings.auth_mode == "dev":
            valid = request.client is not None and request.client.host in {"127.0.0.1", "::1", "testclient"}
        elif settings.auth_mode == "basic":
            try:
                scheme, encoded = request.headers.get("authorization", "").split(" ", 1)
                username, password = base64.b64decode(encoded, validate=True).decode().split(":", 1)
                valid = (
                    scheme.lower() == "basic"
                    and secrets.compare_digest(username, settings.admin_username)
                    and secrets.compare_digest(password, settings.admin_password.get_secret_value())
                )
                user = username
            except (ValueError, UnicodeError):
                pass
        else:
            # SSO terminates at the trusted proxy; it must strip and replace both headers.
            supplied = request.headers.get("x-tudelu-proxy-secret", "")
            user = request.headers.get("x-authenticated-user", "")
            valid = bool(user) and secrets.compare_digest(
                supplied, settings.proxy_auth_secret.get_secret_value()
            )
        if not valid:
            return JSONResponse(
                {"detail": "Internal login required"},
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Tudelu"'},
            )
        request.state.actor = user
        request.state.prompt_account = {
            "prompted_by": user if settings.auth_mode == "crm" else f"{settings.auth_mode}:{user}",
            "prompted_by_label": label if settings.auth_mode == "crm" else user,
        }
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; img-src 'self' data: https:; "
            "media-src 'self' https:; form-action 'self'; frame-ancestors 'none'"
        )
        return response

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret.get_secret_value() or secrets.token_urlsafe(32),
        same_site="lax",
        https_only=settings.environment == "production" or settings.app_base_url.startswith("https://"),
        max_age=3600,
    )
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    if settings.storage_backend == "local":
        app.mount("/media", StaticFiles(directory=settings.media_dir), name="media")
    else:
        from .media import router as media_router
        from .storage import S3Store

        app.state.media_store = S3Store(settings)
        app.include_router(media_router)

    def db_session():
        with factory() as db:
            yield db
            # Work the request didn't commit is discarded, as closing the session would; then the AI requests
            # it made are saved for Settings → Usage.
            db.rollback()
            flush_usage(db)

    DB = Annotated[object, Depends(db_session)]

    async def csrf(request: Request):
        supplied = request.headers.get("x-csrf-token", "")
        if not supplied and request.headers.get("content-type", "").split(";")[0] in {
            "application/x-www-form-urlencoded",
            "multipart/form-data",
        }:
            try:
                supplied = str((await request.form()).get("csrf", ""))
            except OSError as exc:
                status, message = describe_upload_error(exc)
                event("asset_upload_failed", stage="receive", **upload_error_details(exc))
                raise HTTPException(status, message) from None
        expected = request.session.get("csrf", "")
        if not expected or not secrets.compare_digest(supplied, expected):
            raise HTTPException(403, "CSRF token missing or invalid")

    mutation = [Depends(csrf)]

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(PostConflict)
    async def post_conflict(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.get("/health")
    def health():
        try:
            gaps = schema_gaps(engine)
        except SQLAlchemyError as exc:
            event("database_health_failed", error_type=type(exc).__name__)
            return JSONResponse(
                {"status": "not_ready", "detail": "Database unavailable."},
                status_code=503,
            )
        if gaps["missing_tables"] or gaps["missing_columns"]:
            event("database_schema_incomplete", **gaps)
            return JSONResponse(
                {
                    "status": "not_ready",
                    "detail": "Database migration required. Run alembic upgrade head with the current image.",
                },
                status_code=503,
            )
        return {"status": "ok"}

    def page(
        request, db, view, post=None, *, error_message="", post_busy=False, status_code=200, post_values=None,
        **form_values,
    ):
        request.session.setdefault("csrf", secrets.token_urlsafe(32))
        request.session.setdefault("browser_nonce", secrets.token_urlsafe(32))
        context = page_context(request, db, settings, view, **form_values)
        if post is not None:
            context.update(post_context(db, settings, post, editor=request.state.editor))
            context.update(post_values or {})  # What the admin typed, kept when the action failed.
            if not request.state.editor:
                context.update(workspace_navigation(db, post, request.query_params))
                context["post_busy"] = post_busy or post.status == "publishing" or post_processing(engine, post.id)
        context.update(
            csrf=request.session["csrf"],
            static_version=static_version,
            error_message=error_message,
            notice=request.session.pop("notice", ""),  # Set by the action that redirected here.
        )
        return templates.TemplateResponse(
            request=request,
            name="editor.html" if request.state.editor else "studio.html",
            status_code=status_code,
            context=context,
        )

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, db: DB):
        return page(request, db, "dashboard")

    @app.get("/admin/{view}", response_class=HTMLResponse)
    def admin(view: str, request: Request, db: DB):
        if view not in {
            "connections",
            "queues",
            "assets",
            "upload",
            "facts",
            "compose",
            "analytics",
            "create",
            "media-queue",
            "published-posts",
            "media-studio",
            "automation",
            "usage",
        }:
            raise HTTPException(404)
        if view == "usage":
            flush_usage(db)  # Show requests made moments ago.
        return page(request, db, view)

    @app.post("/forms/usage/sync", dependencies=mutation)
    async def refresh_usage(request: Request, db: DB):
        days = str((await request.form()).get("days", "30"))
        await run_in_threadpool(sync_usage, db, settings)
        query = f"?days={days}" if days.isdigit() and int(days) in USAGE_PERIODS else ""
        return RedirectResponse("/admin/usage" + query, status_code=303)

    @app.get("/admin/assets/{asset_id}/edit", response_class=HTMLResponse)
    def edit_media(asset_id: str, request: Request, db: DB):
        asset = db.get(Asset, asset_id)
        if not asset:
            raise HTTPException(404, "Media not found")
        return page(request, db, "asset-edit", editing_asset=asset)

    @app.post("/forms/assets/{asset_id}/save-media", dependencies=mutation)
    async def save_media(asset_id: str, request: Request, db: DB):
        from .asset_edit import AssetEditConflict, edit_asset

        asset = db.get(Asset, asset_id)
        if not asset:
            raise HTTPException(404, "Media not found")
        form = await request.form()
        values = media_form_values(form) | {key: str(form.get(key, "")) for key in ("original_name", "revision", "real_customer_content", MUSIC_TAG)}
        try:
            arguments = media_form_arguments(values)
            # These two tags have dedicated controls in the editor.
            for key in ("brand_logo", "real_customer_content", MUSIC_TAG):
                arguments["tags"].pop(key, None)
                if values[key] == "on":
                    arguments["tags"][key] = True
            await run_in_threadpool(edit_asset, db, asset, arguments, original_name=values["original_name"], expected_revision=values["revision"], actor=request.state.actor)
            db.commit()
        except (ValueError, SQLAlchemyError) as exc:
            db.rollback()
            status = 409 if isinstance(exc, AssetEditConflict) else 503 if isinstance(exc, SQLAlchemyError) else 400
            message = "The media details could not be saved. Try again shortly." if isinstance(exc, SQLAlchemyError) else str(exc)
            return page(request, db, "asset-edit", editing_asset=asset, media_edit_values=values, error_message=message, status_code=status)
        return RedirectResponse(f"/admin/assets/{asset_id}/edit?saved=1", status_code=303)

    @app.get("/posts/{post_id}", response_class=HTMLResponse)
    def detail(post_id: str, request: Request, db: DB):
        post = db.get(Post, post_id)
        if not post or post.status == "deleted" or (request.state.editor and post.prompted_by != request.state.actor):
            raise HTTPException(404)
        return page(request, db, "post", post)

    @app.get("/posts/{post_id}/media/{asset_id}")
    def post_media(post_id: str, asset_id: str, request: Request, db: DB):
        post = db.get(Post, post_id)
        if (not post or post.status == "deleted" or asset_id not in post.asset_ids
                or (request.state.editor and post.prompted_by != request.state.actor)):
            raise HTTPException(404)
        asset = db.get(Asset, asset_id)
        if not asset:
            raise HTTPException(404)
        if settings.storage_backend == "local":
            return FileResponse(storage(settings).path(asset.storage_key), media_type=asset.media["mime"])
        from .media import public_media
        return public_media(asset.storage_key, request)

    @app.get("/posts/{post_id}/repair-evidence/{clip}")
    def repair_evidence(post_id: str, clip: int, request: Request, db: DB):
        import io

        from fastapi.responses import Response
        from PIL import Image, ImageDraw

        from .clip_replace import failed_vision, review_evidence, source_job
        from .video_repair import evidence_frames

        post = db.get(Post, post_id)
        if not post or post.status == "deleted" or (request.state.editor and post.prompted_by != request.state.actor):
            raise HTTPException(404)
        job = source_job(db, post)
        shot = next((s for s in review_evidence(db, job, failed_vision(db, post)) if s["clip"] == clip), None) if job else None
        if not shot:
            raise HTTPException(404)
        times = shot["times"]
        frames = evidence_frames(settings, db.get(Asset, shot["asset_id"]), times)
        if len(frames) != len(times):
            raise HTTPException(422, "Could not read the flagged frames")
        sheet = Image.new("RGB", (320 * len(times), 270), "#222222")
        draw = ImageDraw.Draw(sheet)
        for i, (at, frame) in enumerate(zip(times, frames, strict=True)):
            frame.thumbnail((320, 240))
            sheet.paste(frame, (i*320+(320-frame.width)//2, 0))
            draw.text((i*320+6, 248), f"{at:g} s", fill="white")
        out = io.BytesIO()
        sheet.save(out, "JPEG", quality=88)
        return Response(out.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "private, no-store"})

    @app.get("/api/session")
    def session(request: Request):
        request.session.setdefault("csrf", secrets.token_urlsafe(32))
        return {"csrf": request.session["csrf"]}

    @app.get("/api/posts")
    def list_posts(request: Request, db: DB):
        posts = recent_posts(db, actor=request.state.actor if request.state.editor else None, limit=500)
        availability = availability_by_post(db, posts)
        return [
            {
                "id": p.id,
                "platform": p.platform,
                "status": p.status,
                "caption": p.caption,
                "prompted_by": p.prompted_by,
                "prompted_by_label": p.prompted_by_label,
                "scheduled_at": p.scheduled_at,
                "url": p.live_url,
                "dry_run": p.dry_run,
                "availability": ({"state": a.state, "reason": a.reason, "checked_at": a.checked_at,
                                  "last_seen_at": a.last_seen_at, "next_check_at": a.next_check_at}
                                 if (a := availability.get(p.id)) else None),
            }
            for p in posts
        ]

    def queue_prompt(db, brief, account):
        jobs = queue_jobs(db, settings, brief)
        for job in jobs:
            job.brief = job.brief | account
        return jobs

    @app.post("/api/media-jobs", dependencies=mutation)
    def create_media_job(payload: CreativeBrief, request: Request, db: DB):
        jobs = queue_prompt(db, payload, request.state.prompt_account)
        db.commit()
        return {"jobs": [{"id": j.id, "platform": j.platform, "state": j.state} for j in jobs]}

    @app.get("/api/media-jobs")
    def list_media_jobs(db: DB, campaign_id: str = "", queue_revision: bool = False):
        if queue_revision:
            return {"revision": queue_contents(db, campaign_id)["queue_revision"]}
        return [job_progress(*row) for row in recent_media_jobs(db, campaign_id)]

    @app.get("/api/posts/{post_id}/activity")
    def post_activity(post_id: str, db: DB):
        post = db.get(Post, post_id)
        if not post or post.status == "deleted":
            raise HTTPException(404)
        return {
            "processing": post.status == "publishing" or post_processing(engine, post_id),
            "status": post.status,
            "revision": post_revision(db, post_id),
        }

    @app.post("/forms/media-studio", dependencies=mutation)
    async def submit_creative(request: Request, db: DB):
        return await run_in_threadpool(save_creative, request, db, await request.form())

    @app.get("/admin/media-campaigns/{campaign_id}/edit", response_class=HTMLResponse)
    def edit_campaign(campaign_id: str, request: Request, db: DB):
        campaign = db.get(MediaCampaign, campaign_id)
        if not campaign:
            raise HTTPException(404, "Campaign not found")
        values = campaign.brief | {"campaign": campaign.name, "revision": campaign_revision(campaign),
                                  "website": "on" if campaign.brief.get("website", True) else ""}
        return page(request, db, "media-studio", editing_campaign=campaign, asset_form=values)

    @app.post("/forms/media-campaigns/{campaign_id}/save", dependencies=mutation)
    async def save_campaign(campaign_id: str, request: Request, db: DB):
        return await run_in_threadpool(save_creative, request, db, await request.form(), campaign_id)

    def save_creative(request, db, form, campaign_id=None):
        campaign = lock_campaign(db, campaign_id) if campaign_id else None
        if campaign_id and not campaign:
            raise HTTPException(404, "Campaign not found")
        stored = campaign.brief if campaign else {}
        mode = "recurring" if campaign else str(form.get("mode", "once"))
        lists = {key: form.getlist(key) for key in ("platforms", "reference_ids", "audio_reference_ids")}
        try:
            if campaign and form.get("revision") != campaign_revision(campaign):
                raise PostConflict("This campaign was edited after you opened it. Reload its settings before saving.")
            if mode not in {"once", "next", "recurring"}:
                raise ValueError("Choose Create now, Next automatic post, or Recurring campaign")
            values = {key: form.get(key, default) for key, default in {
                "campaign": "", "prompt": "", "facts_id": "", "rights": "", "owner": "", "audio_prompt": "",
                "product": "company", "operation": "image", "kind": "promotional", "duration": "5", "interval_hours": "24",
            }.items()}
            for key, selected in lists.items():
                # Checkbox display order can differ from the campaign's saved reference order.
                previous = stored.get(key, [])
                values[key] = [i for i in previous if i in selected] + [i for i in selected if i not in previous]
            values.update(website=form.get("website") == "on", create_posts=campaign is not None or form.get("create_posts") == "on",
                          workflow={"once": "immediate", "next": "next", "recurring": "automatic"}[mode])
            brief = CreativeBrief.model_validate(stored | values, context={"stored": values["rights"] == stored.get("rights")})
            if mode == "recurring":
                validate_campaign(db, settings, brief)
                if campaign:
                    update_campaign(db, campaign, brief)
                else:
                    db.add(MediaCampaign(name=brief.campaign, brief=brief.model_dump() | request.state.prompt_account, enabled=False))
            else:
                queue_prompt(db, brief, request.state.prompt_account)
            db.commit()
        except ValueError as exc:
            db.rollback()
            return page(
                request, db, "media-studio", editing_campaign=campaign, error_message=friendly(exc),
                asset_form=dict(form) | lists | {"website": form.get("website", ""), "create_posts": form.get("create_posts", "")},
                status_code=409 if isinstance(exc, PostConflict) else 400,
            )
        if campaign:
            request.session["notice"] = f"Saved changes to {campaign.name}."
            return RedirectResponse(f"/admin/media-studio#campaign-{campaign.id}", status_code=303)
        return RedirectResponse(
            "/admin/media-studio" if mode == "recurring" else "/admin/media-queue", status_code=303
        )

    def idea_values(form):
        values = {key: str(form.get(key, "")) for key in ("prompt", "format", "kind", "facts_id", "product", "rights", "owner")}
        values["product"] = "" if values["product"].strip() == "company" else values["product"].strip()
        values["website"] = "on" if form.get("website") == "on" else ""
        values["platforms"] = form.getlist("platforms")
        # Files chosen from the asset library; they're used like files added with the idea.
        values["reference_ids"] = list(dict.fromkeys(str(i) for i in form.getlist("reference_ids")))[:MAX_IDEA_PICKS]
        # Files added on the idea page, already saved in the asset library.
        values["upload_ids"] = list(dict.fromkeys(str(i) for i in form.getlist("upload_ids")))[:MAX_IDEA_UPLOADS]
        use = str(form.get("upload_use", ""))
        # Without a choice (older pages and API clients), library picks guide AI media as before.
        default = "reference" if values["reference_ids"] and not values["upload_ids"] else "as_is"
        values["upload_use"] = use if use in {"as_is", "reference", "real"} else default
        return values

    async def save_idea_uploads(db, form, values, actor):
        """Save files added with an idea to the asset library as real company media."""
        files = [f for f in form.getlist("files") if isinstance(f, UploadFile) and f.filename]
        if len(values["upload_ids"]) + len(files) > MAX_IDEA_UPLOADS:
            raise ValueError(f"Add at most {MAX_IDEA_UPLOADS} files to one idea")
        for file in files:
            try:
                if file.size is not None and file.size > settings.max_upload_bytes:
                    raise ValueError(
                        f"It's larger than the {settings.max_upload_bytes / 1_000_000:g} MB upload limit"
                    )
                data = await file.read(settings.max_upload_bytes + 1)
                asset = await run_in_threadpool(
                    ingest,
                    db,
                    settings,
                    data,
                    file.filename,
                    rights=usage_rights(values["rights"]),
                    owner=owner_value(values["owner"]),
                    tags={"product": values["product"] or "company", "uploaded_with_idea": True, "submitted_by": actor},
                )
                db.commit()
            except Exception as exc:
                db.rollback()
                event("asset_upload_failed", stage="idea", **upload_error_details(exc))
                raise ValueError(f"{file.filename}: {describe_upload_error(exc)[1]}") from None
            finally:
                data = None  # Release this upload before reading the next file in the batch.
                await file.close()
            # Kept even if a later step fails, so the file isn't uploaded twice.
            values["upload_ids"].append(asset.id)

    def idea_uploads(db, values):
        """The idea's media: files chosen from the library, then files added with the idea."""
        picks = load_assets(db, values.get("reference_ids", []))
        if any(a is None or a.removed_at is not None for a in picks):
            raise ValueError("A file you chose was deleted from the asset library. Choose it again or pick another.")
        uploads = load_assets(db, values.get("upload_ids", []))
        if any(a is None or a.removed_at is not None for a in uploads):
            raise ValueError("A file you added is no longer in the asset library. Add it again.")
        return picks + [a for a in uploads if a.id not in values.get("reference_ids", [])]

    def check_editor_inputs(request, db, form, values):
        if not request.state.editor:
            return
        actor = request.state.actor
        for asset_id in values["reference_ids"] + values["upload_ids"]:
            asset = db.get(Asset, asset_id)
            if not crm_access.editor_asset(asset, actor):
                raise HTTPException(404, "Media not found")
            if asset_id in values["upload_ids"] and asset.tags.get("submitted_by") != actor:
                raise HTTPException(404, "Upload not found")
        if logo_id := form.get("logo_id"):
            asset = db.get(Asset, logo_id)
            if not crm_access.editor_asset(asset, actor) or not logo_assets([asset]):
                raise HTTPException(404, "Logo not found")

    def friendly(exc):
        if isinstance(exc, ValidationError):
            return " ".join(
                str(e["msg"]).removeprefix("Value error, ").rstrip(".") + "." for e in exc.errors()[:3]
            )
        return str(exc)

    @app.post("/forms/ideas/plan", dependencies=mutation)
    async def plan_from_idea(request: Request, db: DB):
        form = await request.form()
        values = idea_values(form)
        check_editor_inputs(request, db, form, values)
        try:
            idea = " ".join(values["prompt"].split())
            if not idea:
                raise ValueError("Describe your idea first")
            if len(idea) > 1000:
                raise ValueError("Keep the idea under 1,000 characters")
            if not values["platforms"] or set(values["platforms"]) - set(PLATFORMS):
                raise ValueError("Choose at least one platform")
            usage_rights(values["rights"])
            owner_value(values["owner"])
            values["facts_id"] = values["facts_id"] or (
                db.scalar(select(FactSheet.id).order_by(FactSheet.created_at.desc()).limit(1)) or ""
            )
            if not db.get(FactSheet, values["facts_id"]):
                raise ValueError("Save an approved facts version first")
            await save_idea_uploads(db, form, values, request.state.actor)
            uploads = idea_uploads(db, values)
            signals = read_signals(idea)
            # A real project post edits the company's own media: the files added with the idea, or the
            # library's matches for an idea about real work, such as an installation process.
            real = (
                values["format"] == "real"
                or (uploads and values["upload_use"] == "real")
                or (values["format"] in {"", "auto"} and signals["real"] and not uploads)
            )
            if set(values["platforms"]) & REAL_MEDIA_ONLY and not real:
                raise ValueError(
                    "Pinterest only posts real photos and videos from Google Drive. Choose Real project, or "
                    "create Pinterest posts from Post from library media."
                )
            if real:
                actor = request.state.actor
                kind = values["kind"] if values["kind"] in {"promotional", "non_promotional"} else (
                    "non_promotional" if signals["editorial"] else "promotional"
                )
                found = await run_in_threadpool(
                    plan_real_idea,
                    db,
                    settings,
                    idea=idea,
                    title=default_title(idea),
                    uploads=uploads,
                    platforms=values["platforms"],
                    product=values["product"] or "company",
                    kind=kind,
                    rights=values["rights"],
                    requested="",
                    signals=signals,
                    allowed=(lambda a: crm_access.editor_asset(a, actor)) if request.state.editor else None,
                )
                plan = real_idea_plan(found, default_title(idea), kind)
                return page(request, db, "plan", idea_form=values | {"prompt": idea}, plan=plan)
            if uploads and values["upload_use"] == "as_is":
                videos = [a for a in uploads if a.media["mime"].startswith("video/")]
                silent = len(videos) == 1 and not await run_in_threadpool(
                    has_sound, storage(settings).read(videos[0].storage_key)
                )
                plan = uploaded_plan(idea, uploads, requested_style=values["kind"] or "auto", silent=silent)
                return page(request, db, "plan", idea_form=values | {"prompt": idea}, plan=plan)
            references = uploads  # Chosen and added files guide the new AI media.
            snapshot = await run_in_threadpool(website_snapshot, settings) if values["website"] else {}
            assets = db.scalars(
                select(Asset).where(Asset.removed_at.is_(None)).order_by(Asset.created_at.desc())
            ).all()
            plan = await run_in_threadpool(
                plan_idea,
                db,
                settings,
                idea=idea,
                requested_format=values["format"] or "auto",
                requested_style=values["kind"] or "auto",
                product=values["product"] or "company",
                facts_id=values["facts_id"],
                snapshot=snapshot,
                has_video_reference=any(a.media["mime"].startswith("video/") for a in references),
                has_logo=bool(logo_assets(assets)),
                platforms=values["platforms"],
            )
        except ValueError as exc:
            db.rollback()
            return page(request, db, "create", error_message=friendly(exc), idea_form=values, status_code=400)
        return page(request, db, "plan", idea_form=values | {"prompt": idea}, plan=plan)

    def plan_from_form(form):
        places, systems = form.getlist("shot_setting"), form.getlist("shot_system")
        configurations = form.getlist("shot_configuration")
        # A page opened before the configuration field existed still submits its clips.
        configurations += [""] * (len(places) - len(configurations))
        shots = [
            Shot(setting=setting, system=system, configuration=configuration)
            for setting, system, configuration in zip(places, systems, configurations, strict=False)
            if str(setting).strip() or str(system).strip()
        ]
        operation = str(form.get("operation", ""))
        try:
            duration = int(str(form.get("duration", "10")))
        except ValueError:
            duration = 10
        silent = form.get("silent_video") == "on"
        real_format = str(form.get("real_format", ""))
        real_format = real_format if operation == "real" and real_format in {"photo", "video"} else ""

        def saved(name, default):
            # The plan page carries the planned edit and the search's matches back unchanged.
            try:
                value = json.loads(str(form.get(name) or "null"))
            except ValueError:
                return default
            return value if isinstance(value, type(default)) else default

        return IdeaPlan(
            real_format=real_format,
            real_edit=saved("real_plan", {}) if real_format else {},
            real_matches=[m for m in saved("real_matches", []) if isinstance(m, dict)][:40] if real_format else [],
            real_sounds=[str(s) for s in form.getlist("real_sounds")][:3] if real_format else [],
            as_uploaded=form.get("as_uploaded") == "on",
            ambient_music=silent or form.get("sound") == "ambient",
            silent_video=silent,
            title=str(form.get("campaign", "")),
            operation=operation if operation in {"image", "video", "video_edit"} or real_format else "image",
            duration=max(3, min(90, duration)),
            kind="non_promotional" if form.get("plan_kind") == "non_promotional" else "promotional",
            shots=shots[:MAX_CLIPS],
            soundtrack=str(form.get("soundtrack", ""))[:1800],
            # Every video ends with a cut to the logo.
            end_card=operation in {"video", "video_edit"} or real_format == "video",
            available_systems=[str(s) for s in form.getlist("available_systems")][:40],
            notes=[str(n) for n in form.getlist("notes")][:8],
            conflicts=[str(c) for c in form.getlist("conflicts")][:6],
            planner=str(form.get("planner", ""))[:100],
        )

    def real_selection(form, plan):
        """The real project media chosen on the plan page: (asset IDs, the planned edit with the admin's
        trims, or {} when the edit must be planned again, the finished length in seconds, and the planned
        edit with the admin's trims and left-out clips whatever else changed)."""
        planned = plan.real_edit if isinstance(plan.real_edit, dict) else {}
        # Sent back by the browser: anything that isn't an item is kept as it is, so the edit fails
        # validation when it's queued and is planned again.
        items = planned.get("items", []) if isinstance(planned.get("items"), list) else []
        picks = [str(i) for i in form.getlist("real_media")]
        listed = "clip_rows" in form  # A page from before clips could be trimmed sends real_media only.
        ticked = {str(v) for v in form.getlist("real_clip")}

        def seconds(name):
            try:
                value = float(str(form.get(name, "")))
            except ValueError:
                return None
            return value if math.isfinite(value) and 0 <= value <= 3600 else None

        def number(value):
            try:
                return float(value or 0)
            except (TypeError, ValueError):
                return 0.0

        kept, edited = [], False
        for index, item in enumerate(items):
            asset_id = item.get("asset_id") if isinstance(item, dict) else None
            if (str(index) not in ticked) if listed else (asset_id not in picks):
                edited = True
                continue
            if isinstance(item, dict):
                item = dict(item)
                for key in ("start", "end"):
                    value = seconds(f"clip_{key}_{index}")
                    # The page shows times to a tenth of a second, so smaller differences are rounding.
                    if value is not None and abs(value - number(item.get(key))) > 0.06:
                        item[key], edited = round(value, 2), True
            kept.append(item)
        planned_ids = {i.get("asset_id") for i in items if isinstance(i, dict)}
        added = [i for i in picks if i not in planned_ids]
        chosen = list(
            dict.fromkeys([str(i["asset_id"]) for i in kept if isinstance(i, dict) and i.get("asset_id")] + picks)
        )[:MAX_IDEA_PICKS]
        same_shape = str(form.get("planned_format", "")) == plan.real_format
        duration = plan.duration
        length_kept = str(form.get("planned_duration", "")) == str(plan.duration)
        if added and length_kept and plan.real_format == "video":
            # The planned length fitted the planned clips; added files get room, up to the idea's length.
            duration = room_for(settings, str(form.get("prompt", "")), plan_platforms(form), plan.duration)
        if kept and not added and same_shape and edited:
            # Clips trimmed or left out by hand: the edit is kept, and the video is as long as its clips.
            if plan.real_format == "video":
                footage = sum(
                    max(0.0, number(i.get("end")) - number(i.get("start"))) for i in kept if isinstance(i, dict)
                )
                longest = min(profiles(settings)[p].max_duration for p in plan_platforms(form))
                duration = int(max(END_CARD_SECONDS + MIN_CLIP_SECONDS, min(longest, math.ceil(footage) + END_CARD_SECONDS)))
            return chosen, planned | {"items": kept}, duration, planned | {"items": kept}
        unchanged = bool(kept) and not added and same_shape and length_kept
        return chosen, (planned | {"items": kept}) if unchanged else {}, duration, planned | {"items": kept}

    def plan_platforms(form):
        return [p for p in form.getlist("platforms") if p in PLATFORMS] or ["instagram"]

    def real_idea_plan(found, title, kind):
        return IdeaPlan(
            title=title,
            operation="real",
            real_format=found["real_format"],
            duration=found["duration"],
            kind=kind,
            end_card=found["real_format"] == "video",
            notes=found["notes"],
            planner=found["planner"],
            real_edit=found["edit"],
            real_matches=found["matches"],
            real_sounds=found["sounds"],
        )

    def check_real_picks(request, db, chosen):
        if not chosen:
            raise ValueError("Tick at least one photo or video to use")
        for asset_id in chosen:
            asset = db.get(Asset, asset_id)
            if asset is None or asset.removed_at is not None:
                raise ValueError("A chosen file is no longer in the asset library. Plan the idea again.")
            if request.state.editor and not crm_access.editor_asset(asset, request.state.actor):
                raise HTTPException(404, "Media not found")

    async def replan_real(request, db, form, values, plan):
        """Plan the real project edit again on the plan page with the media ticked there, the length and
        format chosen there, and the admin's description of what to change."""
        chosen, _, duration, trimmed = real_selection(form, plan)
        check_real_picks(request, db, chosen)
        change = " ".join(str(form.get("edit_request", "")).split())
        if len(change) > 1000:
            raise ValueError("Keep the changes under 1,000 characters")
        planned_ids = {i.get("asset_id") for i in trimmed.get("items", []) if isinstance(i, dict)}
        # Files ticked in the library list are added to the edit the admin sees, trims included; with no
        # changes described and nothing added, the edit is planned afresh.
        request_text = change or (
            "Add the newly ticked files where they fit best, and keep the rest of the edit."
            if set(chosen) - planned_ids else ""
        )
        actor = request.state.actor
        idea = values["prompt"]
        found = await run_in_threadpool(
            plan_real_idea,
            db,
            settings,
            idea=idea,
            title=plan.title,
            uploads=load_assets(db, chosen),
            platforms=values["platforms"],
            product=values["product"] or "company",
            kind=plan.kind,
            rights=values["rights"],
            requested=plan.real_format,
            signals=read_signals(idea) | {"seconds": duration},
            allowed=(lambda a: crm_access.editor_asset(a, actor)) if request.state.editor else None,
            revision={"request": request_text, "previous_edit": trimmed} if request_text and trimmed.get("items") else None,
            replanning=True,
        )
        # The search's matches stay first in the library list.
        found["matches"] = plan.real_matches
        if change:
            found["notes"].insert(0, f"Planned again with your changes: {change}")
        result = real_idea_plan(found, plan.title, plan.kind)
        return page(request, db, "plan", idea_form=values, plan=result)

    @app.post("/forms/ideas/create", dependencies=mutation)
    async def create_from_plan(request: Request, db: DB):
        form = await request.form()
        values = idea_values(form)
        check_editor_inputs(request, db, form, values)
        mode = str(form.get("mode", "once"))
        if mode == "back":
            return page(request, db, "create", idea_form=values)
        try:
            plan = plan_from_form(form)
            if mode == "replan" and plan.operation == "real":
                return await replan_real(request, db, form, values, plan)
            if mode not in {"once", "next"}:
                raise ValueError("Choose Create now or Use for next automatic post")
            video = plan.operation in {"video", "video_edit"} or plan.real_format == "video"
            # Blank picks the newest logo; with none in the library, queueing explains how to add it.
            logo_id = str(form.get("logo_id", "")) if video else ""
            if video and request.state.editor and not logo_id:
                allowed_logos = logo_assets([a for a in db.scalars(select(Asset).where(Asset.removed_at.is_(None))
                                                                   .order_by(Asset.created_at.desc()))
                                            if crm_access.editor_asset(a, request.state.actor)])
                if not allowed_logos:
                    raise ValueError("Ask an admin to add a company logo before creating a video")
                logo_id = allowed_logos[0].id
            uploads = idea_uploads(db, values)
            sounds = [a.id for a in uploads if a.media["mime"].startswith("audio/")]
            pictures = [a.id for a in uploads if a.id not in sounds]
            if plan.operation == "real":
                # The plan page's edit is used, with any trims, for the media, format and length it was planned
                # for; added files or another format or length plan it again when the job runs.
                chosen, prepared, duration, _ = real_selection(form, plan)
                check_real_picks(request, db, chosen)
                brief = CreativeBrief(
                    campaign=plan.title,
                    prompt=values["prompt"],
                    operation="real",
                    platforms=values["platforms"],
                    facts_id=values["facts_id"],
                    product=values["product"] or "company",
                    kind=plan.kind,
                    rights=values["rights"],
                    owner=values["owner"],
                    reference_ids=chosen,
                    audio_reference_ids=[s for s in plan.real_sounds if s in sounds] if video else [],
                    duration=duration if video else 5,
                    website=False,
                    create_posts=True,
                    workflow={"once": "immediate", "next": "next"}[mode],
                    end_card_asset_id=logo_id if video else "",
                    real_format=plan.real_format,
                    prepared_edit=prepared,
                )
                await run_in_threadpool(queue_prompt, db, brief, request.state.prompt_account)
                db.commit()
                return RedirectResponse("/admin/create#my-posts" if request.state.editor else "/admin/media-queue", status_code=303)
            if plan.as_uploaded:
                brief = CreativeBrief(
                    campaign=plan.title,
                    prompt=values["prompt"],
                    operation=plan.operation,
                    platforms=values["platforms"],
                    facts_id=values["facts_id"],
                    product=values["product"] or "company",
                    kind=plan.kind,
                    rights=values["rights"],
                    owner=values["owner"],
                    reference_ids=pictures,
                    audio_reference_ids=sounds if video else [],
                    duration=plan.duration if video else 5,
                    website=False,
                    create_posts=True,
                    workflow={"once": "immediate", "next": "next"}[mode],
                    end_card_asset_id=logo_id,
                    as_uploaded=True,
                    ambient_music=video and plan.ambient_music and not sounds,
                )
                await run_in_threadpool(queue_prompt, db, brief, request.state.prompt_account)
                db.commit()
                return RedirectResponse("/admin/create#my-posts" if request.state.editor else "/admin/media-queue", status_code=303)
            brief = CreativeBrief(
                campaign=plan.title,
                prompt=values["prompt"],
                operation=plan.operation,
                platforms=values["platforms"],
                facts_id=values["facts_id"],
                product=values["product"] or "company",
                kind=plan.kind,
                rights=values["rights"],
                owner=values["owner"],
                reference_ids=pictures,
                audio_reference_ids=sounds if video else [],
                audio_prompt=plan.soundtrack.strip() if video else "",
                duration=plan.duration if video else 5,
                website=values["website"] == "on",
                create_posts=True,
                workflow={"once": "immediate", "next": "next"}[mode],
                shots=plan.shots if plan.operation == "video" else plan.shots[:1] if not video else [],
                end_card_asset_id=logo_id,
            )
            await run_in_threadpool(queue_prompt, db, brief, request.state.prompt_account)
            db.commit()
        except ValueError as exc:
            db.rollback()
            return page(
                request,
                db,
                "plan",
                error_message=friendly(exc),
                idea_form=values,
                plan=plan_from_form(form),
                status_code=400,
            )
        return RedirectResponse("/admin/create#my-posts" if request.state.editor else "/admin/media-queue", status_code=303)

    @app.post("/forms/media-campaigns/{campaign_id}", dependencies=mutation)
    def toggle_campaign(campaign_id: str, db: DB, enabled: Annotated[str, Form()] = ""):
        changed = db.execute(update(MediaCampaign).where(MediaCampaign.id == campaign_id)
                             .values(enabled=enabled == "true", error=""))
        if not changed.rowcount:
            raise HTTPException(404)
        db.commit()
        return RedirectResponse("/admin/media-studio", status_code=303)

    def queue_location(request):
        return list_location("media-queue", request.query_params.get("campaign_id"))

    @app.post("/forms/media-jobs/{job_id}/{action}", dependencies=mutation)
    async def media_job_action(job_id: str, action: str, request: Request, db: DB):
        job = db.scalar(select(MediaJob).where(MediaJob.id == job_id).with_for_update())
        if not job:
            raise HTTPException(404)
        form = await request.form()
        try:
            if action == "resume":
                if task_failed(job):
                    raise ValueError("Runway already reported this task as failed. Use Generate again instead.")
                resume_job(job)
            elif action == "resubmit":
                retry_step(db, job, form.get("prompt", ""), audio_model=form.get("audio_model", ""))
            elif action == "approve_scene":
                from .scene_generation import approve_scene

                approve_scene(job, request.state.actor, form.get("note", ""))
            elif action == "reconcile" and job.state == "uncertain" and not job.task_id:
                import re

                task_id = str(form.get("task_id", ""))
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id):
                    raise ValueError("Enter the task ID returned by the original Runway submission")
                job.task_id, job.state, job.submitted_at, job.error = task_id, "running", now(), ""
            elif action == "cancel" and job.state in {"planned", "queued", "failed", "uncertain"}:
                # Conditional write prevents a late UI cancellation racing a provider submission.
                changed = db.execute(
                    update(MediaJob)
                    .where(MediaJob.id == job.id, MediaJob.state == job.state)
                    .values(state="cancelled")
                )
                if not changed.rowcount:
                    raise PostConflict("This media job has changed. Refresh before cancelling.")
            elif action == "dismiss" and job.state == "complete":
                dismiss_completed(db, job, form.get("post_id") or None)
            else:
                raise ValueError("This job can't do that now. Refresh the page to see its current state.")
            db.commit()
        except (ValueError, PostConflict) as exc:
            db.rollback()
            return page(request, db, "media-queue", error_message=str(exc), status_code=400)
        return RedirectResponse(queue_location(request), status_code=303)

    @app.post("/api/posts", dependencies=mutation)
    def new_post(payload: DraftInput, db: DB):
        values = payload.model_dump()
        try:
            post = compose_post(db, values)
        except ValueError as exc:
            db.rollback()
            raise HTTPException(400, str(exc)) from None
        return {"id": post.id, "status": post.status}

    def action_impl(db, post, action, actor, parts=None, revision_request="", reviewed_digest="", confirmed_removed=False):
        if action in {"check", "approve", "reject", "edit", "tiktok_status"}:
            lock_post(db, post)
        if action in {"check", "approve"} and not post.caption:
            db.rollback()
            raise ValueError("This post has no caption yet. Write a new caption first.")
        if action == "generate":
            generate(db, settings, post)
        elif action == "rewrite":
            rewrite_caption(db, settings, post, actor, revision_request)
        elif action == "retry":
            post = retry_post(db, settings, post, actor)
        elif action == "availability":
            from .availability import check_post

            check_post(db, settings, post, actor)
        elif action == "repost":
            from .reposts import create

            post = create(db, settings, post, actor, confirmed_removed)
        elif action in {"delete", "restart"}:
            delete_draft(db, post, actor)
        elif action == "approve":
            from .human_review import approve

            approve(db, settings, post, actor, reviewed_digest)
            if post.status == "approved":
                schedule(db, settings, post)
        elif action == "stop":
            from .human_review import stop

            stop(db, post, actor)
        elif action == "check":
            if post.status in {"held", "awaiting_review"}:
                transition(db, post, "generated", "Requested automated recheck", actor)
            run_gate(db, settings, post)
        elif action == "schedule":
            schedule(db, settings, post)
        elif action == "reject":
            transition(db, post, "failed", "Rejected by internal user", actor)
            db.commit()
        elif action == "edit":
            if post.status not in {"held", "draft", "awaiting_review"}:
                raise ValueError("Only held/draft posts are editable")
            from .human_review import require_no_replacement

            require_no_replacement(db, post)
            if post.status != "draft":
                transition(db, post, "draft", "Edited; approval invalidated", actor)
            facts = Facts.model_validate(db.get(FactSheet, post.facts_id).document)
            post.parts = parts
            post.fact_ids = caption_fact_ids(parts)
            post.ai_disclosure = any(requires_ai_review(a) for a in load_assets(db, post.asset_ids))
            post.caption = render(parts, profiles(settings)[post.platform], facts, post.platform, settings)
            post.approved_digest = ""
            post.human_review_required, post.human_approved_digest = True, ""
            transition(db, post, "generated", "Edited caption and fact citations", actor)
            db.commit()
            run_gate(db, settings, post)
        elif action == "finish_video":
            # Posts made before every video had sound and the logo ending: the app adds them itself.
            if post.status not in {"held", "awaiting_review"} or not post.caption:
                raise ValueError("Only a held or unapproved post with a caption can get the logo ending and sound")
            from .human_review import require_no_replacement

            require_no_replacement(db, post)
            if needs_runway_music(db, post):
                # A generated video gets its ambient music from Runway, and the logo ending when it is
                # assembled again; the new post replaces this one.
                replace_part(db, post, "music", "", actor)
                return {"id": post.id, "status": post.status}
            assets = load_assets(db, post.asset_ids)
            if None in assets:
                raise ValueError("Media this post uses is missing from the asset library")
            finished = [finish_video(db, settings, a, post.platform) for a in assets]
            if [a.id for a in finished] == post.asset_ids:
                raise ValueError("This post's video already has sound and ends with the Tudelu logo")
            transition(db, post, "draft", "Added the logo ending and sound; approval invalidated", actor)
            post.asset_ids = [a.id for a in finished]
            post.ai_disclosure = any(requires_ai_review(a) for a in finished)
            post.approved_digest = ""
            post.human_review_required, post.human_approved_digest = True, ""
            transition(db, post, "generated", "Video now has sound and ends with the Tudelu logo", actor)
            db.commit()
            run_gate(db, settings, post)
        elif action == "tiktok_status":
            if post.platform != "tiktok" or post.status != "awaiting_manual":
                raise ValueError("Only TikTok inbox posts may be reconciled here")
            if post.dry_run:
                raise ValueError("Simulated inbox delivery cannot be confirmed as public")
            from .adapters import adapter

            selected = adapter(settings, db, post)
            result = selected.get_status(post.remote_id)["data"]
            if result["status"] == "PUBLISH_COMPLETE":
                ids = result.get("publicaly_available_post_id", [])
                if ids:
                    from .models import PostAvailability

                    availability = db.get(PostAvailability, post.id)
                    if availability is None:
                        availability = PostAvailability(post_id=post.id)
                        db.add(availability)
                    availability.public_ids = [str(i) for i in ids]
                # The status endpoint does not return a canonical profile/share URL.
                # Use a platform-returned URL when the optional Display API scope exists.
                if ids and "video.list" in selected.credentials.get("scope", ""):
                    try:
                        videos = selected.direct(
                            "POST",
                            f"{selected.base}/video/query/",
                            params={"fields": "id,share_url"},
                            json={"filters": {"video_ids": [str(ids[0])]}},
                        )["data"]["videos"]
                        post.live_url = videos[0].get("share_url", "") if videos else ""
                    except Exception:
                        pass  # Publication is confirmed even when its share URL is unavailable.
                transition(db, post, "published", "TikTok API confirmed manual publication", actor)
                db.commit()
            elif result["status"] == "FAILED":
                transition(db, post, "failed", "TikTok API reports failed delivery", actor)
                db.commit()
        else:
            raise ValueError("Unknown action")
        return {"id": post.id, "status": post.status}

    @app.post("/api/posts/{post_id}/{action}", dependencies=mutation)
    def post_action(post_id: str, action: str, request: Request, db: DB, parts: list[dict] | None = None, revision_request: str = "", reviewed_digest: str = "", confirmed_removed: bool = False):
        post = db.get(Post, post_id)
        if not post or post.status == "deleted":
            raise HTTPException(404)
        return action_impl(db, post, action, request.state.actor, parts, revision_request, reviewed_digest, confirmed_removed)

    def compose_post(db, values):
        asset_ids = values["asset_ids"]
        assets = load_assets(db, asset_ids)
        if not assets or None in assets:
            raise ValueError("Choose a photo or video from the asset library")
        if values["platform"] in REAL_MEDIA_ONLY:
            # Real Drive media only; a resized copy is made when the original doesn't fit the platform.
            if error := real_drive_media_error(db, assets):
                raise ValueError(error)
            asset_ids = [platform_copy(db, settings, a, values["platform"]).id for a in assets]
        elif values["platform"] in PLATFORMS:
            # Every video has sound and ends with the logo; a copy gets them when the original lacks them.
            asset_ids = [finish_video(db, settings, a, values["platform"]).id for a in assets]
        return draft(
            db,
            values["platform"],
            values["campaign"],
            asset_ids,
            values["facts_id"],
            values["product"],
            values["kind"],
        )

    @app.post("/forms/posts", dependencies=mutation)
    async def compose(request: Request, db: DB):
        form = await request.form()
        values = {k: str(form.get(k, "")) for k in ("campaign", "platform", "kind", "product", "facts_id")}
        values["asset_ids"] = form.getlist("asset_ids")
        try:
            post = await run_in_threadpool(compose_post, db, values)
        except ValueError as exc:
            db.rollback()
            return page(request, db, "compose", error_message=str(exc), asset_form=values, status_code=400)
        return RedirectResponse(f"/posts/{post.id}", status_code=303)

    @app.post("/forms/posts/{post_id}/clips/{part}/regenerate", dependencies=mutation)
    @app.post("/forms/posts/{post_id}/parts/{part}/regenerate", dependencies=mutation)
    async def regenerate_part(post_id: str, part: str, request: Request, db: DB):
        post = db.get(Post, post_id)
        if not post or post.status == "deleted":
            raise HTTPException(404)
        form = await request.form()
        try:
            if post_processing(engine, post_id):
                raise PostBusy("This post is being processed")
            await run_in_threadpool(replace_part, db, post, part, str(form.get("prompt", "")), request.state.actor)
        except PostBusy:
            db.rollback()
            return page(request, db, "post", post, post_busy=True, status_code=202)
        except PostConflict:
            db.rollback()
            query = source_query(request.query_params.get("source"), request.query_params.get("campaign_id"))
            return RedirectResponse(f"/posts/{post_id}{query}", status_code=303)
        except ValueError as exc:
            db.rollback()
            return page(request, db, "post", post, error_message=str(exc), status_code=400)
        return RedirectResponse(queue_location(request), status_code=303)

    @app.post("/forms/posts/{post_id}/change-media", dependencies=mutation)
    async def change_media(post_id: str, request: Request, db: DB):
        post = db.get(Post, post_id)
        if not post or post.status == "deleted":
            raise HTTPException(404)
        form = await request.form()
        text = str(form.get("change_request", ""))
        picks = list(dict.fromkeys(str(i) for i in form.getlist("real_media")))
        try:
            if post_processing(engine, post_id):
                raise PostBusy("This post is being processed")
            await run_in_threadpool(request_changes, db, settings, post, text, request.state.actor, picks)
        except PostBusy:
            db.rollback()
            return page(request, db, "post", post, post_busy=True, status_code=202)
        except PostConflict:
            db.rollback()
            query = source_query(request.query_params.get("source"), request.query_params.get("campaign_id"))
            return RedirectResponse(f"/posts/{post_id}{query}", status_code=303)
        except ValueError as exc:
            db.rollback()
            return page(
                request, db, "post", post, error_message=str(exc), status_code=400,
                post_values={"change_request": text[:2000], "change_picks": picks},
            )
        return RedirectResponse(queue_location(request), status_code=303)

    @app.post("/forms/posts/{post_id}/{action}", dependencies=mutation)
    async def form_action(post_id: str, action: str, request: Request, db: DB):
        form = await request.form()
        source = request.query_params.get("source")
        campaign_id = request.query_params.get("campaign_id")
        query = source_query(source, campaign_id)
        queue_delete = action == "delete" and form.get("return_to") == "/admin/media-queue"
        after_delete = list_location(source, campaign_id) if query else "/"
        if queue_delete:
            after_delete = queue_location(request)
        post = db.get(Post, post_id)
        if post and post.status == "deleted" and action == "delete" and (queue_delete or query):
            return RedirectResponse(after_delete, status_code=303)
        if not post or post.status == "deleted":
            raise HTTPException(404)
        parts = json.loads(form["parts"]) if "parts" in form else None
        try:
            if post_processing(engine, post_id):
                raise PostBusy("This post is being processed")
            result = await run_in_threadpool(action_impl, db, post, action, request.state.actor, parts, str(form.get("revision_request", "")), str(form.get("reviewed_digest", "")), form.get("confirmed_removed") == "on")
        except PostBusy:
            db.rollback()
            if queue_delete:
                return page(request, db, "media-queue", error_message="This post is being processed. Try again when it finishes.", status_code=409)
            return page(request, db, "post", post, post_busy=True, status_code=202)
        except PostConflict as exc:
            db.rollback()
            if db.get(Post, post_id).status == "deleted":
                return RedirectResponse(after_delete, status_code=303)
            if queue_delete:
                return page(request, db, "media-queue", error_message=str(exc), status_code=409)
            # Another tab or the worker completed the action; display its saved result.
            return RedirectResponse(f"/posts/{post_id}{query}", status_code=303)
        except ValueError as exc:
            db.rollback()
            if queue_delete:
                return page(request, db, "media-queue", error_message=str(exc), status_code=400)
            return page(request, db, "post", post, error_message=str(exc), status_code=400)
        if action == "restart" and post.status == "deleted":
            return RedirectResponse(f"/admin/create?from_post={post.id}", status_code=303)
        if action == "approve" and source == "media-queue" and post.status in DELIVERY_STATES:
            following = next_review(db, post, campaign_id)
            request.session["notice"] = f"Approved {post.campaign} for {post.platform}. Track delivery in Published posts."
            target = f"/posts/{following}{query}" if following else published_location(post, campaign_id)
            return RedirectResponse(target, status_code=303)
        return RedirectResponse(after_delete if post.status == "deleted" else f"/posts/{result['id']}{query}", status_code=303)

    def return_to(form, default):
        # Only fixed in-app pages; never an arbitrary redirect target.
        target = str(form.get("return_to", ""))
        return target if target in {"/", "/admin/automation", "/admin/connections", "/admin/queues"} else default

    @app.post("/forms/live-platform", dependencies=mutation)
    async def live_platform(request: Request, db: DB):
        form = await request.form()
        platform, value = str(form.get("platform", "")), str(form.get("enabled", ""))
        account = db.get(Account, platform)
        if not account or value not in {"true", "false"}:
            raise HTTPException(400, "Select a platform and a valid live-posting setting")
        enabled = value == "true"
        if enabled:
            if not connections.live_controls_available(settings):
                raise HTTPException(400, "Configure authenticated access before enabling live posting")
            if not connections.account_connected(settings, account):
                raise HTTPException(400, "Connect and validate this platform before enabling live posting")
        account.live_enabled = enabled
        db.commit()
        event("platform_live_changed", platform=platform, enabled=enabled, actor=request.state.actor)
        return RedirectResponse(return_to(form, "/admin/connections"), status_code=303)

    @app.post("/forms/automation", dependencies=mutation)
    async def automation_control(request: Request, db: DB):
        form = await request.form()
        value = str(form.get("enabled", ""))
        if value not in {"true", "false"}:
            raise HTTPException(400, "Choose a valid automatic-post setting")
        control = db.get(Control, 1)
        control.automation_enabled = value == "true"
        db.commit()
        event("automation_changed", enabled=control.automation_enabled, actor=request.state.actor)
        return RedirectResponse(return_to(form, "/admin/automation"), status_code=303)

    @app.post("/forms/control", dependencies=mutation)
    async def control(request: Request, db: DB):
        form = await request.form()
        if form.get("platform"):
            account = db.get(Account, form["platform"])
            if not account:
                raise ValueError("Unknown platform")
            account.paused = form["enabled"] == "true"
            db.commit()
        else:
            set_kill(db, form["enabled"] == "true", request.state.actor)
        return RedirectResponse(return_to(form, "/admin/automation" if form.get("platform") else "/"), status_code=303)

    @app.post("/forms/facts", dependencies=mutation)
    async def facts_editor(request: Request, db: DB):
        form = await request.form()
        save_facts(db, yaml.safe_load(form["document"]))
        db.commit()
        return RedirectResponse("/admin/facts", status_code=303)

    @app.post("/forms/assets", dependencies=mutation)
    async def upload(request: Request, db: DB):
        form = await request.form()
        file = form.get("file")
        values = media_form_values(form)
        try:
            if not isinstance(file, UploadFile) or not file.filename:
                raise ValueError("Choose a media file to upload")
            values["filename"] = file.filename
            arguments = media_form_arguments(values)
            if file.size is not None and file.size > settings.max_upload_bytes:
                raise ValueError(
                    f"The file exceeds the {settings.max_upload_bytes / 1_000_000:g} MB upload limit"
                )
            data = await file.read(settings.max_upload_bytes + 1)
            await run_in_threadpool(ingest, db, settings, data, file.filename, **arguments)
            db.commit()
        except Exception as exc:
            return upload_failed(request, db, exc, upload_form=values)
        finally:
            if isinstance(file, UploadFile):
                await file.close()
        return RedirectResponse("/admin/assets", status_code=303)

    def library_url(form):
        # Back to the library page with the same filters; only its own query keys are kept.
        from urllib.parse import parse_qsl, urlencode

        keys = {"type", "product", "source", "rights", "q", "sort", "page"}
        query = urlencode([(k, v) for k, v in parse_qsl(str(form.get("back", ""))[:2000]) if k in keys])
        return "/admin/assets" + ("?" + query if query else "") + "#library"

    @app.post("/forms/assets/delete-files", dependencies=mutation)
    async def delete_media(request: Request, db: DB):
        from .library import delete_assets, outcome

        form = await request.form()
        try:
            result = await run_in_threadpool(
                delete_assets, db, settings, form.getlist("asset_ids"), request.state.actor
            )
        except ValueError as exc:
            db.rollback()
            return page(request, db, "assets", error_message=str(exc), status_code=400)
        request.session["notice"] = outcome(result)
        return RedirectResponse(library_url(form), status_code=303)

    @app.post("/forms/assets/restore-files", dependencies=mutation)
    async def restore_media(request: Request, db: DB):
        from .library import restore_assets

        form = await request.form()
        restored = restore_assets(db, form.getlist("asset_ids"))
        request.session["notice"] = (
            f"Restored {restored} file{'' if restored == 1 else 's'} to the library." if restored
            else "Nothing was restored: choose deleted files."
        )
        return RedirectResponse(library_url(form), status_code=303)

    @app.post("/forms/google-drive/files/{file_id}/import-again", dependencies=mutation)
    def import_drive_file_again(file_id: str, request: Request, db: DB):
        from sqlalchemy.orm.exc import StaleDataError

        from .drive_import import DriveError, import_again

        try:
            import_again(db, file_id)
        except DriveError as exc:
            db.rollback()
            raise HTTPException(404, str(exc)) from None
        except StaleDataError:
            db.rollback()
            raise HTTPException(409, "Import progress changed; retry this action") from None
        request.session["notice"] = "The file will be imported on the next Google Drive scan, which starts shortly."
        return RedirectResponse("/admin/assets#drive", status_code=303)

    def media_form_values(form):
        return {
            key: str(form.get(key, ""))
            for key in (
                "source",
                "rights",
                "owner",
                "rights_expire_at",
                "generator",
                "prompt",
                "tags",
                "customer_permission",
                "permission_reference",
                "key",
                "brand_logo",
                MUSIC_TAG,
            )
        }

    def media_form_arguments(values):
        from datetime import datetime

        try:
            tags = json.loads(values["tags"] or "{}")
        except ValueError:
            raise ValueError('Tags must be a JSON object, for example {"product": "company"}.') from None
        if not isinstance(tags, dict):
            raise ValueError("Tags must be a JSON object")
        if values.get("brand_logo") == "on":
            # Marks the official logo used for video end cards.
            tags = tags | {"brand_logo": True}
        if values.get(MUSIC_TAG) == "on":
            # Real ambient music that silent real videos can use.
            tags = tags | {MUSIC_TAG: True}
        try:
            expiry = (
                datetime.fromisoformat(values["rights_expire_at"]) if values["rights_expire_at"] else None
            )
        except ValueError:
            raise ValueError("Enter a valid rights expiry date and time, or leave it blank") from None
        return {
            "source": values["source"] or "company",
            "rights": usage_rights(values["rights"]),
            "owner": owner_value(values["owner"]),
            "rights_expire_at": expiry,
            "generator": values["generator"] or None,
            "prompt": values["prompt"] or None,
            "tags": tags,
            "customer_permission": values["customer_permission"] == "on",
            "permission_reference": values["permission_reference"],
        }

    def upload_failed(request, db, exc, *, upload_form=None, bucket_form=None):
        db.rollback()
        status, message = describe_upload_error(exc)
        event("asset_upload_failed", stage="import" if bucket_form else "save", **upload_error_details(exc))
        try:
            return page(
                request,
                db,
                "upload",
                error_message=message,
                upload_form=upload_form,
                bucket_form=bucket_form,
                status_code=status,
            )
        except SQLAlchemyError:
            db.rollback()
            return templates.TemplateResponse(
                request=request,
                name="upload_unavailable.html",
                context={"error_message": message},
                status_code=503,
            )

    @app.post("/forms/import-bucket-asset", dependencies=mutation)
    async def import_bucket_asset(request: Request, db: DB):
        from .storage import storage

        form = await request.form()
        values = media_form_values(form)
        try:
            if settings.storage_backend != "s3":
                raise ValueError("Bucket import requires STORAGE_BACKEND=s3")
            key = values["key"].strip()
            if not key or len(key) > 1024:
                raise ValueError("Enter the existing bucket object key")
            arguments = media_form_arguments(values)
            arguments["tags"] = arguments["tags"] | {"imported_bucket_key": key}

            def import_existing():
                store = storage(settings)
                try:
                    if not 0 < store.head(key)["ContentLength"] <= settings.max_upload_bytes:
                        raise ValueError("Bucket object is empty or exceeds MAX_UPLOAD_BYTES")
                    ingest(db, settings, store.read(key), key, **arguments)
                    db.commit()
                finally:
                    store.client.close()

            await run_in_threadpool(import_existing)
        except Exception as exc:
            return upload_failed(request, db, exc, bucket_form=values)
        return RedirectResponse("/admin/assets", status_code=303)

    @app.post("/forms/google-drive", dependencies=mutation)
    async def configure_google_drive(request: Request, db: DB):
        from .drive_import import DriveError, save_source

        form = dict(await request.form())
        try:
            save_source(db, form)
        except DriveError as exc:
            db.rollback()
            return page(request, db, "assets", error_message=str(exc), drive_form=form, status_code=400)
        return RedirectResponse("/admin/assets", status_code=303)

    @app.post("/forms/google-drive/{action}", dependencies=mutation)
    def control_google_drive(action: str, db: DB):
        from sqlalchemy.orm.exc import StaleDataError

        from .db import now
        from .models import DriveSource

        source = db.get(DriveSource, 1)
        if not source or action not in {"pause", "resume", "scan"}:
            raise HTTPException(404, "Drive connection or action not found")
        if action == "pause":
            source.enabled = False
        else:
            source.enabled = True
            source.next_poll_at = now()
            source.error = ""
        # Even an already-paused action invalidates an in-flight worker snapshot.
        source.revision += 1
        try:
            db.commit()
        except StaleDataError:
            db.rollback()
            raise HTTPException(409, "Import progress changed; retry this action") from None
        return RedirectResponse("/admin/assets", status_code=303)

    @app.post("/forms/generate-asset", dependencies=mutation)
    async def generate_asset(request: Request, db: DB):
        form = await request.form()
        values = {key: str(form.get(key, "")) for key in ("prompt", "kind", "rights", "owner", "tags")}
        try:
            try:
                tags = json.loads(values["tags"] or "{}")
            except ValueError:
                raise ValueError('Tags must be a JSON object, for example {"campaign": "summer"}.') from None
            await run_in_threadpool(
                Providers(settings).generate_asset,
                db,
                values["prompt"],
                values["kind"],
                usage_rights(values["rights"]),
                tags,
                owner_value(values["owner"]),
            )
        except ValueError as exc:
            db.rollback()
            if isinstance(exc, ProviderError):
                event(
                    "asset_generation_failed",
                    provider=exc.provider,
                    category=exc.category,
                    upstream_status=exc.upstream_status,
                    request_id=exc.request_id,
                )
            return page(
                request,
                db,
                "upload",
                error_message=str(exc),
                asset_form=values,
                status_code=exc.status if isinstance(exc, ProviderError) else 400,
            )
        db.commit()
        return RedirectResponse("/admin/assets", status_code=303)

    @app.post("/oauth/{platform}/connect", dependencies=mutation)
    def connect(platform: str, request: Request, db: DB):
        request.session.setdefault("browser_nonce", secrets.token_urlsafe(32))
        url = connections.begin(db, settings, platform, request.session["browser_nonce"])
        # Chrome applies form-action 'self' to cross-origin POST redirects too.
        # Finish the protected POST here, then navigate from a same-origin page.
        return templates.TemplateResponse(
            request=request,
            name="oauth_handoff.html",
            context={
                "authorize_url": url,
                "platform_name": {"x": "X", "linkedin": "LinkedIn", "tiktok": "TikTok"}.get(
                    platform, platform.title()
                ),
            },
        )

    @app.get("/oauth/{platform}/callback")
    def oauth_callback(
        platform: str, request: Request, db: DB, state: str = "", code: str = "", error: str = ""
    ):
        if error or not code:
            raise HTTPException(400, "Authorization was declined or incomplete")
        try:
            connections.callback(db, settings, platform, state, code, request.session.get("browser_nonce", ""))
        except connections.OAuthStateError as exc:
            if "text/html" not in request.headers.get("accept", ""):
                raise
            request.session["notice"] = str(exc)
        return RedirectResponse(
            "/admin/connections", status_code=303,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @app.post("/forms/meta", dependencies=mutation)
    async def meta(request: Request, db: DB):
        form = await request.form()
        await run_in_threadpool(
            connections.save_meta, db, settings, form["token"], form["page_id"], form["instagram_id"]
        )
        return RedirectResponse("/admin/connections", status_code=303)

    @app.post("/forms/import-meta", dependencies=mutation)
    async def import_meta(request: Request, db: DB):
        await run_in_threadpool(connections.import_meta_from_env, db, settings, force=True)
        return RedirectResponse("/admin/connections", status_code=303)

    @app.post("/forms/disconnect/{platform}", dependencies=mutation)
    def disconnect(platform: str, db: DB):
        account = db.get(Account, platform)
        if not account:
            raise HTTPException(404)
        account.credentials, account.validated_at, account.expires_at = "", None, None
        account.refresh_expires_at, account.auth_error, account.paused = None, "Disconnected", True
        db.commit()
        return RedirectResponse("/admin/connections", status_code=303)

    return app


_application = None


async def app(scope, receive, send):
    """Load deployment secrets at ASGI startup, never as an import side effect."""
    global _application
    if _application is None:
        _application = create_app()
    await _application(scope, receive, send)
