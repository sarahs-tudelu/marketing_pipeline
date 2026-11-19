"""What the app's AI requests cost, and how posts perform for that spend (Settings → Usage).

Two sources, each doing what the other can't:

- **Billed costs** come from each provider's cost API, by day and model (ProviderCost): Runway's account
  usage with the existing API key, and OpenAI and Anthropic with an organization admin key. They are the
  totals the dashboard leads with.
- **The app's own records** (UsageRecord) say what each request was for and which media job, post and
  platform it served: Runway tasks with the credits Runway reports charging, and Claude and OpenAI
  requests with their token counts priced from resources/pricing.yaml. Providers' reports can't be split
  by post or platform, so these records do that, and stand in for a provider's total until it's connected.

Requests are recorded in memory and written only where the caller has just committed (flush), so recording
never changes when or whether the pipeline's own work is saved, and a failure to record never breaks it.
"""

import functools
import threading
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
from importlib.resources import files

import httpx
import yaml
from sqlalchemy import delete, select

from .db import now
from .models import MediaJob, Post, ProviderCost, ProviderSync, UsageRecord
from .provider_errors import ProviderError

PROVIDERS = ("runway", "openai", "anthropic")
PROVIDER_LABELS = {"runway": "Runway", "openai": "OpenAI", "anthropic": "Anthropic (Claude)"}
PERIODS = (7, 30, 90)
SYNC_DAYS = 95  # Enough for the 90-day view.
ANALYTICS_BETA = "analytics-usage-cost-2026-08-24"
STAGES = ("Media generation", "Planning and editing", "Captions", "Checks")
# What each request was for, with the stage of making a post it belongs to.
PURPOSES = {
    "scene_image": ("Scene images", "Media generation"),
    "video_clip": ("Video clips", "Media generation"),
    "image": ("Images", "Media generation"),
    "video_rerender": ("Video re-renders", "Media generation"),
    "music": ("Music", "Media generation"),
    "sound": ("Audio", "Media generation"),
    "idea_plan": ("Idea planning", "Planning and editing"),
    "prompt_writer": ("Prompt writing", "Planning and editing"),
    "product_reference": ("Reference selection", "Planning and editing"),
    "scene_review": ("Scene review", "Planning and editing"),
    "real_media_edit": ("Real media editing", "Planning and editing"),
    "media_search": ("Library search", "Planning and editing"),
    "media_changes": ("Media change requests", "Planning and editing"),
    "visual_summary": ("Media descriptions", "Planning and editing"),
    "caption": ("Captions", "Captions"),
    "caption_review": ("Caption fact check", "Checks"),
    "vision_review": ("Visual check", "Checks"),
    "audio_listening": ("Audio listening", "Checks"),
    "audio_review": ("Audio claims check", "Checks"),
    "embedding": ("Duplicate check", "Checks"),
}
NOT_POSTED = {"held", "failed", "deleted"}
MAX_PENDING = 5000

_scope = ContextVar("usage_scope", default={})
_pending = []
_lock = threading.Lock()


# Prices -----------------------------------------------------------------------------------------------


@functools.cache
def prices():
    return yaml.safe_load(files("social").joinpath("resources", "pricing.yaml").read_text(encoding="utf-8"))


def model_prices(provider, model):
    """The price row for a model: an exact match, or the longest name it starts with (dated snapshots)."""
    table = prices().get(provider) or {}
    if model in table:
        return table[model]
    matches = [name for name in table if model.startswith(name)]
    return table[max(matches, key=len)] if matches else None


def credit_usd():
    return float((prices().get("runway") or {}).get("credit_usd", 0.01))


def token_units(provider, usage):
    """Token counts by kind, from a Messages, Responses, Chat Completions or Embeddings usage object."""
    u = usage or {}
    if provider == "anthropic":
        units = {
            "input": u.get("input_tokens"),
            "cached_input": u.get("cache_read_input_tokens"),
            "cache_write": u.get("cache_creation_input_tokens"),
            "output": u.get("output_tokens"),
        }
    elif "input_tokens" in u:
        cached = (u.get("input_tokens_details") or {}).get("cached_tokens") or 0
        units = {"input": (u.get("input_tokens") or 0) - cached, "cached_input": cached, "output": u.get("output_tokens")}
    else:
        details = u.get("prompt_tokens_details") or {}
        cached, audio_in = details.get("cached_tokens") or 0, details.get("audio_tokens") or 0
        completion = u.get("completion_tokens") or 0
        audio_out = (u.get("completion_tokens_details") or {}).get("audio_tokens") or 0
        units = {
            "input": (u.get("prompt_tokens") or 0) - cached - audio_in,
            "cached_input": cached,
            "audio_input": audio_in,
            "output": completion - audio_out,
            "audio_output": audio_out,
        }
    return {k: int(v) for k, v in units.items() if isinstance(v, int | float) and v > 0}


