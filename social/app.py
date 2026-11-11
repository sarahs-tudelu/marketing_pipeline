import base64
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import yaml
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select, update
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from . import connections
from .config import PLATFORMS, get_settings, profiles
from .db import make_engine, sessions
from .facts import Facts, caption_fact_ids, load_facts, render, save_facts
from .gate import run_gate
from .lifecycle import PostConflict, lock_post, transition
from .media_studio import CreativeBrief, queue_jobs, resume_job, validate_sources
from .models import (
    Account,
    Alert,
    Asset,
    CheckResult,
    Control,
    FactSheet,
    MediaCampaign,
    MediaJob,
    Post,
    RequestRecord,
    Transition,
)
from .monitoring import event, set_kill, setup_logging
from .pipeline import bootstrap, delete_draft, draft, generate
from .provider_errors import ProviderError
from .providers import Providers
from .scheduler import schedule
from .storage import ingest


class DraftInput(BaseModel):
    platform: str
    campaign: str
    asset_ids: list[str]
    facts_id: str
    product: str = "company"
    kind: str = "promotional"


def create_app(settings=None):
    settings = settings or get_settings()
    engine = make_engine(settings.database_dsn)
    factory = sessions(engine)
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    settings.media_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app):
        setup_logging()
        with factory() as db:
            bootstrap(db)
            load_facts(db, settings.facts_path)
            db.commit()
        yield
        if settings.storage_backend == "s3":
            app.state.media_store.client.close()
        engine.dispose()

    app = FastAPI(title="Tudelu Social Studio", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings, app.state.factory = settings, factory

    @app.middleware("http")
    async def auth(request: Request, call_next):
        path = request.url.path
        if path == "/health" or path.startswith(("/media/", "/static/")):
            response = await call_next(request)
            if path.startswith("/media/"):
                response.headers["Cache-Control"] = (
                    "public, max-age=31536000, immutable"
                    if response.status_code in {200, 206, 304}
                    else "no-store"
                )
                response.headers["X-Content-Type-Options"] = "nosniff"
            return response
        if path.startswith("/oauth/") and path.endswith("/callback"):
            return await call_next(request)  # Bound state + signed session checked at callback.
        user = "local-developer"
        valid = False
        if settings.auth_mode == "dev":
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

    DB = Annotated[object, Depends(db_session)]

    async def csrf(request: Request):
        supplied = request.headers.get("x-csrf-token", "")
        if not supplied and request.headers.get("content-type", "").split(";")[0] in {
            "application/x-www-form-urlencoded",
            "multipart/form-data",
        }:
            supplied = str((await request.form()).get("csrf", ""))
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
        return {"status": "ok"}

    def page(request, db, view, post_id=None, *, error_message="", asset_form=None, status_code=200):
        request.session.setdefault("csrf", secrets.token_urlsafe(32))
        request.session.setdefault("browser_nonce", secrets.token_urlsafe(32))
        from .adapters import ADAPTERS

        accounts = db.scalars(select(Account)).all()
        state = []
        for account in accounts:
            a = ADAPTERS[account.platform](settings, db, None)
            state.append(
                {
                    "account": account,
                    "connected": a.connected(),
                    "dry": a.dry,
                    "oauth_ready": connections.ready(settings, account.platform),
                }
            )
        post = db.get(Post, post_id) if post_id else None
        posts = db.scalars(
            select(Post).where(Post.status != "deleted").order_by(Post.created_at.desc()).limit(200)
        ).all()
        if view == "queues":
            posts = [p for p in posts if p.status in {"held", "awaiting_manual", "failed"}]
        from .analytics import report

        return templates.TemplateResponse(
            request=request,
            name="studio.html",
            status_code=status_code,
            context={
                "view": view,
                "error_message": error_message,
                "asset_form": asset_form or {},
                "media_base_url": (settings.media_base_url or "/media").rstrip("/"),
                "csrf": request.session["csrf"],
                "posts": posts,
                "post": post,
                "connections": state,
                "control": db.get(Control, 1),
                "platforms": PLATFORMS,
                "assets": db.scalars(select(Asset).order_by(Asset.created_at.desc())).all(),
                "facts": db.scalars(select(FactSheet).order_by(FactSheet.created_at.desc())).all(),
                "alerts": db.scalars(select(Alert).order_by(Alert.created_at.desc()).limit(15)).all(),
                "checks": db.scalars(
                    select(CheckResult).where(CheckResult.post_id == post_id).order_by(CheckResult.id.desc())
                ).all()
                if post
                else [],
                "history": db.scalars(
                    select(Transition).where(Transition.post_id == post_id).order_by(Transition.id)
                ).all()
                if post
                else [],
                "requests": db.scalars(select(RequestRecord).where(RequestRecord.post_id == post_id)).all()
                if post
                else [],
                "settings": settings,
                "json": json,
                "yaml": yaml,
                "analytics": report(db, settings) if view == "analytics" else None,
                "media_jobs": db.scalars(
                    select(MediaJob).order_by(MediaJob.created_at.desc()).limit(100)
                ).all()
                if view == "media-studio"
                else [],
                "media_campaigns": db.scalars(
                    select(MediaCampaign).order_by(MediaCampaign.created_at.desc())
                ).all()
                if view == "media-studio"
                else [],
            },
        )

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, db: DB):
        return page(request, db, "dashboard")

    @app.get("/admin/{view}", response_class=HTMLResponse)
    def admin(view: str, request: Request, db: DB):
        if view not in {"connections", "queues", "assets", "facts", "compose", "analytics", "media-studio"}:
            raise HTTPException(404)
        return page(request, db, view)

    @app.get("/posts/{post_id}", response_class=HTMLResponse)
    def detail(post_id: str, request: Request, db: DB):
        post = db.get(Post, post_id)
        if not post or post.status == "deleted":
            raise HTTPException(404)
        return page(request, db, "post", post_id)

    @app.get("/api/session")
    def session(request: Request):
        request.session.setdefault("csrf", secrets.token_urlsafe(32))
        return {"csrf": request.session["csrf"]}

    @app.get("/api/posts")
    def list_posts(db: DB):
        return [
            {
                "id": p.id,
                "platform": p.platform,
                "status": p.status,
                "caption": p.caption,
                "scheduled_at": p.scheduled_at,
                "url": p.live_url,
                "dry_run": p.dry_run,
            }
            for p in db.scalars(
                select(Post).where(Post.status != "deleted").order_by(Post.created_at.desc()).limit(500)
            )
        ]

    @app.post("/api/media-jobs", dependencies=mutation)
    def create_media_job(payload: CreativeBrief, db: DB):
        jobs = queue_jobs(db, settings, payload)
        db.commit()
        return {"jobs": [{"id": j.id, "platform": j.platform, "state": j.state} for j in jobs]}

    @app.get("/api/media-jobs")
    def list_media_jobs(db: DB):
        return [
            {
                "id": j.id,
                "platform": j.platform,
                "state": j.state,
                "stage": j.stage,
                "task_id": j.task_id,
                "error": j.error,
                "asset_id": j.output_asset_id,
                "post_id": j.post_id,
            }
            for j in db.scalars(select(MediaJob).order_by(MediaJob.created_at.desc()).limit(100))
        ]

    @app.post("/forms/media-studio", dependencies=mutation)
    async def submit_creative(request: Request, db: DB):
        form = await request.form()
        try:
            brief = CreativeBrief(
                campaign=form.get("campaign", ""),
                prompt=form.get("prompt", ""),
                operation=form.get("operation", "image"),
                platforms=form.getlist("platforms"),
                facts_id=form.get("facts_id", ""),
                product=form.get("product", "company"),
                kind=form.get("kind", "promotional"),
                rights=form.get("rights", ""),
                reference_ids=form.getlist("reference_ids"),
                audio_reference_ids=form.getlist("audio_reference_ids"),
                audio_prompt=form.get("audio_prompt", ""),
                duration=form.get("duration", "5"),
                website=form.get("website") == "on",
                create_posts=form.get("create_posts") == "on",
                interval_hours=form.get("interval_hours", "24"),
            )
            if form.get("mode") == "recurring":
                validate_sources(db, brief)
                if not brief.create_posts or brief.operation == "audio":
                    raise ValueError("Recurring campaigns must create image/video posts")
                db.add(MediaCampaign(name=brief.campaign, brief=brief.model_dump(), enabled=False))
            else:
                await run_in_threadpool(queue_jobs, db, settings, brief)
            db.commit()
        except ValueError as exc:
            db.rollback()
            return page(
                request,
                db,
                "media-studio",
                error_message=str(exc),
                asset_form={
                    k: form.getlist(k)
                    if k in {"platforms", "reference_ids", "audio_reference_ids"}
                    else str(v)
                    for k, v in form.items()
                }
                | {
                    "website": str(form.get("website", "")),
                    "create_posts": str(form.get("create_posts", "")),
                    "platforms": form.getlist("platforms"),
                },
                status_code=400,
            )
        return RedirectResponse("/admin/media-studio", status_code=303)

    @app.post("/forms/media-campaigns/{campaign_id}", dependencies=mutation)
    async def toggle_campaign(campaign_id: str, request: Request, db: DB):
        campaign = db.get(MediaCampaign, campaign_id)
        if not campaign:
            raise HTTPException(404)
        campaign.enabled = (await request.form()).get("enabled") == "true"
        campaign.error = ""
        db.commit()
        return RedirectResponse("/admin/media-studio", status_code=303)

    @app.post("/forms/media-jobs/{job_id}/{action}", dependencies=mutation)
    async def media_job_action(job_id: str, action: str, request: Request, db: DB):
        job = db.scalar(select(MediaJob).where(MediaJob.id == job_id).with_for_update())
        if not job:
            raise HTTPException(404)
        if action == "resume":
            resume_job(job)
        elif action == "reconcile" and job.state == "uncertain" and not job.task_id:
            import re

            from .db import now

            task_id = str((await request.form()).get("task_id", ""))
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id):
                raise ValueError("Enter the task ID returned by the original Runway submission")
            job.task_id, job.state, job.submitted_at, job.error = task_id, "running", now(), ""
        elif action == "cancel" and job.state in {"queued", "failed", "uncertain"}:
            # Conditional write prevents a late UI cancellation racing a provider submission.
            changed = db.execute(
                update(MediaJob)
                .where(MediaJob.id == job.id, MediaJob.state == job.state)
                .values(state="cancelled")
            )
            if not changed.rowcount:
                raise PostConflict("This media job has changed. Refresh before cancelling.")
        else:
            raise ValueError("This job cannot perform that action")
        db.commit()
        return RedirectResponse("/admin/media-studio", status_code=303)

    @app.post("/api/posts", dependencies=mutation)
    def new_post(payload: DraftInput, db: DB):
        post = draft(db, **payload.model_dump())
        return {"id": post.id, "status": post.status}

    def action_impl(db, post, action, actor, parts=None):
        if action in {"check", "approve", "reject", "edit", "tiktok_status"}:
            lock_post(db, post)
        if action == "generate":
            generate(db, settings, post)
        elif action == "delete":
            delete_draft(db, post, actor)
        elif action in {"check", "approve"}:
            if post.status == "held":
                transition(db, post, "generated", "Requested automated recheck", actor)
            run_gate(db, settings, post)
        elif action == "schedule":
            schedule(db, settings, post)
        elif action == "reject":
            transition(db, post, "failed", "Rejected by internal user", actor)
            db.commit()
        elif action == "edit":
            if post.status not in {"held", "draft"}:
                raise ValueError("Only held/draft posts are editable")
            if post.status == "held":
                transition(db, post, "draft", "Edited; approval invalidated", actor)
            facts = Facts.model_validate(db.get(FactSheet, post.facts_id).document)
            post.parts = parts
            post.fact_ids = caption_fact_ids(parts)
            post.ai_disclosure = any(db.get(Asset, id).source != "company" for id in post.asset_ids)
            post.caption = render(parts, profiles(settings)[post.platform], facts, post.platform, settings)
            post.approved_digest = ""
            transition(db, post, "generated", "Edited caption and fact citations", actor)
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
    def post_action(post_id: str, action: str, request: Request, db: DB, parts: list[dict] | None = None):
        post = db.get(Post, post_id)
        if not post or post.status == "deleted":
            raise HTTPException(404)
        return action_impl(db, post, action, request.state.actor, parts)

    @app.post("/forms/posts", dependencies=mutation)
    async def compose(request: Request, db: DB):
        form = await request.form()
        post = await run_in_threadpool(
            draft,
            db,
            form["platform"],
            form["campaign"],
            form.getlist("asset_ids"),
            form["facts_id"],
            form["product"],
            form["kind"],
        )
        return RedirectResponse(f"/posts/{post.id}", status_code=303)

    @app.post("/forms/posts/{post_id}/{action}", dependencies=mutation)
    async def form_action(post_id: str, action: str, request: Request, db: DB):
        post = db.get(Post, post_id)
        if not post or post.status == "deleted":
            raise HTTPException(404)
        form = await request.form()
        parts = json.loads(form["parts"]) if "parts" in form else None
        try:
            await run_in_threadpool(action_impl, db, post, action, request.state.actor, parts)
        except PostConflict as exc:
            db.rollback()
            if db.get(Post, post_id).status == "deleted":
                return RedirectResponse("/", status_code=303)
            return page(request, db, "post", post_id, error_message=str(exc), status_code=409)
        return RedirectResponse("/" if post.status == "deleted" else f"/posts/{post.id}", status_code=303)

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
        return RedirectResponse("/", status_code=303)

    @app.post("/forms/facts", dependencies=mutation)
    async def facts_editor(request: Request, db: DB):
        form = await request.form()
        save_facts(db, yaml.safe_load(form["document"]))
        db.commit()
        return RedirectResponse("/admin/facts", status_code=303)

    @app.post("/forms/assets", dependencies=mutation)
    async def upload(request: Request, db: DB):
        form = await request.form()
        upload = form["file"]
        data = await upload.read(settings.max_upload_bytes + 1)
        from datetime import datetime

        expiry = datetime.fromisoformat(form["rights_expire_at"]) if form.get("rights_expire_at") else None
        await run_in_threadpool(
            ingest,
            db,
            settings,
            data,
            upload.filename,
            source=form["source"],
            generator=form.get("generator") or None,
            prompt=form.get("prompt") or None,
            tags=json.loads(form.get("tags") or "{}"),
            rights=form["rights"],
            rights_expire_at=expiry,
            customer_permission=form.get("customer_permission") == "on",
            permission_reference=form.get("permission_reference", ""),
        )
        db.commit()
        return RedirectResponse("/admin/assets", status_code=303)

    @app.post("/forms/import-bucket-asset", dependencies=mutation)
    async def import_bucket_asset(request: Request, db: DB):
        from datetime import datetime

        from .storage import storage

        if settings.storage_backend != "s3":
            raise ValueError("Bucket import requires STORAGE_BACKEND=s3")
        form = await request.form()
        key = str(form.get("key", "")).strip()
        if not key or len(key) > 1024:
            raise ValueError("Enter the existing bucket object key")
        tags = json.loads(form.get("tags") or "{}")
        if not isinstance(tags, dict) or not str(form.get("rights", "")).strip():
            raise ValueError("Usage rights and a JSON object for tags are required")
        store = storage(settings)

        def import_existing():
            try:
                if not 0 < store.head(key)["ContentLength"] <= settings.max_upload_bytes:
                    raise ValueError("Bucket object is empty or exceeds MAX_UPLOAD_BYTES")
                ingest(
                    db,
                    settings,
                    store.read(key),
                    key,
                    source=str(form.get("source", "company")),
                    generator=str(form.get("generator", "")) or None,
                    prompt=str(form.get("prompt", "")) or None,
                    rights=str(form.get("rights", "")),
                    tags=tags | {"imported_bucket_key": key},
                    rights_expire_at=datetime.fromisoformat(form["rights_expire_at"])
                    if form.get("rights_expire_at")
                    else None,
                    customer_permission=form.get("customer_permission") == "on",
                    permission_reference=str(form.get("permission_reference", "")),
                )
                db.commit()
            finally:
                store.client.close()

        await run_in_threadpool(import_existing)
        return RedirectResponse("/admin/assets", status_code=303)

    @app.post("/forms/generate-asset", dependencies=mutation)
    async def generate_asset(request: Request, db: DB):
        form = await request.form()
        values = {key: str(form.get(key, "")) for key in ("prompt", "kind", "rights", "tags")}
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
                values["rights"],
                tags,
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
                "assets",
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
                "platform_name": {"x": "X", "linkedin": "LinkedIn", "tiktok": "TikTok"}[platform],
            },
        )

    @app.get("/oauth/{platform}/callback")
    def oauth_callback(
        platform: str, request: Request, db: DB, state: str = "", code: str = "", error: str = ""
    ):
        if error or not code:
            raise HTTPException(400, "Authorization was declined or incomplete")
        connections.callback(db, settings, platform, state, code, request.session.get("browser_nonce", ""))
        return RedirectResponse("/admin/connections", status_code=303)

    @app.post("/forms/meta", dependencies=mutation)
    async def meta(request: Request, db: DB):
        form = await request.form()
        await run_in_threadpool(
            connections.save_meta, db, settings, form["token"], form["page_id"], form["instagram_id"]
        )
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
