"""Durable creative jobs and bounded recurring campaigns for the publishing worker."""

import hashlib
import math
from datetime import timedelta
from html.parser import HTMLParser
from typing import Literal

import httpx
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import case, select, update

from .config import PLATFORMS, profiles
from .db import now, uid
from .facts import Facts, active
from .models import Account, Asset, Control, FactSheet, MediaCampaign, MediaJob, Post
from .pipeline import draft
from .provider_errors import ProviderError
from .renditions import image_rendition, video_rendition
from .runway import Runway, payload_for
from .storage import ingest, storage

CONCEPTS = (
    ("detail", "Close-up material detail, soft side light, precise architectural composition."),
    ("space", "Wide architectural space study, natural light, calm human-scale perspective."),
    ("process", "Design-studio mood, tactile material palette, thoughtful composition."),
    ("daylight", "Explore light and shadow through the day, warm editorial atmosphere."),
    ("geometry", "Graphic study of geometry and rhythm, restrained colors, clean lines."),
    ("seasonal", "An inviting seasonal setting, subtle environmental movement."),
    ("focus", "One focal product detail with generous negative space and a slow reveal."),
    ("inspiration", "Design inspiration vignette, unexpected viewpoint, refined textures."),
)


class CreativeBrief(BaseModel):
    campaign: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=450)
    operation: Literal["image", "video", "video_edit", "audio", "mixed"] = "image"
    platforms: list[str] = Field(min_length=1, max_length=5)
    facts_id: str
    product: str = Field(default="company", min_length=1, max_length=200)
    kind: Literal["promotional", "non_promotional", "mixed"] = "promotional"
    reference_ids: list[str] = Field(default_factory=list, max_length=30)
    audio_reference_ids: list[str] = Field(default_factory=list, max_length=3)
    audio_prompt: str = Field(default="", max_length=1800)
    rights: str = Field(min_length=1, max_length=2000)
    duration: int = Field(default=5, ge=3, le=10)
    website: bool = True
    create_posts: bool = True
    interval_hours: int = Field(default=24, ge=24, le=720)
    workflow: Literal["automatic", "immediate", "next"] = "automatic"

    @model_validator(mode="after")
    def valid(self):
        if not self.campaign.strip() or not self.prompt.strip() or not self.rights.strip():
            raise ValueError("Campaign, prompt and usage rights cannot be blank")
        if len(set(self.platforms)) != len(self.platforms) or set(self.platforms) - set(PLATFORMS):
            raise ValueError("Select distinct supported platforms")
        if self.operation == "audio" and self.create_posts:
            raise ValueError("Save audio to the library, or add it as a video soundtrack")
        if self.operation == "audio" and len(set(self.reference_ids + self.audio_reference_ids)) > 3:
            raise ValueError("Audio generation accepts at most three reference bites in total")
        if self.operation == "image" and "tiktok" in self.platforms and self.create_posts:
            raise ValueError("TikTok requires video; choose video or a mixed campaign")
        if self.audio_reference_ids and not self.audio_prompt.strip() and self.operation != "audio":
            raise ValueError("Describe how to use or edit the audio references")
        if self.operation == "image" and (self.audio_prompt or self.audio_reference_ids):
            raise ValueError("Soundtracks require a video operation")
        return self


class WebsiteText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.capture = [], False

    def handle_starttag(self, tag, attrs):
        if tag in {"title", "h1", "h2"}:
            self.capture = True
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("name") == "description":
            self.parts.append(attrs.get("content", ""))

    def handle_endtag(self, tag):
        if tag in {"title", "h1", "h2"}:
            self.capture = False

    def handle_data(self, data):
        if self.capture and data.strip():
            self.parts.append(data.strip())