_FALLBACK = {"cached_input": "input", "cache_write": "input", "audio_input": "input", "audio_output": "output"}


def token_cost(provider, model, units):
    """Estimated USD for the token counts, or None when the price table doesn't cover the model."""
    rates = model_prices(provider, model)
    if not rates:
        return None
    total = 0.0
    for kind, count in units.items():
        rate = rates.get(kind, rates.get(_FALLBACK.get(kind, "")))
        if rate is None:
            return None
        total += count * float(rate) / 1_000_000
    return round(total, 6)


# Recording --------------------------------------------------------------------------------------------


def attribute(**attrs):
    """Attribute the current worker step's requests to a media job, post and platform (None clears)."""
    _scope.set({k: v for k, v in attrs.items() if v})


@contextmanager
def attributed(**attrs):
    token = _scope.set(_scope.get() | {k: v for k, v in attrs.items() if v})
    try:
        yield
    finally:
        _scope.reset(token)


def labelled(purpose):
    """Label the Claude requests a function makes (OpenAI requests are labelled from their payload)."""

    def decorate(function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            token = _scope.set(_scope.get() | {"purpose": purpose})
            try:
                return function(*args, **kwargs)
            finally:
                _scope.reset(token)

        return wrapper

    return decorate


def current_purpose(default):
    return _scope.get().get("purpose") or default


def for_post(function):
    """Attribute a post step's requests (caption writing, checks) to the post, then save them."""

    @functools.wraps(function)
    def wrapper(db, settings, post, *args, **kwargs):
        with attributed(post_id=post.id, platform=post.platform):
            result = function(db, settings, post, *args, **kwargs)
        flush(db)  # The step has committed its own work by now.
        return result

    return wrapper


def _add(row):
    scope = _scope.get()
    row = {
        "post_id": scope.get("post_id"),
        "media_job_id": scope.get("media_job_id"),
        "platform": scope.get("platform") or "",
        "request_id": "",
        "created_at": now(),
    } | row
    with _lock:
        _pending.append(row)
        del _pending[:-MAX_PENDING]


def record_tokens(provider, purpose, model, usage):
    """Record a successful Claude or OpenAI request from its usage object."""
    try:
        if not isinstance(usage, dict):
            return
        units = token_units(provider, usage)
        _add({
            "provider": provider,
            "purpose": purpose,
            "model": str(model or "")[:120],
            "units": units,
            "cost_usd": token_cost(provider, str(model or ""), units),
            "estimated": True,
        })
    except Exception:  # noqa: BLE001 - Recording must never break a request that succeeded.
        pass


def openai_purpose(endpoint, payload):
    if endpoint == "embeddings":
        return "embedding"
    if endpoint.startswith("chat"):
        return "audio_listening"
    if endpoint.startswith("images"):
        return "image"
    return ((((payload or {}).get("text") or {}).get("format") or {}).get("name")) or "openai"


def runway_purpose(brief, stage):
    if stage == "scene":
        return "scene_image"
    if stage == "audio" or brief.get("operation") == "audio":
        return "music" if stage == "audio" and brief.get("soundtrack_model") == "music" else "sound"
    return {"image": "image", "video_edit": "video_rerender"}.get(brief.get("operation"), "video_clip")


def record_runway_task(job, task):
    """Record a finished Runway task with the credits Runway reports charging (or its estimate)."""
    try:
        credits = (task.get("cost") or {}).get("credits")
        estimated = credits is None
        if estimated:
            credits = job.brief.get("active_estimate")
        if credits is None:
            return
        stage = "scene" if job.brief.get("active_generation") == "scene" else job.stage
        _add({
            "provider": "runway",
            "purpose": runway_purpose(job.brief, stage),
            "model": str(job.brief.get("active_model", ""))[:120],
            "units": {"credits": credits},
            "cost_usd": round(float(credits) * credit_usd(), 6),
            "estimated": estimated,
            "request_id": job.task_id,
            "media_job_id": job.id,
            "platform": job.platform,
            "post_id": job.post_id,
        })
    except Exception:  # noqa: BLE001
        pass


def flush(db):
    """Write recorded requests. Call only where the caller has just committed its own work."""
    with _lock:
        rows = list(_pending)
        _pending.clear()
    if not rows:
        return 0
    try:
        ids = {r["request_id"] for r in rows if r["request_id"]}
        known = set(db.scalars(select(UsageRecord.request_id).where(UsageRecord.request_id.in_(ids))).all()) if ids else set()
        added = 0
        for row in rows:
            if row["request_id"]:
                if row["request_id"] in known:
                    continue  # A resumed task is polled again; it was charged once.
                known.add(row["request_id"])
            db.add(UsageRecord(**row))
            added += 1
        db.commit()
        return added
    except Exception:  # noqa: BLE001 - Kept for the next flush rather than lost.
        db.rollback()
        with _lock:
            _pending[:0] = rows
            del _pending[:-MAX_PENDING]
        return 0


def reset():
    """Forget unsaved records and attribution (tests)."""
    with _lock:
        _pending.clear()
    _scope.set({})


# Billed costs from the providers ------------------------------------------------------------------------


def setup_needed(settings, provider):
    """What's missing before a provider's billed costs can be read, or "" when nothing is."""
    if settings.ai_mode != "live":
        return "Billed costs are read in live mode (AI_MODE=live) only."
    if provider == "runway" and not settings.runway_api_key.get_secret_value():
        return "Add RUNWAY_API_KEY, the key the app uses for Runway."
    if provider == "openai" and not settings.openai_admin_key.get_secret_value():
        return (
            "Add OPENAI_ADMIN_KEY: an Admin key (sk-admin-…) that an organization owner creates in the OpenAI "
            "platform under Settings → Organization → Admin keys. Add OPENAI_COST_PROJECT_ID (proj_…) to count "
            "only this app's project."
        )
    if provider == "anthropic" and not anthropic_cost_key(settings):
        return "Add ANTHROPIC_API_KEY: an organization admin's personal key that isn't scoped to a workspace."
    return ""


def anthropic_cost_key(settings):
    """The key that reads Claude costs: the optional admin key, else the app's own key.

    Anthropic's cost API accepts an Admin API key, or a personal or service-account key that isn't
    scoped to a workspace, such as a key created by the organization's owner.
    """
    return settings.anthropic_admin_key.get_secret_value() or settings.anthropic_api_key.get_secret_value()


def _get_json(client, provider, url, *, headers, params):
    try:
        response = client.get(url, headers=headers, params=params)
    except httpx.HTTPError:
        raise ProviderError(f"Could not reach {provider}'s cost API.", provider=provider, category="usage") from None
    if response.status_code in (401, 403):
        hint = (
            "The key needs organization admin access and mustn't be scoped to a workspace: use an organization "
            "owner's or admin's personal key, or set ANTHROPIC_ADMIN_KEY."
            if provider == "Anthropic"
            else "Check that OPENAI_ADMIN_KEY is an organization Admin key (sk-admin-…) and still active."
        )
        raise ProviderError(
            f"{provider} refused to share cost data with this key. {hint}",
            provider=provider, category="usage", status=response.status_code,
        )
    if response.is_error:
        raise ProviderError(
            f"{provider}'s cost API returned an error ({response.status_code}).",
            provider=provider, category="usage", status=response.status_code,
        )
    try:
        result = response.json()
    except ValueError:
        result = None
    if not isinstance(result, dict):
        raise ProviderError(f"{provider}'s cost API returned an unexpected response.", provider=provider, category="usage")
    return result


def runway_costs(settings, start, end):
    from .runway import Runway

    runway = Runway(settings)
    try:
        balance = runway.organization().get("creditBalance")
        rows, chunk = [], start
        while chunk < end:
            stop = min(end, chunk + timedelta(days=90))
            for day in runway.usage(chunk, stop).get("results", []):
                for used in day.get("usedCredits", []):
                    credits = used.get("amount") or 0
                    rows.append((date.fromisoformat(day["date"]), used.get("model") or "Other",
                                 credits * credit_usd(), {"credits": credits}))
            chunk = stop
    finally:
        runway.close()
    usd = balance * credit_usd() if isinstance(balance, int | float) else None
    return rows, {"credit_balance": balance, "credit_balance_usd": usd, "scope": "this Runway API project"}


def openai_costs(settings, start, end):
    epoch = lambda day: int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())  # noqa: E731
    params = {"start_time": epoch(start), "end_time": epoch(end), "bucket_width": "1d", "group_by": ["line_item"],
              "limit": 180}
    if settings.openai_cost_project_id:
        params["project_ids"] = [settings.openai_cost_project_id]
    headers = {"Authorization": "Bearer " + settings.openai_admin_key.get_secret_value()}
    rows, page = [], None
    with httpx.Client(timeout=settings.http_timeout_seconds) as client:
        for _ in range(20):
            result = _get_json(client, "OpenAI", "https://api.openai.com/v1/organization/costs", headers=headers,
                               params=params | ({"page": page} if page else {}))
            for bucket in result.get("data", []):
                day = datetime.fromtimestamp(bucket["start_time"], timezone.utc).date()
                for item in bucket.get("results", []):
                    amount = float((item.get("amount") or {}).get("value") or 0)
                    rows.append((day, item.get("line_item") or "Other", amount,
                                 {"quantity": item.get("quantity"), "unit": item.get("quantity_unit")}))
            page = result.get("next_page")
            if not result.get("has_more") or not page:
                break
    scope = f"project {settings.openai_cost_project_id}" if settings.openai_cost_project_id else "the whole organization"
    return rows, {"scope": scope}


