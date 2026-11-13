"""Durable creative jobs and bounded recurring campaigns for the publishing worker."""

import hashlib
import math
from datetime import timedelta
from html.parser import HTMLParser
from typing import Literal

import httpx
from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator
from sqlalchemy import case, select, update

from .config import PLATFORMS, profiles
from .db import now, uid
from .idea_plan import END_CARD_SECONDS, MAX_CLIPS, MIN_CLIP_SECONDS, Shot
from .lifecycle import transition
from .models import Account, Asset, Control, FactSheet, MediaCampaign, MediaJob, Post
from .pipeline import draft
from .prompting import utf16_trim, write_prompt
from .provider_errors import ProviderError
from .renditions import image_rendition, logo_card, video_rendition, video_sequence
from .rights import combined_rights, owner_value, usage_rights
from .runway import Runway, payload_for
from .storage import ingest, storage
from .video_segments import current_segment, edit_reference, plan_segments, save_segment_prompt, segment_brief

# Directions describe how to show the chosen Tudelu system; the prompt writer picks the system.
CONCEPTS = (
    ("detail", "Close-up of the system's materials, joints and finish in soft side light."),
    ("space", "Wide view of the installed system in its setting at human scale, natural light."),
    ("process", "The system in operation: its panels, louvers or blades moving through their range."),
    ("daylight", "Light and shadow moving across the installed system through the day, warm editorial mood."),
    ("geometry", "Graphic composition of the system's lines and rhythm, restrained colors, clean lines."),
    ("seasonal", "The installed system in an inviting seasonal setting with subtle environmental movement."),
    ("focus", "One defining feature of the system with generous negative space and a slow reveal."),
    ("inspiration", "An unexpected viewpoint of the installed system, refined textures, calm atmosphere."),
)


class CreativeBrief(BaseModel):
    campaign: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=1000)
    operation: Literal["image", "video", "video_edit", "audio", "mixed"] = "image"
    platforms: list[str] = Field(min_length=1, max_length=5)
    facts_id: str
    product: str = Field(default="company", min_length=1, max_length=200)
    kind: Literal["promotional", "non_promotional", "mixed"] = "promotional"
    reference_ids: list[str] = Field(default_factory=list, max_length=30)
    audio_reference_ids: list[str] = Field(default_factory=list, max_length=3)
    audio_prompt: str = Field(default="", max_length=1800)
    rights: str = Field(min_length=1, max_length=2000)
    owner: str = Field(default="", max_length=200)
    duration: int = Field(default=5, ge=3, le=90)
    website: bool = True
    create_posts: bool = True
    interval_hours: int = Field(default=24, ge=24, le=720)
    workflow: Literal["automatic", "immediate", "next"] = "automatic"
    # Clips in viewing order (setting and Tudelu system each), from the reviewed idea plan.
    shots: list[Shot] = Field(default_factory=list, max_length=MAX_CLIPS)
    # Company logo image appended as a short end card after the generated clips.
    end_card_asset_id: str = Field(default="", max_length=64)

    @field_validator("rights")
    @classmethod
    def rights_choice(cls, value, info: ValidationInfo):
        # Briefs saved before the Public/Not public choice keep their text when reloaded.
        if (info.context or {}).get("stored") and value.strip():
            return value
        return usage_rights(value)

    @field_validator("owner")
    @classmethod
    def owner_text(cls, value):
        return owner_value(value)

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
        if self.end_card_asset_id and self.operation not in {"video", "video_edit"}:
            raise ValueError("A logo end card requires a video")
        if self.end_card_asset_id and self.duration < END_CARD_SECONDS + MIN_CLIP_SECONDS:
            raise ValueError(
                f"A logo end card needs a video of at least {END_CARD_SECONDS + MIN_CLIP_SECONDS} seconds"
            )
        if len(self.shots) > 1 and self.operation != "video":
            raise ValueError("Several clips require a generated video")
        if self.operation == "video" and len(self.shots) > 1:
            footage = self.duration - (END_CARD_SECONDS if self.end_card_asset_id else 0)
            if footage < MIN_CLIP_SECONDS * len(self.shots):
                raise ValueError(
                    f"{len(self.shots)} clips need at least {MIN_CLIP_SECONDS * len(self.shots)} seconds of "
                    "footage. Lengthen the video or remove clips."
                )
        return self


