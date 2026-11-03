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
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from . import connections
from .config import PLATFORMS, get_settings, profiles
from .db import make_engine, sessions
from .facts import Facts, load_facts, render, save_facts
from .gate import run_gate
from .lifecycle import transition
from .models import Account, Alert, Asset, CheckResult, Control, FactSheet, Post, RequestRecord, Transition
from .monitoring import set_kill, setup_logging
from .pipeline import bootstrap, draft, generate
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
            "default-src 'self'; style-src 'self'; img-src 'self' data: https:; form-action 'self'; frame-ancestors 'none'"
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

    @app.get("/health")
    def health():
        return {"status": "ok"}

    def page(request, db, view, post_id=None):
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
        posts = db.scalars(select(Post).order_by(Post.created_at.desc()).limit(200)).all()
        if view == "queues":
            posts = [p for p in posts if p.status in {"held", "awaiting_manual", "failed"}]
        from .analytics import report

        return templates.TemplateResponse(
            request=request,
            name="studio.html",
            context={
                "view": view,
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
            },
        )

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, db: DB):
        return page(request, db, "dashboard")

    @app.get("/admin/{view}", response_class=HTMLResponse)
    def admin(view: str, request: Request, db: DB):
        if view not in {"connections", "queues", "assets", "facts", "compose", "analytics"}:
            raise HTTPException(404)
        return page(request, db, view)

    @app.get("/posts/{post_id}", response_class=HTMLResponse)
    def detail(post_id: str, request: Request, db: DB):
        if not db.get(Post, post_id):
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
            for p in db.scalars(select(Post).order_by(Post.created_at.desc()).limit(500))
        ]

    @app.post("/api/posts", dependencies=mutation)
    def new_post(payload: DraftInput, db: DB):
        post = draft(db, **payload.model_dump())
        return {"id": post.id, "status": post.status}

    def action_impl(db, post, action, actor, parts=None):
        if action == "generate":
            generate(db, settings, post)
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
            post.fact_ids = list(dict.fromkeys(p["fact_id"] for p in parts))
            post.ai_disclosure = any(db.get(Asset, id).source != "company" for id in post.asset_ids)
            post.caption = render(
                parts, profiles(settings)[post.platform], facts, post.platform, post.ai_disclosure, settings
            )
            post.approved_digest = ""
            transition(db, post, "generated", "Edited approved fact selections", actor)
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
        if not post:
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
        if not post:
            raise HTTPException(404)
        form = await request.form()
        parts = json.loads(form["parts"]) if "parts" in form else None
        await run_in_threadpool(action_impl, db, post, action, request.state.actor, parts)
        return RedirectResponse(f"/posts/{post.id}", status_code=303)

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

    @app.post("/forms/generate-asset", dependencies=mutation)
    async def generate_asset(request: Request, db: DB):
        form = await request.form()
        await run_in_threadpool(
            Providers(settings).generate_asset,
            db,
            form["prompt"],
            form["kind"],
            form["rights"],
            json.loads(form.get("tags") or "{}"),
        )
        db.commit()
        return RedirectResponse("/admin/assets", status_code=303)

    @app.post("/oauth/{platform}/connect", dependencies=mutation)
    def connect(platform: str, request: Request, db: DB):
        request.session.setdefault("browser_nonce", secrets.token_urlsafe(32))
        url = connections.begin(db, settings, platform, request.session["browser_nonce"])
        return RedirectResponse(url, status_code=303)

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