def anthropic_costs(settings, start, end):
    headers = {
        "x-api-key": anthropic_cost_key(settings),
        "anthropic-version": settings.anthropic_api_version,
        "anthropic-beta": ANALYTICS_BETA,
    }
    rows, refreshed = [], None
    # The API takes ranges of at most 31 days, starting on or after 2026-01-01, ending no later than now.
    chunk, stop_at = max(start, date(2026, 1, 1)), now()
    with httpx.Client(timeout=settings.http_timeout_seconds) as client:
        while chunk < end:
            stop = min(end, chunk + timedelta(days=31))
            ending = min(datetime(stop.year, stop.month, stop.day), stop_at)
            params = {
                "starting_at": f"{chunk.isoformat()}T00:00:00Z",
                "ending_at": ending.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "time_bucket_width": "1d",
                "group_by[]": ["model"],
                "order_by": "time",
                "order": "asc",
                "limit": 1000,
            }
            if settings.anthropic_cost_api_key_id:
                params["api_key_ids[]"] = [settings.anthropic_cost_api_key_id]
            page = None
            for _ in range(20):
                result = _get_json(client, "Anthropic", "https://api.anthropic.com/v1/organizations/analytics/cost",
                                   headers=headers, params=params | ({"page": page} if page else {}))
                refreshed = result.get("data_refreshed_at") or refreshed
                for row in result.get("data", []):
                    cost = row.get("cost") or {}
                    # Amounts are decimal strings in cents.
                    rows.append((date.fromisoformat(row["starting_at"][:10]), (row.get("group") or {}).get("model") or "Other",
                                 float(cost.get("amount") or 0) / 100, {"requests": cost.get("requests")}))
                page = result.get("next_page")
                if not result.get("has_more") or not page:
                    break
            if ending >= stop_at:
                break
            chunk = stop
    scope = f"API key {settings.anthropic_cost_api_key_id}" if settings.anthropic_cost_api_key_id else "the whole organization"
    return rows, {"scope": scope, "data_refreshed_at": refreshed}