class WebsiteText(HTMLParser):
    """Visible homepage text in page order, plus meta descriptions and image descriptions."""

    SKIP = {"script", "style", "noscript", "svg", "template"}
    META = {"description", "og:description", "twitter:description", "keywords"}

    def __init__(self):
        super().__init__()
        self.lines, self.images, self.meta, self.skip = [], [], [], 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in self.SKIP:
            self.skip += 1
        if tag == "meta" and (attrs.get("name") or attrs.get("property")) in self.META:
            self.meta.append(attrs.get("content") or "")
        if tag == "img" and (attrs.get("alt") or "").strip():
            self.images.append(" ".join(attrs["alt"].split()))

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        text = " ".join(data.split())
        # Page order is kept so labels such as "Coming soon" stay beside their system.
        if not self.skip and text and (not self.lines or self.lines[-1] != text):
            self.lines.append(text)


def website_snapshot(settings):
    if settings.ai_mode == "fixture":
        return {
            "url": "https://tudelu.com/",
            "text": "Offline architectural design fixture",
            "image_descriptions": [],
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
        text = " | ".join([m for m in parser.meta if m.strip()] + parser.lines)[:6000]
        if not text:
            raise ValueError("No brand context on website")
        return {
            "url": "https://tudelu.com/",
            "text": text,
            "image_descriptions": list(dict.fromkeys(parser.images))[:30],
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
        if brief.rights == "public" and asset.rights == "not_public":
            raise ValueError(
                f"{asset.original_name} is marked Not public, so media made from it cannot be Public. "
                "Choose Not public, or use a reference cleared for public use."
            )
        sources.append(asset)
    if brief.end_card_asset_id:
        logo = db.get(Asset, brief.end_card_asset_id)
        if not logo or not logo.media["mime"].startswith("image/"):
            raise ValueError("Choose an uploaded logo image for the end card")
        if logo.rights_expire_at and logo.rights_expire_at <= now():
            raise ValueError("The logo's usage rights have expired")
        if live and logo.demo_only:
            raise ValueError("Live campaigns cannot use a demo logo")
        if brief.rights == "public" and logo.rights == "not_public":
            raise ValueError(
                f"{logo.original_name} is marked Not public, so it cannot close a Public video. "
                "Upload the logo as Public, or choose Not public."
            )
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
    footage = brief.duration - (END_CARD_SECONDS if brief.end_card_asset_id else 0)
    if brief.operation == "video_edit" and footage > 10 and visual[0].media["duration"] + 0.1 < footage:
        raise ValueError("The requested edit is longer than the uploaded video. Choose a shorter duration or generate a new video.")
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
        intent = (
            "Invite exploration of the system."
            if content_kind == "promotional"
            else "Editorial design inspiration rather than a sales pitch."
        )
        values.update(
            operation=operation,
            kind=content_kind,
            reference_ids=refs,
            concept=concept,
            direction=f"{direction} {intent}",
            # Visual prompts are written by the worker just before submission (social/prompting.py).
            generation_prompt=utf16_trim(brief.prompt) if operation == "audio" else "",
            website_snapshot=snapshot,
            seed=int(hashlib.sha256((batch + platform).encode()).hexdigest()[:8], 16),
            execution_mode=settings.ai_mode,
        )
        if operation in {"video", "video_edit"}:
            if brief.duration > profile.max_duration:
                raise ValueError(f"{platform}: choose a duration of at most {profile.max_duration:g} seconds")
            end_card = (
                {"asset_id": brief.end_card_asset_id, "seconds": END_CARD_SECONDS}
                if brief.end_card_asset_id
                else None
            )
            # Generated or edited footage; the logo end card is rendered locally after it.
            footage = brief.duration - (END_CARD_SECONDS if end_card else 0)
            maximum = 30 if operation == "video_edit" else 10
            shots = brief.shots if operation == "video" else []
            edit_source = db.get(Asset, refs[0]) if operation == "video_edit" and refs else None
            if edit_source and footage > 10 and edit_source.media["duration"] + 0.1 < footage:
                raise ValueError("The requested edit is longer than the selected source video")
            if (
                footage > maximum
                or end_card
                or len(shots) > 1
                or (edit_source and edit_source.media["duration"] > 30)
            ):
                segments = plan_segments(footage, maximum, len(shots) if len(shots) > 1 else None)
                if len(shots) > 1:
                    # Each clip shows its planned setting and system; extra clips continue the nearest shot.
                    for segment in segments:
                        segment["shot"] = shots[segment["index"] * len(shots) // len(segments)].model_dump()
                values["video_segments"] = segments
            values["end_card"] = end_card
            # A full-length music track for a plain soundtrack direction; seed_audio reworks uploaded bites.
            values["soundtrack_model"] = (
                "music" if brief.audio_prompt.strip() and not brief.audio_reference_ids else "seed_audio"
            )
        if operation == "image":
            values.update(audio_prompt="", audio_reference_ids=[], end_card_asset_id="")
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
        brief = CreativeBrief.model_validate(campaign.brief, context={"stored": True})
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
    brief = segment_brief(job)
    segment = current_segment(job) if job.stage == "visual" else None
    is_audio = job.stage == "audio" or brief["operation"] == "audio"
    model = (
        (settings.runway_music_model if brief.get("soundtrack_model") == "music" else settings.runway_audio_model)
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
        f"runway-{job.id}-{job.stage}{'-' + str(segment['index'] + 1) if segment else ''}.{extension}",
        source="ai_edited" if refs else "ai_generated",
        generator=("fixture/runway/" if brief["execution_mode"] == "fixture" else "runway/") + model,
        prompt=prompt,
        rights=combined_rights(brief["rights"], refs),
        owner=brief.get("owner", ""),
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
            "creative_brief": {k: brief[k] for k in ("prompt", "concept", "kind")}
            | {"system": (segment or {}).get("system") or brief.get("depicted_system", "")},
            "generated_audio": is_audio,
            "audio_prompt": brief["audio_prompt"],
            **({"video_segment": {"index": segment["index"], "start": segment["start"],
                                   "duration": segment["duration"], "input_asset_id": segment.get("input_asset_id")}}
               if segment else {}),
        },
    )
    job.source_asset_ids = job.source_asset_ids + [asset.id]
    job.task_history = job.task_history + [{"stage": job.stage, "task_id": job.task_id, "model": model,
                                           **({"segment_index": segment["index"], "output_asset_id": asset.id,
                                               "prompt": prompt, "duration": segment["duration"]} if segment else {})}]
    if segment:
        job.brief = job.brief | {"video_segments": [s | {"output_asset_id": asset.id} if s["index"] == segment["index"] else s
                                                 for s in job.brief["video_segments"]]}
    job.task_id, job.submitted_at = "", None
    if segment and current_segment(job):
        job.state = "queued"
        db.commit()
        return
    # A clip regeneration reuses the finished video's soundtrack instead of composing another.
    job.state = "queued" if not is_audio and brief["audio_prompt"] and not brief.get("soundtrack_asset_id") else "rendering"
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
        if hashlib.sha256(data).hexdigest() != first.checksum:
            raise ValueError("Source integrity failed before creating the publishing rendition")
        end_card = brief.get("end_card") if brief["operation"] != "image" else None
        logo = db.get(Asset, end_card["asset_id"]) if end_card else None
        if end_card and not logo:
            raise ValueError("The end-card logo is missing from the asset library; upload it and create the post again")
        if brief["operation"] == "image":
            data = image_rendition(data, profile.image_width, profile.image_height,
                                   source=first.source, generator=first.generator)
            name = f"{job.platform}-{job.id}.jpg"
        else:
            sound_asset = next((a for a in originals if a.media["mime"].startswith("audio/")), None)
            soundtrack = storage(settings).read(sound_asset.storage_key) if sound_asset else None
            if brief.get("video_segments"):
                clips = []
                for segment in brief["video_segments"]:
                    asset = db.get(Asset, segment.get("output_asset_id"))
                    if not asset:
                        raise ValueError("A planned video clip is missing; resume generation before assembly")
                    clip_data = storage(settings).read(asset.storage_key)
                    if hashlib.sha256(clip_data).hexdigest() != asset.checksum:
                        raise ValueError("Video clip integrity failed before assembly")
                    clips.append((clip_data, 0, segment["duration"]))
                if logo:
                    logo_data = storage(settings).read(logo.storage_key)
                    if hashlib.sha256(logo_data).hexdigest() != logo.checksum:
                        raise ValueError("Logo integrity failed before creating the end card")
                    card = logo_card(logo_data, profile.video_width, profile.video_height, end_card["seconds"])
                    clips.append((card, 0, end_card["seconds"]))
                data = video_sequence(clips, profile.video_width, profile.video_height, soundtrack,
                                      require_full_duration=True)
            else:
                data = video_rendition(
                    data, profile.video_width, profile.video_height, brief["duration"], soundtrack
                )
            name = f"{job.platform}-{job.id}.mp4"
        # A multi-clip video records every clip's prompt, not only the first clip's.
        clip_prompts = list(dict.fromkeys(s["prompt"] for s in brief.get("video_segments", []) if s.get("prompt")))
        result = ingest(
            db,
            settings,
            data,
            name,
            source=first.source,
            generator=first.generator,
            prompt=" ".join(f"Clip {n}: {p}" for n, p in enumerate(clip_prompts, 1))
            if len(clip_prompts) > 1
            else first.prompt,
            rights=combined_rights(first.rights, [logo] if logo else []),
            owner=first.owner,
            rights_expire_at=min(
                [e for e in (first.rights_expire_at, logo.rights_expire_at if logo else None) if e], default=None
            ),
            demo_only=first.demo_only or bool(logo and logo.demo_only),
            tags=first.tags
            | {
                "source_asset_ids": brief["reference_ids"]
                + brief["audio_reference_ids"]
                + job.source_asset_ids
                + ([logo.id] if logo else []),
                "rendition": {"platform": job.platform, "original_asset_id": first.id},
                "media_job_id": job.id,
                **({"replaces": brief["replaces"]} if brief.get("replaces") else {}),
                "generated_audio": any(a.media["mime"].startswith("audio/") for a in originals),
                **({"video_sequence": brief["video_segments"], "requested_duration": brief["duration"]}
                   if brief.get("video_segments") else {}),
                **({"end_card": {
                    "asset_id": logo.id,
                    "checksum": logo.checksum,
                    "seconds": end_card["seconds"],
                    "note": "The final seconds show Tudelu's official logo, supplied as company media and "
                    "added by the app after generation.",
                }} if logo else {}),
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
        replaced = brief.get("replaces")
        held = db.get(Post, replaced["post_id"]) if replaced else None
        if held and held.status == "held":
            transition(
                db,
                held,
                "failed",
                f"Replaced by post {post.id} after clip {replaced['clip_index'] + 1} was regenerated",
                replaced.get("requested_by") or "admin",
            )
    job.state, job.error = "complete", ""
    db.commit()


def needs_prompt(job):
    """Visual jobs get a written prompt once; queued jobs from before this existed are rewritten."""
    return (
        job.stage == "visual"
        and job.brief["operation"] != "audio"
        and "prompt_writer" not in job.brief
        and not job.task_history
    )


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
                    {k: v for k, v in job.brief.items() if k in CreativeBrief.model_fields},
                    context={"stored": True},
                )
                validate_sources(db, brief, live=settings.wants_live(job.platform, db.get(Account, job.platform)))
                if needs_prompt(job):
                    # Written once and saved before any paid request; resume reuses it.
                    job.brief = job.brief | write_prompt(db, settings, job)
                    db.commit()
                save_segment_prompt(db, job)
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
                        segment = current_segment(job)
                        data = seed_media(duration=segment["duration"] if segment else 4)[0 if brief.operation == "image" else 1]
                    _save_output(db, settings, job, data)
                    continue
                if settings.ai_mode != "live":
                    continue
                if not settings.runway_api_key.get_secret_value():
                    raise ValueError("RUNWAY_API_KEY is missing")
                ids = brief.audio_reference_ids if job.stage == "audio" else brief.reference_ids
                segment = current_segment(job) if job.stage == "visual" else None
                if segment and segment.get("shot") and segment["index"] > 0 and brief.operation == "video":
                    # A start image belongs to the first planned clip; later clips show other settings.
                    ids = []
                if job.stage == "audio" and job.brief.get("soundtrack_model") == "music":
                    ids = []
                if brief.operation == "audio":
                    ids = list(dict.fromkeys(ids + brief.audio_reference_ids))[:3]
                if job.stage == "visual" and brief.operation == "video_edit" and current_segment(job):
                    # Never send the unsplit original to Runway for a segmented edit.
                    references = [runway.reference(edit_reference(db, settings, job))]
                else:
                    references = [runway.reference(db.get(Asset, i)) for i in ids]
                endpoint, payload = payload_for(
                    settings, segment_brief(job), job.platform, references, audio=job.stage == "audio"
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
        unfinished_video = job.stage == "visual" and current_segment(job)
        waiting_audio = job.stage == "audio" and not any(h["stage"] == "audio" for h in job.task_history)
        job.state = "queued" if unfinished_video or waiting_audio else "rendering"
    else:
        job.state = "queued"  # Failure occurred before the paid submission checkpoint.
    job.error = ""
