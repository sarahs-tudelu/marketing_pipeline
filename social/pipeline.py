from datetime import timedelta

from sqlalchemy import select, update

from .config import PLATFORMS, profiles
from .db import now
from .facts import Facts, active, content_digest, render
from .gate import evaluate
from .lifecycle import PostConflict, lock_post, transition
from .models import Account, Asset, Control, FactSheet, Post, RequestRecord, Transition
from .monitoring import alert, event
from .provider_errors import ProviderError
from .providers import Providers


def bootstrap(db):
    if not db.get(Control, 1):
        db.add(Control(id=1))
    for platform in PLATFORMS:
        if not db.get(Account, platform):
            db.add(Account(platform=platform))
    db.commit()


def draft(db, platform, campaign, asset_ids, facts_id, product="company", kind="promotional"):
    if platform not in PLATFORMS or kind not in {"promotional", "non_promotional"}:
        raise ValueError("Invalid platform/content kind")
    if not asset_ids or any(not db.get(Asset, id) for id in asset_ids) or not db.get(FactSheet, facts_id):
        raise ValueError("Select existing assets and facts version")
    post = Post(
        platform=platform,
        campaign=campaign,
        asset_ids=asset_ids,
        facts_id=facts_id,
        product=product,
        kind=kind,
    )
    db.add(post)
    db.flush()
    db.add(
        Transition(post_id=post.id, from_status="", to_status="draft", reason="Draft created", actor="admin")
    )
    db.commit()
    return post


def generate(db, settings, post, providers=None):
    lock_post(db, post)
    if post.status != "draft":
        db.rollback()
        raise PostConflict(
            "This post has already moved beyond draft. Refresh its page to see the caption and status."
        )
    providers = providers or Providers(settings)
    facts = Facts.model_validate(db.get(FactSheet, post.facts_id).document)
    assets = [db.get(Asset, id) for id in post.asset_ids]
    post.ai_disclosure = any(a.source != "company" for a in assets)
    profile = profiles(settings)[post.platform]
    context = {
        "platform": post.platform,
        "profile": profile.model_dump(),
        "campaign": post.campaign,
        "assets": [{"id": a.id, "source": a.source, "tags": a.tags} for a in assets],
        "facts": [
            f.model_dump(mode="json")
            for f in facts.facts
            if active(f, now()) and f.product in {post.product, "company"}
        ],
        "brand_voice": facts.brand_voice,
        "disclaimers": facts.required_disclaimers,
        "ai_disclosure": post.ai_disclosure,
    }
    try:
        parts = providers.caption(context)
        caption = render(parts, profile, facts, post.platform, settings)
        post.parts, post.caption = parts, caption
        post.fact_ids = list(dict.fromkeys(p["fact_id"] for p in parts))
        transition(
            db,
            post,
            "generated",
            f"Caption provider: {'fixture' if settings.ai_mode == 'fixture' else settings.anthropic_model}",
        )
    except PostConflict:
        raise
    except Exception as exc:
        if isinstance(exc, ProviderError):
            event(
                "caption_generation_failed",
                post_id=post.id,
                provider=exc.provider,
                category=exc.category,
                upstream_status=exc.upstream_status,
                request_id=exc.request_id,
            )
        reason = str(exc) if isinstance(exc, ProviderError) else type(exc).__name__
        transition(db, post, "held", f"Caption generation rejected: {reason}")
    db.commit()
    return post