FETCH = {"runway": runway_costs, "openai": openai_costs, "anthropic": anthropic_costs}


def sync(db, settings, days=SYNC_DAYS, fetch=None):
    """Read each connected provider's billed daily costs into provider_costs. Returns a status per provider."""
    fetch = fetch or FETCH
    today = now().date()
    start, end = today - timedelta(days=days), today + timedelta(days=1)
    outcome = {}
    for provider in PROVIDERS:
        state = db.get(ProviderSync, provider) or ProviderSync(provider=provider, error="", details={})
        needs = setup_needed(settings, provider)
        if needs:
            state.error, state.details = "", {"needs": needs}
            db.merge(state)
            outcome[provider] = "not connected"
            continue
        attempted = {"attempted_at": now().isoformat()}
        try:
            rows, details = fetch[provider](settings, start, end)
        except Exception as exc:  # noqa: BLE001
            state.error = str(exc) if isinstance(exc, ProviderError) else f"Reading costs failed ({type(exc).__name__})."
            state.details = (state.details or {}) | attempted
            db.merge(state)
            outcome[provider] = "error"
            continue
        totals = defaultdict(lambda: [0.0, {}])
        for day, item, amount, units in rows:
            entry = totals[(day, str(item)[:200])]
            entry[0] += amount
            for key, value in units.items():
                if isinstance(value, int | float):
                    entry[1][key] = entry[1].get(key, 0) + value
        db.execute(delete(ProviderCost).where(ProviderCost.provider == provider, ProviderCost.day >= start,
                                              ProviderCost.day < end))
        for (day, item), (amount, units) in totals.items():
            db.add(ProviderCost(provider=provider, day=day, item=item, amount_usd=round(amount, 6), units=units))
        state.synced_at, state.error, state.details = now(), "", details | attempted
        db.merge(state)
        outcome[provider] = "synced"
    db.commit()
    return outcome