def website_snapshot(settings):
    if settings.ai_mode == "fixture":
        return {
            "url": "https://tudelu.com/",
            "text": "Offline architectural design fixture",
            "demo_only": True,
        }
    try:
        # Fixed company origin; no arbitrary URLs, scripts, or instructions are executed.
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            with client.stream("GET", "https://tudelu.com/") as response:
                response.raise_for_status()
                content = bytearray()
                for part in response.iter_bytes():
                    content.extend(part)
                    if len(content) > 2_000_000:
                        raise ValueError("Website response too large")
        parser = WebsiteText()
        parser.feed(content.decode("utf-8", errors="replace"))
        summary = " ".join(" ".join(parser.parts).split())[:1200]
        if not summary:
            raise ValueError("No brand context on website")
        return {
            "url": "https://tudelu.com/",
            "text": summary,
            "sha256": hashlib.sha256(content).hexdigest(),
            "fetched_at": now().isoformat(),
        }
    except (httpx.HTTPError, ValueError):
        raise ValueError(
            "Could not read tudelu.com brand context. Retry later or turn off website inspiration."
        ) from None


def validate_sources(db, brief, *, live=False):
    sheet = db.get(FactSheet, brief.facts_id)
    if not sheet:
        raise ValueError("Select an existing approved facts version")
    if live and sheet.document.get("demo_only"):
        raise ValueError("Live campaigns require approved non-demo facts")
    sources = []
    for asset_id in dict.fromkeys(brief.reference_ids + brief.audio_reference_ids):
        asset = db.get(Asset, asset_id)
        if (
            not asset
            or not asset.rights.strip()
            or (asset.rights_expire_at and asset.rights_expire_at <= now())
        ):
            raise ValueError("A reference is missing or its usage rights have expired")
        if live and asset.demo_only:
            raise ValueError("Live campaigns cannot use demo references")
        if asset.tags.get("real_customer_content") and not (
            asset.customer_permission and asset.permission_reference
        ):
            raise ValueError("Customer references require documented permission")
        if asset_id in brief.audio_reference_ids and not asset.media["mime"].startswith("audio/"):
            raise ValueError("Soundtrack references must be audio files")
        sources.append(asset)
    visual = [a for a in sources if a.id in brief.reference_ids]
    expected = {"image": "image/", "video": "image/", "video_edit": "video/", "audio": "audio/"}
    if brief.operation in expected and any(
        not a.media["mime"].startswith(expected[brief.operation]) for a in visual
    ):
        raise ValueError("Reference media type does not match the selected operation")
    limit = {"image": 3, "video": 1, "video_edit": 1, "audio": 3}.get(brief.operation, 30)
    if len(visual) > limit:
        raise ValueError(f"This operation accepts at most {limit} reference assets")
    if brief.operation == "video_edit" and len(visual) != 1:
        raise ValueError("Video editing requires one uploaded video")
    if brief.operation == "mixed" and any(a.media["mime"].startswith("audio/") for a in visual):
        raise ValueError("Put audio in the soundtrack reference selection")
    if any(a.media["mime"].startswith("video/") and a.media["duration"] > 30 for a in visual):
        raise ValueError("Runway video edits accept clips up to 30 seconds; upload a shorter source clip")
    return sources