def publish_due(db, settings, at=None, client=None):
    from .adapters import adapter
    from .adapters.base import Deferred, PublishError
    from .connections import refresh_account
    from .scheduler import COUNTED, permitted

    at = at or now()
    candidates = db.scalars(
        select(Post)
        .where(Post.status.in_(["scheduled", "publishing"]), Post.scheduled_at <= at)
        .order_by(Post.scheduled_at)
    ).all()
    for candidate in candidates:
        if candidate.next_attempt_at and candidate.next_attempt_at > at:
            continue
        db.execute(update(Control).where(Control.id == 1).values(revision=Control.revision + 1))
        db.refresh(candidate)
        post = candidate
        account = db.get(Account, post.platform)
        if db.get(Control, 1).killed or account.paused:
            db.rollback()
            continue
        already_delivered = db.scalar(
            select(RequestRecord.id).where(
                RequestRecord.post_id == post.id,
                RequestRecord.irreversible.is_(True),
                RequestRecord.state == "done",
            )
        )
        if not already_delivered:
            others = db.scalars(select(Post).where(Post.id != post.id, Post.status.in_(COUNTED))).all()
            if not permitted(
                at, post, others, profiles(settings)[post.platform], settings, respect_target=False
            ):
                db.rollback()
                continue
        if post.status == "scheduled":
            transition(db, post, "publishing", "Worker claimed reservation")
        db.commit()
        try:
            selected = adapter(settings, db, post, client)
            connection_missing = selected.dry and not post.dry_run
            # A dry-run post remains a dry-run even after accounts are connected.
            selected.dry = post.dry_run
            result = getattr(selected, "recover_delivery", lambda: None)()
            if not result and connection_missing:
                raise PublishError("auth", "Live post lost required connection/configuration")
            if not result and not selected.dry:
                if post.account_id != account.external_id:
                    raise PublishError("validation", "Account changed since scheduling; create a new post")
                if account.expires_at and account.expires_at <= now() + timedelta(minutes=5):
                    if not refresh_account(db, settings, post.platform, client):
                        raise PublishError("auth", "Could not refresh credentials")
                    selected = adapter(settings, db, post, client)
            if not result:
                checks, assets = evaluate(db, settings, post, at=at, live=not selected.dry)
                failed = [c.name for c in checks if not c.passed]
                if content_digest(post, assets) != post.approved_digest:
                    failed.append("approval_digest_changed")
                if failed:
                    transition(db, post, "held", "Pre-publish checks: " + ", ".join(failed))
                    db.commit()
                    continue
                result = selected.publish(post, post.idempotency_key)
            post.remote_id, post.live_url = result.platform_post_id, result.url
            post.published_at = at  # For TikTok this is inbox delivery time, not public publication.
            post.next_attempt_at, post.error = None, ""
            account.failure_count = 0
            transition(
                db,
                post,
                result.status,
                "Simulated delivery" if selected.dry else "Platform delivery acknowledged",
            )
            if result.status == "awaiting_manual":
                alert(
                    db,
                    "tiktok_manual",
                    f"{settings.tiktok_manual_assignee}: {'SIMULATED inbox' if selected.dry else 'Open TikTok inbox'}; finish post {post.id}. "
                    f"Turn AI-generated label {'ON' if post.ai_disclosure else 'on only if further AI editing is added'}. Caption to paste:\n{post.caption}",
                    "manual:" + post.id,
                )
            db.commit()
            event(
                "post_delivered",
                post_id=post.id,
                platform=post.platform,
                dry_run=selected.dry,
                status=post.status,
            )
        except Deferred as exc:
            post.next_attempt_at = at + timedelta(seconds=exc.retry_after)
            if (at - post.scheduled_at).total_seconds() > 1800:
                account.failure_count += 1
                if account.failure_count >= settings.failure_pause_threshold:
                    account.paused = True
                transition(db, post, "failed", "Remote processing exceeded 30 minutes; inspect platform")
                alert(
                    db,
                    "publish_failure",
                    f"{post.platform}: media processing timed out for {post.id}",
                    "processing:" + post.id,
                )
            db.commit()
        except PublishError as exc:
            post.attempts += 1
            post.error = f"{exc.kind}: {exc}"
            if exc.kind == "paused":
                db.commit()
                continue
            account.failure_count += 1
            if exc.kind == "auth":
                refresh_account(db, settings, post.platform, client, force=True)
            if exc.kind == "policy" or account.failure_count >= settings.failure_pause_threshold:
                account.paused = True
            retry = (
                exc.kind in {"transient", "auth"}
                and post.attempts < settings.retry_max_attempts
                and not account.paused
            )
            if retry:
                delay = max(exc.retry_after, settings.retry_base_seconds * 2 ** (post.attempts - 1))
                post.next_attempt_at = at + timedelta(seconds=delay)
            else:
                transition(db, post, "failed", post.error)
            alert(
                db,
                "policy_error" if exc.kind == "policy" else "publish_failure",
                f"{post.platform} post {post.id}: {post.error}",
                f"publish:{post.id}:{post.attempts}",
            )
            db.commit()
        except Exception as exc:
            # Unknown errors must never trigger a blind retry of a publishing operation.
            db.rollback()
            post = db.get(Post, candidate.id)
            if post.status == "publishing":
                account = db.get(Account, post.platform)
                account.failure_count += 1
                if account.failure_count >= settings.failure_pause_threshold:
                    account.paused = True
                post.error = (
                    "Unexpected failure; inspect request audit before reconciliation: " + type(exc).__name__
                )
                transition(db, post, "failed", post.error)
                alert(
                    db,
                    "publish_failure",
                    f"{post.platform}: {post.id}: {post.error}",
                    "unexpected:" + post.id,
                )
                db.commit()