def backfill_runway(db, settings, limit=10):
    """Read the charged credits of Runway tasks the app ran before it recorded them, a few at a time."""
    if settings.ai_mode != "live" or not settings.runway_api_key.get_secret_value():
        return 0
    from .runway import Runway

    known = set(db.scalars(select(UsageRecord.request_id).where(UsageRecord.provider == "runway")).all())
    runway, checked = Runway(settings), 0
    try:
        for job in db.scalars(select(MediaJob).order_by(MediaJob.created_at.desc()).limit(300)).all():
            done = set(job.brief.get("usage_checked", []))
            tasks = [(h.get("task_id"), h.get("stage") or "visual", h.get("model", "")) for h in job.task_history]
            tasks += [(f.get("task_id"), f.get("stage") or "visual", "") for f in job.brief.get("failed_tasks", [])
                      if isinstance(f, dict)]
            unchecked = []
            for task_id, stage, model in tasks:
                if not task_id or task_id in known or task_id in done or task_id == job.task_id or checked >= limit:
                    continue
                checked += 1
                try:
                    task = runway.task(task_id)
                except (ProviderError, ValueError):
                    unchecked.append(task_id)
                    continue
                credits = (task.get("cost") or {}).get("credits")
                if credits is None:
                    unchecked.append(task_id)
                    continue
                try:
                    created = datetime.fromisoformat(task["createdAt"].replace("Z", "+00:00")).astimezone(timezone.utc)
                    created = created.replace(tzinfo=None)
                except (KeyError, ValueError, AttributeError):
                    created = job.created_at
                db.add(UsageRecord(
                    provider="runway", purpose=runway_purpose(job.brief, stage), model=str(model)[:120],
                    units={"credits": credits}, cost_usd=round(float(credits) * credit_usd(), 6), estimated=False,
                    request_id=task_id, post_id=job.post_id, media_job_id=job.id, platform=job.platform,
                    created_at=created,
                ))
                known.add(task_id)
            if unchecked:
                # Tasks Runway no longer returns, or that report no cost, aren't asked about again.
                job.brief = job.brief | {"usage_checked": [*done, *unchecked]}
            if checked >= limit:
                break
        db.commit()
    finally:
        runway.close()
    return checked


def tick(factory, settings):
    """Worker job: save recorded requests, read billed costs every USAGE_SYNC_HOURS, backfill old tasks."""
    with factory() as db:
        flush(db)
        if settings.ai_mode != "live":
            return
        due = now() - timedelta(hours=settings.usage_sync_hours)
        attempted = [(s.details or {}).get("attempted_at") for s in db.scalars(select(ProviderSync)).all()]
        if len(attempted) < len(PROVIDERS) or any(not a or datetime.fromisoformat(a) < due for a in attempted):
            sync(db, settings)
        backfill_runway(db, settings)


# The dashboard ----------------------------------------------------------------------------------------