def queue_jobs(db, settings, brief, *, campaign=None, sequence=0, snapshot=None):
    brief = CreativeBrief.model_validate(brief)
    sources = validate_sources(db, brief, live=any(settings.wants_live(p, db.get(Account, p)) for p in brief.platforms))
    if settings.ai_mode == "live" and not settings.runway_api_key.get_secret_value():
        raise ValueError("RUNWAY_API_KEY is missing. Fill in .env and restart web/worker.")
    if (
        brief.operation != "audio"
        and settings.image_provider != "runway"
        and brief.operation in {"image", "mixed"}
    ):
        raise ValueError("Media Studio images require IMAGE_PROVIDER=runway")
    if brief.operation in {"video", "video_edit", "mixed"} and settings.video_provider != "runway":
        raise ValueError("Media Studio videos require VIDEO_PROVIDER=runway")
    if not campaign and (brief.operation == "mixed" or brief.kind == "mixed"):
        raise ValueError("Mixed format/content is for recurring campaigns; select a specific one-off format")
    snapshot = snapshot if snapshot is not None else (website_snapshot(settings) if brief.website else {})
    batch = f"{campaign.id}:{sequence}" if campaign else uid()
    result = []
    for platform in brief.platforms:
        profile = profiles(settings)[platform]
        recent = db.scalars(
            select(MediaJob)
            .where(MediaJob.platform == platform)
            .order_by(MediaJob.created_at.desc())
            .limit(16)
        ).all()
        used = [j.brief.get("concept") for j in recent[: len(CONCEPTS) - 1]]
        concept, direction = next(
            (c for c in CONCEPTS if c[0] not in used), CONCEPTS[sequence % len(CONCEPTS)]
        )
        values = brief.model_dump()
        operation = brief.operation
        if operation == "mixed":
            options = ["image", "video"]
            if any(a.media["mime"].startswith("video/") for a in sources):
                options.append("video_edit")
            if platform == "tiktok":
                options = [v for v in options if v != "image"]
            if platform == "facebook" and settings.wants_live(platform, db.get(Account, platform)):
                options = ["image"]  # Existing native AI-video disclosure gate remains in force.
            operation = options[sequence % len(options)]
        content_kind = brief.kind
        if content_kind == "mixed":
            history = db.scalars(
                select(Post).where(
                    Post.platform == platform,
                    Post.status != "deleted",
                    Post.created_at >= now() - timedelta(days=7),
                )
            ).all()
            recent_kinds = [p.kind for p in history] + [j.brief["kind"] for j in recent if not j.post_id]
            promotions = recent_kinds.count("promotional")
            content_kind = (
                "promotional"
                if promotions + 1 <= math.ceil((len(recent_kinds) + 1) * profile.promotional_ratio)
                else "non_promotional"
            )
        mime = {"image": "image/", "video": "image/", "video_edit": "video/", "audio": "audio/"}[operation]
        refs = [a.id for a in sources if a.id in brief.reference_ids and a.media["mime"].startswith(mime)]
        if campaign and refs:
            offset = sequence % len(refs)
            refs = refs[offset:] + refs[:offset]
        refs = refs[: 3 if operation in {"image", "audio"} else 1]
        facts = Facts.model_validate(db.get(FactSheet, brief.facts_id).document)
        approved = [
            f.text for f in facts.facts if active(f, now()) and f.product in {brief.product, "company"}
        ]
        intent = (
            "Invite exploration; no invented product claims."
            if content_kind == "promotional"
            else ("Editorial design inspiration; no sales pitch or purchase call to action.")
        )
        suffix = f" {direction} {intent} {platform}: {profile.tone[:65]}. "
        suffix += "Preserve referenced product geometry. No text, logos or customer testimony."
        if snapshot:
            suffix += " Brand mood from tudelu.com: " + snapshot["text"][:90]
        if approved:
            suffix += " Approved context: " + approved[sequence % len(approved)][:90]
        prompt = brief.prompt if operation == "audio" else brief.prompt + suffix
        # API limit is UTF-16 units; user brief is always retained before optional context.
        prompt = prompt.encode("utf-16-le")[:2000].decode("utf-16-le", errors="ignore")
        values.update(
            operation=operation,
            kind=content_kind,
            reference_ids=refs,
            concept=concept,
            generation_prompt=prompt,
            website_snapshot=snapshot,
            seed=int(hashlib.sha256((batch + platform).encode()).hexdigest()[:8], 16),
            execution_mode=settings.ai_mode,
        )
        if operation == "image":
            values.update(audio_prompt="", audio_reference_ids=[])
        job = MediaJob(
            campaign_id=campaign.id if campaign else None,
            batch_key=batch + ":" + platform,
            platform=platform,
            brief=values,
            state="planned" if brief.workflow == "next" and not campaign else "queued",
        )
        db.add(job)
        result.append(job)
    db.flush()
    return result


def activate_planned_jobs(db, settings):
    from .automation import can_automate

    if not can_automate(db, settings):
        return
    activated = set()
    for job in db.scalars(select(MediaJob).where(MediaJob.state == "planned").order_by(MediaJob.created_at)).all():
        if not can_automate(db, settings):
            return
        account = db.get(Account, job.platform)
        if account.paused or job.platform in activated:
            continue
        busy = db.scalar(select(MediaJob.id).where(
            MediaJob.platform == job.platform, MediaJob.state.in_(["queued", "submitting", "running", "rendering"])
        ).limit(1))
        if busy:
            continue
        changed = db.execute(update(MediaJob).where(MediaJob.id == job.id, MediaJob.state == "planned").values(state="queued"))
        db.commit()
        if changed.rowcount:
            activated.add(job.platform)