def dashboard(db, settings, days=30):
    """Everything Settings → Usage shows for the last `days` days."""
    from .analytics import published_with_metrics, reach

    days = days if days in PERIODS else 30
    at = now()
    since, before = at - timedelta(days=days), at - timedelta(days=2 * days)
    records = db.scalars(select(UsageRecord).where(UsageRecord.created_at >= before)).all()
    current = [r for r in records if r.created_at >= since]
    billed_rows = db.scalars(select(ProviderCost).where(ProviderCost.day >= before.date())).all()
    syncs = {s.provider: s for s in db.scalars(select(ProviderSync)).all()}
    job_ids = {r.media_job_id for r in current if r.media_job_id}
    job_posts = {}
    if job_ids:
        links = select(MediaJob.id, MediaJob.post_id).where(MediaJob.id.in_(job_ids))
        job_posts = {job_id: post_id for job_id, post_id in db.execute(links)}

    def post_of(record):
        return record.post_id or job_posts.get(record.media_job_id)

    posts = {}
    live = []
    metrics = {}
    simulated = 0
    for post, latest in db.execute(published_with_metrics().where(Post.published_at >= since)):
        posts[post.id] = post
        if post.dry_run:
            simulated += 1
        else:
            live.append(post)
            if latest is not None:
                metrics[post.id] = latest
    other_ids = {post_of(r) for r in current} - posts.keys() - {None}
    if other_ids:
        posts.update((p.id, p) for p in db.scalars(select(Post).where(Post.id.in_(other_ids))))

    def platform_of(record):
        post = posts.get(post_of(record))
        return record.platform or (post.platform if post else "")

    # Spend: billed where a provider is connected, the app's records otherwise.
    connected = {p for p in PROVIDERS if syncs.get(p) and syncs[p].synced_at}

    def spend(start, end):
        """Spend from start (inclusive) to end (exclusive); billed days count from start's day."""
        total = 0.0
        for p in PROVIDERS:
            if p in connected:
                total += sum(c.amount_usd for c in billed_rows
                             if c.provider == p and start.date() <= c.day and (c.day < end.date() or end >= at))
            else:
                total += sum(r.cost_usd or 0 for r in records if r.provider == p and start <= r.created_at < end)
        return total

    total = spend(since, at + timedelta(seconds=1))
    previous = spend(before, since)
    providers = []
    for p in PROVIDERS:
        recorded = sum(r.cost_usd or 0 for r in current if r.provider == p)
        billed = sum(c.amount_usd for c in billed_rows if c.provider == p and c.day >= since.date()) if p in connected else None
        state = syncs.get(p)
        providers.append({
            "key": p,
            "label": PROVIDER_LABELS[p],
            "amount": billed if billed is not None else recorded,
            "source": "billed" if billed is not None else ("recorded" if p == "runway" else "estimated"),
            "billed": billed,
            "recorded": recorded,
            "requests": sum(1 for r in current if r.provider == p),
            "unpriced": sum(1 for r in current if r.provider == p and r.cost_usd is None),
            "synced_at": state.synced_at if state else None,
            "error": state.error if state else "",
            "details": (state.details or {}) if state else {},
            "needs": setup_needed(settings, p),
            "billed_items": sorted(
                ({"item": item, "amount": amount} for item, amount in _sum_by(
                    (c.item, c.amount_usd) for c in billed_rows if c.provider == p and c.day >= since.date())),
                key=lambda row: -row["amount"],
            ) if p in connected else [],
        })

    # The app's records, split by stage, step, platform and post.
    stage_totals, purposes, platforms = defaultdict(float), defaultdict(lambda: {"count": 0, "cost": 0.0, "units": defaultdict(int)}), defaultdict(float)
    for r in current:
        label, stage = PURPOSES.get(r.purpose, (r.purpose.replace("_", " ").capitalize(), "Other"))
        stage_totals[stage] += r.cost_usd or 0
        row = purposes[(stage, label, PROVIDER_LABELS.get(r.provider, r.provider), r.model)]
        row["count"] += 1
        row["cost"] += r.cost_usd or 0
        for key, value in (r.units or {}).items():
            if isinstance(value, int | float):
                row["units"][key] += value
        platforms[platform_of(r) or ""] += r.cost_usd or 0
    recorded_total = sum(r.cost_usd or 0 for r in current)

    # Performance of live posts published in the period.
    post_costs = defaultdict(float)
    post_lines = defaultdict(lambda: defaultdict(lambda: [0, 0.0]))
    for r in current:
        pid = post_of(r)
        if pid:
            post_costs[pid] += r.cost_usd or 0
            label = PURPOSES.get(r.purpose, (r.purpose, ""))[0]
            line = post_lines[pid][(label, r.model)]
            line[0] += 1
            line[1] += r.cost_usd or 0

    def performance(group):
        views = actions = measured = 0
        for post in group:
            row = metrics.get(post.id)
            if row and not row.simulated and not (row.metrics or {}).get("unavailable"):
                v, a = reach(row.metrics)
                views, actions, measured = views + v, actions + a, measured + 1
        cost = sum(post_costs.get(p.id, 0) for p in group)
        return {
            "posts": len(group), "measured": measured, "views": views, "interactions": actions,
            "engagement": actions / views if views else None, "cost": cost,
            "cost_per_post": cost / len(group) if group else None,
            "cost_per_1k_views": cost / views * 1000 if views else None,
        }

    by_platform = []
    for platform in sorted({p.platform for p in live} | {k for k in platforms if k}):
        row = performance([p for p in live if p.platform == platform])
        row["platform"] = platform
        row["spend"] = platforms.get(platform, 0.0)
        by_platform.append(row)
    overall = performance(live)
    kinds = [
        {"label": "Real project media", **performance([p for p in live if not p.ai_disclosure])},
        {"label": "AI inspiration", **performance([p for p in live if p.ai_disclosure])},
    ]

    post_rows = []
    for pid, cost in sorted(post_costs.items(), key=lambda item: -item[1]):
        post = posts.get(pid)
        if not post:
            continue
        row = metrics.get(pid)
        views, actions = reach(row.metrics) if row and not row.simulated else (0, 0)
        post_rows.append({
            "post": post, "cost": cost, "views": views,
            "engagement": actions / views if views else None,
            "lines": sorted(({"label": k[0], "model": k[1], "count": v[0], "cost": v[1]}
                             for k, v in post_lines[pid].items()), key=lambda line: -line["cost"]),
        })
    unposted = sum(row["cost"] for row in post_rows if row["post"].status in NOT_POSTED)
    no_post = sum(r.cost_usd or 0 for r in current if r.media_job_id and not post_of(r))
    unattributed = sum(r.cost_usd or 0 for r in current if not r.media_job_id and not post_of(r))

    # Daily spend, oldest first.
    daily = defaultdict(float)
    for c in billed_rows:
        if c.provider in connected and c.day >= since.date():
            daily[c.day] += c.amount_usd
    for r in current:
        if r.provider not in connected:
            daily[r.created_at.date()] += r.cost_usd or 0
    series = [{"day": since.date() + timedelta(days=i), "amount": daily.get(since.date() + timedelta(days=i), 0.0)}
              for i in range(days + 1)]
    peak = max((s["amount"] for s in series), default=0)

    def bars(pairs):
        pairs = [(label, amount) for label, amount in pairs if amount > 0]
        top = max((amount for _, amount in pairs), default=0)
        return [{"label": label, "amount": amount, "share": amount / top if top else 0} for label, amount in pairs]

    return {
        "days": days,
        "periods": PERIODS,
        "since": since,
        "total": total,
        "previous": previous,
        "change": (total - previous) / previous if previous else None,
        "all_billed": len(connected) == len(PROVIDERS),
        "connected": sorted(connected),
        "providers": providers,
        "provider_bars": bars(sorted(((p["label"], p["amount"]) for p in providers), key=lambda x: -x[1])),
        "stage_bars": bars([(stage, stage_totals.get(stage, 0)) for stage in (*STAGES, "Other")]),
        "platform_bars": bars(sorted(((name or "Not tied to a post", amount) for name, amount in platforms.items()),
                                     key=lambda x: -x[1])),
        "recorded_total": recorded_total,
        "purposes": sorted(({"stage": k[0], "label": k[1], "provider": k[2], "model": k[3], "count": v["count"],
                             "cost": v["cost"], "units": dict(v["units"])} for k, v in purposes.items()),
                           key=lambda row: -row["cost"]),
        "overall": overall,
        "published": len(live),
        "simulated": simulated,
        "cost_per_post": total / len(live) if live else None,
        "by_platform": by_platform,
        "kinds": kinds,
        "posts": post_rows[:60],
        "unposted": unposted,
        "no_post": no_post,
        "unattributed": unattributed,
        "series": series,
        "peak": peak,
        "prices": prices(),
    }


def _sum_by(pairs):
    totals = defaultdict(float)
    for key, amount in pairs:
        totals[key] += amount
    return totals.items()