def replenish_campaigns(db, settings):
    from .automation import can_automate

    if not can_automate(db, settings) or settings.ai_mode != "live":
        return
    for campaign in db.scalars(
        select(MediaCampaign).where(MediaCampaign.enabled.is_(True), MediaCampaign.next_run_at <= now())
    ).all():
        if not can_automate(db, settings):
            return
        brief = CreativeBrief.model_validate(campaign.brief)
        pending_idea = db.scalar(select(MediaJob.id).where(
            MediaJob.platform.in_(brief.platforms),
            MediaJob.brief["workflow"].as_string().in_(["next", "immediate"]),
            MediaJob.state.in_(["planned", "queued", "submitting", "running", "rendering", "failed", "uncertain"]),
        ).limit(1))
        if pending_idea:
            campaign.error = "Waiting for the admin's requested post idea to finish."
            db.commit()
            continue
        # Active recurring campaigns only spend when every selected publishing account is live/ready.
        from .adapters import ADAPTERS

        if any(
            not settings.wants_live(p, db.get(Account, p)) or db.get(Account, p).paused or ADAPTERS[p](settings, db, None).dry
            for p in brief.platforms
        ):
            campaign.error = "Waiting for selected platforms to be live, connected and unpaused."
            db.commit()
            continue
        jobs = db.scalars(select(MediaJob).where(MediaJob.campaign_id == campaign.id)).all()
        if any(j.state != "complete" and j.state != "cancelled" for j in jobs) or any(
            j.post_id and db.get(Post, j.post_id).status not in {"published", "awaiting_manual", "deleted"}
            for j in jobs
        ):
            campaign.error = "Waiting for the previous batch to finish or for held work to be corrected."
            db.commit()
            continue
        try:
            queue_jobs(db, settings, brief, campaign=campaign, sequence=campaign.sequence)
            campaign.sequence += 1
            campaign.next_run_at = now() + timedelta(hours=brief.interval_hours)
            campaign.error = ""
            db.commit()
        except ValueError as exc:
            db.rollback()
            campaign.error = str(exc)
            campaign.enabled = False
            db.commit()


def _save_output(db, settings, job, data):
    brief = job.brief
    is_audio = job.stage == "audio" or brief["operation"] == "audio"
    model = (
        settings.runway_audio_model
        if is_audio
        else (
            settings.runway_image_model
            if brief["operation"] == "image"
            else settings.runway_edit_model
            if brief["operation"] == "video_edit"
            else settings.runway_video_model
        )
    )
    refs = [db.get(Asset, i) for i in brief["reference_ids"] + brief["audio_reference_ids"]]
    expiries = [a.rights_expire_at for a in refs if a.rights_expire_at]
    prompt = brief["audio_prompt"] if job.stage == "audio" else brief["generation_prompt"]
    extension = (
        ("wav" if data.startswith(b"RIFF") else "mp3")
        if is_audio
        else ("png" if brief["operation"] == "image" else "mp4")
    )
    asset = ingest(
        db,
        settings,
        data,
        f"runway-{job.id}-{job.stage}.{extension}",
        source="ai_edited" if refs else "ai_generated",
        generator=("fixture/runway/" if brief["execution_mode"] == "fixture" else "runway/") + model,
        prompt=prompt,
        rights=brief["rights"],
        rights_expire_at=min(expiries) if expiries else None,
        demo_only=brief["execution_mode"] == "fixture" or any(a.demo_only for a in refs),
        tags={
            "product": brief["product"],
            "campaign": brief["campaign"],
            "source_asset_ids": [a.id for a in refs],
            "runway_task_id": job.task_id,
            "reference_provenance": [
                {"asset_id": a.id, "checksum": a.checksum, "tags": a.tags, "rights": a.rights} for a in refs
            ],
            "media_job_id": job.id,
            "website_snapshot": brief["website_snapshot"],
            "creative_brief": {k: brief[k] for k in ("prompt", "concept", "kind")},
            "generated_audio": is_audio,
            "audio_prompt": brief["audio_prompt"],
        },
    )
    job.source_asset_ids = job.source_asset_ids + [asset.id]
    job.task_history = job.task_history + [{"stage": job.stage, "task_id": job.task_id, "model": model}]
    job.task_id, job.submitted_at = "", None
    job.state = "queued" if not is_audio and brief["audio_prompt"] else "rendering"
    if job.state == "queued":
        job.stage = "audio"
    db.commit()


def _finish(db, settings, job):
    from .visual_context import ensure_visual_summary

    brief = job.brief
    originals = [db.get(Asset, i) for i in job.source_asset_ids]
    first = originals[0]
    summary_settings = settings.model_copy(update={"ai_mode": brief["execution_mode"]})
    for original in originals:
        ensure_visual_summary(db, summary_settings, original)
        db.commit()
    if job.output_asset_id:
        result = db.get(Asset, job.output_asset_id)
    elif brief["operation"] == "audio":
        result = first
    else:
        profile = profiles(settings)[job.platform]
        data = storage(settings).read(first.storage_key)
        if brief["operation"] == "image":
            data = image_rendition(data, profile.image_width, profile.image_height)
            name = f"{job.platform}-{job.id}.jpg"
        else:
            soundtrack = storage(settings).read(originals[1].storage_key) if len(originals) > 1 else None
            data = video_rendition(
                data, profile.video_width, profile.video_height, brief["duration"], soundtrack
            )
            name = f"{job.platform}-{job.id}.mp4"
        result = ingest(
            db,
            settings,
            data,
            name,
            source=first.source,
            generator=first.generator,
            prompt=first.prompt,
            rights=first.rights,
            rights_expire_at=first.rights_expire_at,
            demo_only=first.demo_only,
            tags=first.tags
            | {
                "source_asset_ids": brief["reference_ids"]
                + brief["audio_reference_ids"]
                + job.source_asset_ids,
                "rendition": {"platform": job.platform, "original_asset_id": first.id},
                "generated_audio": len(originals) > 1,
            },
        )
        from .gate import media_errors

        errors = media_errors([result], profile, job.platform)
        if errors:
            raise ValueError("Generated rendition failed platform checks: " + ", ".join(errors))
    job.output_asset_id = result.id
    # Keep the rendition on summary failure; resume never regenerates or re-renders it.
    db.commit()
    ensure_visual_summary(db, summary_settings, result)
    db.commit()
    if brief["create_posts"] and not job.post_id:
        post = draft(
            db,
            job.platform,
            brief["campaign"],
            [result.id],
            brief["facts_id"],
            brief["product"],
            brief["kind"],
            commit=False,
        )
        job.post_id = post.id
    job.state, job.error = "complete", ""
    db.commit()


def process_jobs(db, settings, provider=None):
    control = db.get(Control, 1)
    db.refresh(control)
    if control.killed:
        return
    jobs = db.scalars(
        select(MediaJob)
        .where(MediaJob.state.in_(["queued", "submitting", "running", "rendering"]))
        .order_by(case((MediaJob.brief["workflow"].as_string() == "immediate", 0), else_=1), MediaJob.created_at)
        .limit(settings.media_jobs_per_tick)
    ).all()
    runway = provider or Runway(settings)
    try:
        for job in jobs:
            db.refresh(control)
            account = db.get(Account, job.platform)
            db.refresh(account)
            if control.killed:
                break
            if account.paused:
                continue
            if (
                job.brief["execution_mode"] == "live"
                and settings.ai_mode != "live"
                and job.state != "rendering"
            ):
                continue
            try:
                if job.state == "submitting":
                    job.state = "uncertain"
                    job.error = (
                        "Submission was interrupted. Reconcile the task ID with Runway before retrying."
                    )
                    db.commit()
                    continue
                if job.state == "rendering":
                    _finish(db, settings, job)
                    continue
                if job.state == "running":
                    if now() - job.submitted_at > timedelta(seconds=settings.runway_task_timeout_seconds):
                        raise ValueError(
                            "Runway task exceeded the wait limit. Resume the saved task to check again."
                        )
                    task = runway.task(job.task_id)
                    if task.get("status") in {"PENDING", "THROTTLED", "RUNNING"}:
                        continue
                    if task.get("status") in {"FAILED", "CANCELED", "CANCELLED"}:
                        raise ValueError(
                            "Runway task failed or was cancelled. Review it in Runway; no automatic resubmission."
                        )
                    if task.get("status") != "SUCCEEDED" or not task.get("output"):
                        raise ValueError("Runway returned an invalid task result")
                    _save_output(db, settings, job, runway.download(task["output"][0]))
                    continue
                brief = CreativeBrief.model_validate(
                    {k: v for k, v in job.brief.items() if k in CreativeBrief.model_fields}
                )
                validate_sources(db, brief, live=settings.wants_live(job.platform, db.get(Account, job.platform)))
                if job.brief["execution_mode"] == "fixture":
                    from .seed import seed_media

                    if job.stage == "audio" or brief.operation == "audio":
                        import io
                        import wave

                        stream = io.BytesIO()
                        with wave.open(stream, "wb") as wav:
                            wav.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
                            wav.writeframes(b"\0\0" * 48000 * brief.duration)
                        # PyAV detects the actual WAV bytes regardless of provider output extension.
                        data = stream.getvalue()
                    else:
                        data = seed_media()[0 if brief.operation == "image" else 1]
                    _save_output(db, settings, job, data)
                    continue
                if settings.ai_mode != "live":
                    continue
                if not settings.runway_api_key.get_secret_value():
                    raise ValueError("RUNWAY_API_KEY is missing")
                ids = brief.audio_reference_ids if job.stage == "audio" else brief.reference_ids
                if brief.operation == "audio":
                    ids = list(dict.fromkeys(ids + brief.audio_reference_ids))[:3]
                references = [runway.reference(db.get(Asset, i)) for i in ids]
                endpoint, payload = payload_for(
                    settings, job.brief, job.platform, references, audio=job.stage == "audio"
                )
                db.refresh(control)
                db.refresh(account)
                if control.killed or account.paused:
                    continue
                # Persist before the paid request; crash recovery never blindly re-submits.
                claimed = db.execute(
                    update(MediaJob)
                    .where(MediaJob.id == job.id, MediaJob.state == "queued")
                    .values(state="submitting")
                )
                if not claimed.rowcount:
                    db.rollback()
                    continue
                db.commit()
                job.task_id = runway.submit(endpoint, payload)
                job.state, job.submitted_at, job.error = "running", now(), ""
                db.commit()
            except Exception as exc:
                # Retain task IDs and saved originals for download/render recovery, without paid retries.
                db.rollback()
                db.refresh(job)
                if isinstance(exc, ProviderError) and exc.category == "poll":
                    job.error = str(exc)
                else:
                    rejected = (
                        isinstance(exc, ProviderError)
                        and exc.upstream_status is not None
                        and 400 <= exc.upstream_status < 500
                    )
                    job.state = "uncertain" if job.state == "submitting" and not rejected else "failed"
                    job.error = (
                        str(exc)
                        if isinstance(exc, ValueError)
                        else (
                            "Media processing failed ("
                            + type(exc).__name__
                            + "). Resume saved output after correcting storage/configuration."
                        )
                    )
                db.commit()
    finally:
        if provider is None:
            runway.close()


def resume_job(job):
    if job.state not in {"failed", "uncertain"}:
        raise ValueError("Only failed or uncertain jobs can be resumed")
    if job.task_id:
        job.state, job.submitted_at = "running", now()
    elif job.state == "uncertain":
        raise ValueError("Reconcile the interrupted submission's task ID in Runway before resuming")
    elif job.source_asset_ids:
        job.state = "queued" if job.stage == "audio" and len(job.source_asset_ids) == 1 else "rendering"
    else:
        job.state = "queued"  # Failure occurred before the paid submission checkpoint.
    job.error = ""
