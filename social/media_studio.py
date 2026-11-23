"""Durable creative jobs and bounded recurring campaigns for the publishing worker."""

import hashlib
import math
import time
from datetime import timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Literal

import httpx
from pydantic import BaseModel, Field, ValidationError, ValidationInfo, field_validator, model_validator
from sqlalchemy import select, update

from .config import PLATFORMS, REAL_MEDIA_ONLY, profiles
from .db import now, uid
from .idea_plan import END_CARD_SECONDS, MAX_CLIPS, MIN_CLIP_SECONDS, Shot, campaign_video_signals
from .lifecycle import transition
from .media_search import campaign_references, campaign_scores, campaign_visuals, library_media
from .models import Account, Asset, Control, FactSheet, MediaCampaign, MediaJob, Post
from .monitoring import event
from .pipeline import draft
from .prompting import utf16_trim, write_prompt
from .provenance import requires_ai_review
from .provider_errors import ProviderError
from .real_edit import RealEditPlan, real_choice
from .recovery import runway_failure
from .renditions import fitted_size, image_rendition, logo_card, video_rendition, video_sequence
from .rights import combined_rights, owner_value, usage_rights
from .runway import Runway, job_settings, payload_for, video_clip_limit
from .shared_media import compatible_render, group_renders, runnable_jobs
from .storage import ingest, source_files, storage
from .video_rules import (
    AMBIENT_STYLES,
    NO_LOGO,
    ambient_tag,
    ambient_track,
    default_logo,
    fit_track,
    has_sound,
    music_tracks,
    next_ambient_style,
    seed_for,
    track_tag,
)
from .video_segments import (
    current_segment,
    edit_reference,
    original_shots,
    plan_segments,
    save_segment_prompt,
    segment_brief,
    sequence_clip,
)

# Directions describe how to show the chosen Tudelu system; the prompt writer picks the system.
CONCEPTS = (
    ("detail", "Close-up of the system's materials, joints and finish in soft side light."),
    ("space", "Wide view of the installed system in its setting at human scale, natural light."),
    (
        "process",
        "The system in use: a MUTE partition retracting overhead, or light and shade moving across a fixed system.",
    ),
    ("daylight", "Light and shadow moving across the installed system through the day, warm editorial mood."),
    ("geometry", "Graphic composition of the system's lines and rhythm, restrained colors, clean lines."),
    ("seasonal", "The installed system in an inviting seasonal setting with subtle environmental movement."),
    ("focus", "One defining feature of the system with generous negative space and a slow push-in."),
    ("inspiration", "An unexpected viewpoint of the installed system, refined textures, calm atmosphere."),
)
# AI campaign posts are inspiration: each suggests a different place the products are used, so a campaign
# shows their range. The prompt writer fits a Tudelu system to it, or keeps the setting the idea names.
USE_CASES = (
    "a family home's backyard or patio",
    "a restaurant or cafe's outdoor seating",
    "a hotel, resort or poolside lounge",
    "an office building's entrance or courtyard",
    "a school, campus or hospital walkway",
    "a shopping center or storefront",
    "a rooftop terrace or apartment amenity deck",
    "a drive-through lane, parking area or carport",
    "an event venue, winery or garden terrace",
    "an indoor space divided or opened up for different uses",
)

REFERENCE_TYPES = {"image": ("image/", "video/"), "video": ("image/", "video/"), "video_edit": "video/", "audio": "audio/"}


class CreativeBrief(BaseModel):
    campaign: str = Field(min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=1000)
    # real: the company's own photos and videos of completed projects, edited with standard tools only
    # (social/real_edit.py). mixed (recurring only) alternates real projects with AI inspiration.
    operation: Literal["image", "video", "video_edit", "audio", "real", "mixed"] = "image"
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
    interval_hours: float = Field(default=24, gt=0, allow_inf_nan=False)
    workflow: Literal["automatic", "immediate", "next"] = "automatic"
    # Clips in viewing order (setting and Tudelu system each), from the reviewed idea plan.
    shots: list[Shot] = Field(default_factory=list, max_length=MAX_CLIPS)
    # Company logo image appended as a short end card after the generated clips.
    end_card_asset_id: str = Field(default="", max_length=64)
    # Post the admin's own photo or video as it is: no Runway request, only a platform-sized copy.
    # reference_ids holds that photo or video and audio_reference_ids an optional soundtrack.
    as_uploaded: bool = False
    # Your own video only: the app's ambient music replaces its sound (always, when it has none).
    ambient_music: bool = False
    # Real project posts from an idea: the format chosen on the plan page, and the edit planned there,
    # used when the media, format and length weren't changed (social/real_edit.py).
    real_format: Literal["", "photo", "video"] = ""
    prepared_edit: dict = Field(default_factory=dict)

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

    @field_validator("interval_hours")
    @classmethod
    def recurring_interval(cls, value, info: ValidationInfo):
        if info.data.get("rights") != "not_public" and (not 24 <= value <= 720 or not value.is_integer()):
            raise ValueError("Public campaigns require interval_hours to be a whole number from 24 to 720")
        try:
            now() + timedelta(hours=value)
        except OverflowError:
            raise ValueError("interval_hours is too large to schedule") from None
        return value

    @model_validator(mode="after")
    def valid(self):
        if not self.campaign.strip() or not self.prompt.strip() or not self.rights.strip():
            raise ValueError("Campaign, prompt and usage rights cannot be blank")
        if len(set(self.platforms)) != len(self.platforms) or set(self.platforms) - set(PLATFORMS):
            raise ValueError("Select distinct supported platforms")
        # Real project posts and varied campaigns give Pinterest real Google Drive media only.
        if set(self.platforms) & REAL_MEDIA_ONLY and self.operation not in {"real", "mixed"}:
            raise ValueError(
                "Pinterest only posts real photos and videos from Google Drive. Choose Real project or a varied "
                "campaign, or create Pinterest posts from Post from library media."
            )
        if self.operation == "audio" and self.create_posts:
            raise ValueError("Save audio to the library, or add it as a video soundtrack")
        if self.as_uploaded:
            if self.operation not in {"image", "video"} or len(self.reference_ids) != 1:
                raise ValueError("Posting your media as it is takes one photo or one video")
            if self.shots or self.audio_prompt.strip() or len(self.audio_reference_ids) > 1:
                raise ValueError("Your own media is posted without generated clips or music")
            if self.operation == "image" and (self.audio_reference_ids or self.end_card_asset_id):
                raise ValueError("A photo can't carry a soundtrack or a logo end card")
            if self.ambient_music and (self.operation != "video" or self.audio_reference_ids):
                raise ValueError("Ambient music replaces a video's own sound; it can't be added to an audio file")
            return self
        if self.ambient_music:
            raise ValueError("Ambient music without a description is only for posting your own video")
        if self.prepared_edit and self.operation != "real":
            raise ValueError("A planned edit is only for real project posts")
        if self.operation == "audio" and len(set(self.reference_ids + self.audio_reference_ids)) > 3:
            raise ValueError("Audio generation accepts at most three reference bites in total")
        if self.operation == "image" and "tiktok" in self.platforms and self.create_posts:
            raise ValueError("TikTok requires video; choose video or a mixed campaign")
        if self.audio_reference_ids and not self.audio_prompt.strip() and self.operation not in {"audio", "real"}:
            raise ValueError("Describe how to use or edit the audio references")
        if self.operation == "image" and (self.audio_prompt or self.audio_reference_ids):
            raise ValueError("Soundtracks require a video operation")
        if self.end_card_asset_id and self.operation not in {"video", "video_edit", "real", "mixed"}:
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


class WebsiteContextError(ValueError):
    def __init__(self, reason, *, retryable=False, retry_after=0):
        self.retryable, self.retry_after = retryable, retry_after
        super().__init__(
            f"Could not read tudelu.com brand context: {reason}. Retry later or turn off website inspiration."
        )


def website_retry_after(response):
    """Honor a site's Retry-After without retaining response headers or URLs in errors."""
    value = response.headers.get("retry-after", "")
    try:
        delay = int(value) if value.isdigit() else parsedate_to_datetime(value).timestamp() - time.time()
        now() + timedelta(seconds=delay)  # Reject delays outside the supported timestamp range.
        return max(0, delay)
    except (ValueError, TypeError, OverflowError):
        return 0


def _website_snapshot():
    # Fixed company origin; no arbitrary URLs, scripts, or instructions are executed.
    with httpx.Client(timeout=httpx.Timeout(15, connect=5), follow_redirects=False) as client:
        with client.stream("GET", "https://tudelu.com/") as response:
            response.raise_for_status()
            content = bytearray()
            for part in response.iter_bytes(chunk_size=65536):
                if len(content) + len(part) > 2_000_000:
                    raise WebsiteContextError("the website response exceeded the 2 MB limit")
                content.extend(part)
    parser = WebsiteText()
    parser.feed(content.decode("utf-8", errors="replace"))
    text = " | ".join([m for m in parser.meta if m.strip()] + parser.lines)[:6000]
    if not text:
        raise WebsiteContextError("the page contained no readable brand context")
    return {
        "url": "https://tudelu.com/",
        "text": text,
        "image_descriptions": list(dict.fromkeys(parser.images))[:30],
        "sha256": hashlib.sha256(content).hexdigest(),
        "fetched_at": now().isoformat(),
    }


def website_snapshot(settings):
    if settings.ai_mode == "fixture":
        return {
            "url": "https://tudelu.com/",
            "text": "Offline architectural design fixture",
            "image_descriptions": [],
            "demo_only": True,
        }
    for attempt in range(2):
        status = None
        try:
            return _website_snapshot()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            error = WebsiteContextError(
                f"the website returned HTTP {status}", retryable=status in {408, 429} or status >= 500,
                retry_after=max(300 if status == 429 else 0, website_retry_after(exc.response)),
            )
        except httpx.TimeoutException:
            error = WebsiteContextError("the website request timed out", retryable=True)
        except httpx.HTTPError:
            error = WebsiteContextError("the website connection failed", retryable=True)
        except WebsiteContextError as exc:
            error = exc
        event("website_context_failed", attempt=attempt + 1, status=status, retryable=error.retryable,
              reason=str(error))
        if not error.retryable or error.retry_after or attempt == 1:
            raise error from None
        time.sleep(1)


def validate_sources(db, brief, *, live=False):
    sheet = db.get(FactSheet, brief.facts_id)
    if not sheet:
        raise ValueError("Select an existing approved facts version")
    if live and sheet.document.get("demo_only"):
        raise ValueError("Live campaigns require approved non-demo facts")
    sources = []
    for asset_id in dict.fromkeys(brief.reference_ids + brief.audio_reference_ids):
        asset = db.get(Asset, asset_id)
        if asset is not None and asset.removed_at is not None:
            raise ValueError(f"{asset.original_name} was deleted from the asset library. Choose other media.")
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
        check_logo(db.get(Asset, brief.end_card_asset_id), brief, live=live)
    visual = [a for a in sources if a.id in brief.reference_ids]
    if brief.as_uploaded:
        return _uploaded_sources(brief, visual, sources)
    if brief.operation == "real":
        if any(requires_ai_review(a) for a in sources):
            raise ValueError(
                "Real project posts show only real photos, videos and audio. Use AI-made media in an AI image "
                "or video instead."
            )
        if any(a.media["mime"].startswith("audio/") for a in visual):
            raise ValueError("Put audio in the soundtrack selection")
        return sources
    if brief.operation in REFERENCE_TYPES and any(
        not a.media["mime"].startswith(REFERENCE_TYPES[brief.operation]) for a in visual
    ):
        raise ValueError("Reference media type does not match the selected operation")
    limit = {"image": 3, "video": 1, "video_edit": 1, "audio": 3}.get(brief.operation, 30)
    if len(visual) > limit:
        raise ValueError(f"This operation accepts at most {limit} reference assets")
    if brief.operation == "video_edit" and len(visual) != 1:
        raise ValueError("Video editing requires one uploaded video")
    if brief.operation == "mixed" and any(a.media["mime"].startswith("audio/") for a in visual):
        raise ValueError("Put audio in the soundtrack reference selection")
    footage = brief.duration - END_CARD_SECONDS
    if brief.operation == "video_edit" and footage > 10 and visual[0].media["duration"] + 0.1 < footage:
        raise ValueError("The requested edit is longer than the uploaded video. Choose a shorter duration or generate a new video.")
    return sources


def check_logo(logo, brief, *, live=False):
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
    return logo


def ending_logo(db, brief, *, live=False):
    """The logo a video ends on: the one chosen in the brief, otherwise the newest tagged logo."""
    if not brief.end_card_asset_id and not (logo := default_logo(db, live=live)):
        raise ValueError(NO_LOGO)
    return check_logo(db.get(Asset, brief.end_card_asset_id) if brief.end_card_asset_id else logo, brief, live=live)


def _uploaded_sources(brief, visual, sources):
    """Checks for posting the admin's own media as it is."""
    if len(visual) != 1 or not visual[0].media["mime"].startswith(brief.operation + "/"):
        raise ValueError(f"Choose one {'photo' if brief.operation == 'image' else 'video'} to post as it is")
    if any(requires_ai_review(a) for a in sources):
        raise ValueError("Only real media can be posted as it is. Use AI-made media as a reference instead.")
    if brief.operation == "video":
        card = END_CARD_SECONDS
        length = visual[0].media["duration"]
        if brief.duration - card > length + 0.1:
            raise ValueError(
                f"Your video is {length:.0f} seconds long. Choose a length of at most {int(length + card)} seconds."
            )
    return sources


def queue_jobs(db, settings, brief, *, campaign=None, sequence=0, snapshot=None):
    brief = CreativeBrief.model_validate(brief)
    live = any(settings.wants_live(p, db.get(Account, p)) for p in brief.platforms)
    sources = validate_sources(db, brief, live=live)
    if brief.as_uploaded:
        return _queue_uploaded(db, settings, brief, snapshot, live=live)
    # Real project edits never call Runway; every other format may.
    generated = brief.operation != "real"
    if generated and settings.ai_mode == "live" and not settings.runway_api_key.get_secret_value():
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
    snapshot = snapshot if snapshot is not None else (
        website_snapshot(settings) if brief.website and generated else {}
    )
    batch = f"{campaign.id}:{sequence}" if campaign else uid()
    # One ambient style per batch, so each platform's version of the post sounds the same.
    style = next_ambient_style(db)
    campaign_media, scores = [], {}
    discover = campaign and brief.operation in {"mixed", "real", "image", "video"}
    if discover:
        campaign_media = (
            [a for a in sources if a.id in brief.reference_ids] if brief.reference_ids else
            library_media(db, product=brief.product, public=brief.rights != "not_public",
                          live=live or settings.ai_mode == "live", limit=None)
        )
        scores = campaign_scores(settings, brief.prompt, campaign_media)
    footage = brief.duration - END_CARD_SECONDS
    best_score = max(scores.values(), default=0)
    edit_sources = [a for a in campaign_media if a.media["mime"].startswith("video/")
                    and a.media["duration"] >= footage and scores[a.id] == best_score]
    signals = campaign_video_signals(brief.prompt) if brief.operation == "mixed" else {}
    video_requested = signals.get("video")
    result = []
    platform_profiles = profiles(settings)
    for platform in brief.platforms:
        profile = platform_profiles[platform]
        recent = db.scalars(
            select(MediaJob)
            .where(MediaJob.platform == platform)
            .order_by(MediaJob.created_at.desc())
            .limit(16)
        ).all()
        # Real project posts have no creative direction, so only AI posts count towards the rotation.
        used = [j.brief["concept"] for j in recent if j.brief.get("concept")][: len(CONCEPTS) - 1]
        concept, direction = next(
            (c for c in CONCEPTS if c[0] not in used), CONCEPTS[sequence % len(CONCEPTS)]
        )
        # Each job is rechecked on its own when it runs, so it lists only its platform: an image job for
        # Instagram mustn't fail TikTok's video rule or Pinterest's real-media rule.
        values = brief.model_dump() | {"platforms": [platform]}
        operation = brief.operation
        repeat = sequence  # Earlier batches of this campaign with the same format, for reference rotation.
        real = None
        # Pinterest takes only real media imported from Google Drive, so it gets a real project every batch.
        drive_only = platform in REAL_MEDIA_ONLY
        if operation in {"real", "mixed"}:
            # A mixed campaign alternates real projects with AI inspiration, so each kind has its own turn.
            turn = sequence // 2 if operation == "mixed" and not drive_only else sequence
            real = real_choice(
                db, brief.model_copy(update={"real_format": "video"}) if video_requested else brief,
                sources, profile, platform, turn, live=live, drive_only=drive_only, keep_order=not campaign,
                library=campaign_media if discover else None, scores=scores,
            )
            if (operation == "real" or drive_only) and not real:
                raise ValueError(no_real_media(platform))
        if operation == "mixed":
            options = ["image", "video"]
            if edit_sources and (not video_requested or signals["edit"]):
                # A Runway re-render of an uploaded video is AI media too.
                options.append("video_edit")
            if platform == "tiktok" or video_requested:
                options = [v for v in options if v != "image"]
            if real and (drive_only or sequence % 2 == 0):
                operation = "real"
            else:
                turn = sequence // 2 if real else sequence
                operation = options[turn % len(options)]
                repeat = turn // len(options)
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
        if operation == "real":
            values = _real_values(db, settings, brief, values, real, platform, profile, batch, style, live=live)
            values["kind"] = content_kind
            result.append(_add_job(db, brief, campaign, batch, platform, values))
            continue
        mime = REFERENCE_TYPES[operation]
        refs = [a.id for a in sources if a.id in brief.reference_ids and a.media["mime"].startswith(mime)]
        if discover:
            if operation in {"video", "video_edit"}:
                # Preserve the campaign's actual library scope for a later repair. Its generation
                # shortlist can be mostly stills even when matching recorded footage exists.
                values["repair_library_scope"] = list(brief.reference_ids)
            if operation == "video_edit":
                refs = [a.id for a in campaign_references(edit_sources, scores, repeat)]
            else:
                refs, originals = campaign_visuals(campaign_media, scores, repeat)
                if refs:
                    values["campaign_reference_ids"] = refs
                if operation == "video" and not brief.shots:
                    values["original_candidates"] = originals
        elif campaign and refs:
            offset = repeat % len(refs)
            refs = refs[offset:] + refs[:offset]
        refs = refs[: 3 if operation in {"image", "audio"} else 1]
        intent = (
            "Invite exploration of the system."
            if content_kind == "promotional"
            else "Editorial design inspiration rather than a sales pitch."
        )
        use_case = ""
        if campaign:
            shown = [j.brief["use_case"] for j in recent if j.brief.get("use_case")][: len(USE_CASES) - 1]
            use_case = next((u for u in USE_CASES if u not in shown), USE_CASES[sequence % len(USE_CASES)])
        values.update(
            operation=operation,
            kind=content_kind,
            reference_ids=refs,
            concept=concept,
            use_case=use_case,
            direction=f"{direction} {intent}"
            + (f" Suggested use case, unless the campaign idea names a setting: {use_case}." if use_case else ""),
            # Visual prompts are written by the worker just before submission (social/prompting.py).
            generation_prompt=utf16_trim(brief.prompt) if operation == "audio" else "",
            website_snapshot=snapshot,
            seed=int(hashlib.sha256((batch + platform).encode()).hexdigest()[:8], 16),
            execution_mode=settings.ai_mode,
            runway_image_model=settings.runway_image_model,
            runway_video_model=settings.runway_video_model,
            runway_music_model=settings.runway_music_model,
            runway_audio_model=settings.runway_audio_model,
        )
        if operation in {"video", "video_edit"}:
            if brief.duration > profile.max_duration:
                raise ValueError(f"{platform}: choose a duration of at most {profile.max_duration:g} seconds")
            if brief.duration < END_CARD_SECONDS + MIN_CLIP_SECONDS:
                raise ValueError(
                    f"Every video ends with the {END_CARD_SECONDS}-second Tudelu logo, so choose a length of at "
                    f"least {END_CARD_SECONDS + MIN_CLIP_SECONDS} seconds"
                )
            # Every video ends with a cut to the logo, and every video has sound: calm ambient music
            # unless the brief describes other music or reworks uploaded audio.
            logo = ending_logo(db, brief, live=live)
            end_card = {"asset_id": logo.id, "seconds": END_CARD_SECONDS}
            values["end_card_asset_id"] = logo.id
            if not brief.audio_prompt.strip() and not brief.audio_reference_ids:
                values.update(audio_prompt=AMBIENT_STYLES[style][1], ambient_style=style)
            # Generated or edited footage; the logo end card is rendered locally after it.
            footage = brief.duration - (END_CARD_SECONDS if end_card else 0)
            maximum = 30 if operation == "video_edit" else video_clip_limit(settings.runway_video_model)
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
                "music" if values["audio_prompt"].strip() and not brief.audio_reference_ids else "seed_audio"
            )
        if operation == "image":
            values.update(audio_prompt="", audio_reference_ids=[], end_card_asset_id="")
        result.append(_add_job(db, brief, campaign, batch, platform, values))
    return group_renders(db, settings, result, platform_profiles)


def no_real_media(platform):
    if platform in REAL_MEDIA_ONLY:
        return (
            f"{platform.title()} only posts real photos and videos imported from Google Drive, and none fit "
            "this post. Import project photos or videos through Asset library → Automatic Google Drive imports, "
            f"or untick {platform.title()}."
        )
    return (
        f"{platform.title()}: no real photos or videos fit this post. Add photos or videos of completed "
        "projects to the asset library, or choose them in the brief."
    )


def check_real_media(db, settings, brief, sources):
    """Before a campaign is saved: every platform that needs real media has some to start from."""
    if brief.operation not in {"real", "mixed"}:
        return
    for platform in brief.platforms:
        drive_only = platform in REAL_MEDIA_ONLY
        if brief.operation == "real" or drive_only:
            profile = profiles(settings)[platform]
            if not real_choice(db, brief, sources, profile, platform, 0, drive_only=drive_only):
                raise ValueError(no_real_media(platform))


def _add_job(db, brief, campaign, batch, platform, values):
    job = MediaJob(
        campaign_id=campaign.id if campaign else None,
        batch_key=batch + ":" + platform,
        platform=platform,
        brief=values,
        state="planned" if brief.workflow == "next" and not campaign else "queued",
    )
    db.add(job)
    return job


def _real_values(db, settings, brief, values, real, platform, profile, batch, style, *, live=False):
    """The job brief of a real project post: candidate media chosen now, the edit planned when it runs."""
    real_format, media_ids, sound_ids = real
    values = values | {
        "operation": "real",
        "real_format": real_format,
        "reference_ids": media_ids,
        "audio_reference_ids": sound_ids,
        "audio_prompt": "",
        "shots": [],
        "concept": "",
        "direction": "",
        "use_case": "",
        "generation_prompt": "",
        "website_snapshot": {},
        "seed": int(hashlib.sha256((batch + platform).encode()).hexdigest()[:8], 16),
        "execution_mode": settings.ai_mode,
        "end_card": None,
        "end_card_asset_id": "",
        "prepared_edit": {},
    }
    try:
        # Sent back by the browser from the plan page, so it's read as untrusted until it validates.
        prepared = RealEditPlan.model_validate(
            {k: v for k, v in brief.prepared_edit.items() if k in RealEditPlan.model_fields}
        ) if brief.prepared_edit else None
    except ValidationError:
        prepared = None
    if (
        prepared
        and prepared.items
        and real_format == brief.real_format
        and {i.asset_id for i in prepared.items} <= set(media_ids)
        and {c.asset_id for c in prepared.audio} <= set(sound_ids)
    ):
        # The plan page's edit fits this platform's media; otherwise the worker plans another.
        values["prepared_edit"] = prepared.model_dump() | {
            k: str(brief.prepared_edit.get(k, ""))[:100] for k in ("planner", "version")
        }
    if real_format == "video":
        if brief.duration > profile.max_duration:
            raise ValueError(f"{platform}: choose a duration of at most {profile.max_duration:g} seconds")
        if brief.duration < END_CARD_SECONDS + MIN_CLIP_SECONDS:
            raise ValueError(
                f"Every video ends with the {END_CARD_SECONDS}-second Tudelu logo, so choose a length of at "
                f"least {END_CARD_SECONDS + MIN_CLIP_SECONDS} seconds"
            )
        logo = ending_logo(db, brief, live=live)
        tracks = music_tracks(db, live=live, public=brief.rights != "not_public")
        values |= {
            "end_card": {"asset_id": logo.id, "seconds": END_CARD_SECONDS},
            "end_card_asset_id": logo.id,
            # Library music for silent footage, the same for every platform of the batch.
            "ambient_track_id": tracks[0].id if tracks else "",
            "fallback_ambient_style": style,
        }
    return values


def _queue_uploaded(db, settings, brief, snapshot, *, live=False):
    """One job per platform that fits the admin's own media to the platform; Runway is never called."""
    batch = uid()
    # A video ends on the logo. The app's ambient music replaces its sound when chosen, and is
    # added anyway when the video turns out to be silent.
    logo = ending_logo(db, brief, live=live) if brief.operation == "video" else None
    style = next_ambient_style(db) if brief.operation == "video" else ""
    tracks = music_tracks(db, live=live, public=brief.rights != "not_public") if brief.operation == "video" else []
    result = []
    platform_profiles = profiles(settings)
    for platform in brief.platforms:
        profile = platform_profiles[platform]
        if brief.operation == "video" and brief.duration > profile.max_duration:
            raise ValueError(f"{platform}: choose a duration of at most {profile.max_duration:g} seconds")
        if brief.operation == "image" and platform == "tiktok":
            raise ValueError("TikTok requires video; untick TikTok to post a photo")
        values = brief.model_dump() | {
            "platforms": [platform],
            "generation_prompt": "",
            "concept": "",
            "direction": "",
            "website_snapshot": snapshot or {},
            "seed": 0,
            "execution_mode": settings.ai_mode,
            "end_card": {"asset_id": logo.id, "seconds": END_CARD_SECONDS} if logo else None,
            "end_card_asset_id": logo.id if logo else "",
            # The same ambient music for every platform; only recorded as used when it is laid.
            "ambient_track_id": tracks[0].id if tracks else "",
            "fallback_ambient_style": style,
        }
        job = MediaJob(
            batch_key=f"{batch}:{platform}",
            platform=platform,
            brief=values,
            state="planned" if brief.workflow == "next" else "queued",
            source_asset_ids=brief.reference_ids + brief.audio_reference_ids,
        )
        db.add(job)
        result.append(job)
    return group_renders(db, settings, result, platform_profiles)


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
    from .campaigns import lock_campaign
    from .media_queue import campaign_wait_reason

    if not can_automate(db, settings) or settings.ai_mode != "live":
        return
    due = db.scalars(select(MediaCampaign.id).where(
        MediaCampaign.enabled.is_(True), MediaCampaign.next_run_at <= now()
    )).all()
    for campaign_id in due:
        if not can_automate(db, settings):
            return
        campaign = lock_campaign(db, campaign_id)
        if not campaign or not campaign.enabled or campaign.next_run_at > now():
            db.commit()
            continue
        brief = CreativeBrief.model_validate(campaign.brief, context={"stored": True})
        if reason := campaign_wait_reason(db, campaign):
            campaign.error = reason
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
        try:
            # Roll back a partial batch on error while keeping the campaign locked until its status is saved.
            with db.begin_nested():
                jobs = queue_jobs(db, settings, brief, campaign=campaign, sequence=campaign.sequence)
            for job in jobs:
                job.brief = job.brief | {k: campaign.brief[k] for k in ("prompted_by", "prompted_by_label") if k in campaign.brief}
            campaign.sequence += 1
            campaign.next_run_at = now() + timedelta(hours=brief.interval_hours)
            campaign.error = ""
            db.commit()
        except WebsiteContextError as exc:
            campaign.error = str(exc)
            if exc.retryable:
                campaign.next_run_at = now() + timedelta(seconds=max(300, exc.retry_after))
                campaign.error += f" Campaign remains enabled; next attempt at {campaign.next_run_at.isoformat()} UTC."
            else:
                campaign.enabled = False
            db.commit()
        except ValueError as exc:
            campaign.error = str(exc)
            campaign.enabled = False
            db.commit()


def _save_output(db, settings, job, data):
    from .reference_grounding import current_plan

    if job.brief.get("active_generation") == "scene":
        from .scene_generation import save_scene

        save_scene(db, settings, job, data)
        return
    if job.brief.get("active_generation") == "repair_keyframe":
        from .video_repair import save_keyframe
        save_keyframe(db, settings, job, data)
        return
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
    if job.brief.get("active_generation") == "repair_edit":
        model = "aleph2"
    if not is_audio and job.brief.get("active_generation") == "repair_edit" and segment:
        from .video_repair import source_for_repair
        source_for_repair(db, job, segment["repair"])
    reference_plan = current_plan(job) if not is_audio else None
    reference_ids = ([r["asset_id"] for r in reference_plan["references"] if r.get("asset_id")]
                     if reference_plan else brief["reference_ids"])
    if reference_plan and reference_plan.get("scene_reference"):
        reference_ids += [reference_plan["scene_reference"]["asset_id"]]
    if segment and (repair := segment.get("repair")) and repair["route"] == "aleph":
        reference_ids.append(repair["source_id"])
        if repair.get("keyframe"):
            reference_ids.append(repair["keyframe"]["asset_id"])
    refs = [db.get(Asset, i) for i in dict.fromkeys(reference_ids + brief["audio_reference_ids"])]
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
            **({"reference_plan": reference_plan} if reference_plan else {}),
            "media_job_id": job.id,
            "website_snapshot": brief["website_snapshot"],
            "creative_brief": {k: brief[k] for k in ("prompt", "concept", "kind")}
            | {"system": (segment or {}).get("system") or brief.get("depicted_system", "")},
            "generated_audio": is_audio,
            "audio_prompt": brief["audio_prompt"],
            **({"audio_fallback": brief["audio_fallback"]} if is_audio and brief.get("audio_fallback") else {}),
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


def _uploaded_copy(db, settings, job, first, originals):
    """The platform copy of the admin's own photo or video, with an uploaded soundtrack and logo card."""
    from .gate import media_errors

    brief = job.brief
    profile = profiles(settings)[job.platform]
    sound = next((a for a in originals if a.media["mime"].startswith("audio/")), None)
    soundtrack = None
    if sound:
        soundtrack = storage(settings).read(sound.storage_key)
        if hashlib.sha256(soundtrack).hexdigest() != sound.checksum:
            raise ValueError("Soundtrack integrity failed before creating the video")
    video = brief["operation"] == "video"
    end_card = brief.get("end_card") if video else None
    logo = db.get(Asset, end_card["asset_id"]) if end_card else None
    if end_card and not logo:
        raise ValueError("The end-card logo is missing from the asset library; upload it and create the post again")
    duration = brief["duration"]
    live = settings.wants_live(job.platform, db.get(Account, job.platform))
    if video and not logo:
        # Planned before every video ended on the logo: the card follows the planned footage.
        logo = default_logo(db, live=live)
        if not logo:
            raise ValueError(NO_LOGO)
        end_card = {"asset_id": logo.id, "seconds": END_CARD_SECONDS}
        duration += END_CARD_SECONDS
    data, extension, sound_tags = fitted_copy(
        settings,
        first,
        profile,
        job.platform,
        duration=duration,
        soundtrack=soundtrack,
        logo=logo,
        card_seconds=end_card["seconds"] if end_card else 0,
        ambient=real_ambient(
            db,
            live=live,
            public=brief["rights"] != "not_public",
            track_id=brief.get("ambient_track_id", ""),
            style=brief.get("fallback_ambient_style", ""),
        )
        if video
        else None,
        replace_sound=bool(brief.get("ambient_music")),
        seed=seed_for(job.batch_key.rsplit(":", 1)[0]),
    )
    music = music_asset(db, sound_tags)
    used = [a for a in [first, sound, logo, music] if a]
    end_card_tag = {
        "asset_id": logo.id,
        "checksum": logo.checksum,
        "seconds": end_card["seconds"],
        "note": "The final seconds show Tudelu's official logo, supplied as company media and added by the app.",
    } if logo else None
    result = ingest(
        db,
        settings,
        data,
        f"{job.platform}-{job.id}.{extension}",
        source=first.source,
        rights=combined_rights(first.rights, [a for a in [sound, logo, music] if a]),
        owner=first.owner,
        rights_expire_at=min([a.rights_expire_at for a in used if a.rights_expire_at], default=None),
        customer_permission=first.customer_permission,
        permission_reference=first.permission_reference,
        demo_only=any(a.demo_only for a in used),
        tags=first.tags
        | {
            "source_asset_ids": [a.id for a in used],
            "rendition": {"platform": job.platform, "original_asset_id": first.id},
            "media_job_id": job.id,
            "as_uploaded": True,
            "generated_audio": False,
            "creative_brief": {"prompt": brief["prompt"], "kind": brief["kind"], "concept": ""},
            **({"soundtrack_asset_id": sound.id} if sound else {}),
            **({"end_card": end_card_tag} if end_card_tag else {}),
            **sound_tags,
        },
    )
    errors = media_errors([result], profile, job.platform)
    if errors:
        raise ValueError(f"Your media doesn't fit {job.platform.title()}: " + ", ".join(errors))
    return result


def _finish(db, settings, job):
    from .visual_context import ensure_visual_summary

    brief = job.brief
    originals = [db.get(Asset, i) for i in job.source_asset_ids]
    # A redone single take is saved after the reused music, so pick the first picture, not the first source.
    first = next((a for a in originals if not a.media["mime"].startswith("audio/")), originals[0])
    summary_settings = settings.model_copy(update={"ai_mode": brief["execution_mode"]})
    for original in originals:
        ensure_visual_summary(db, summary_settings, original)
        db.commit()
    if job.output_asset_id:
        result = db.get(Asset, job.output_asset_id)
    elif brief["operation"] == "audio":
        result = first
    elif brief.get("as_uploaded"):
        result = _uploaded_copy(db, settings, job, first, originals)
    elif brief["operation"] == "real":
        from .real_edit import real_copy

        result = real_copy(db, settings, job)
    else:
        profile = profiles(settings)[job.platform]
        end_card = brief.get("end_card") if brief["operation"] != "image" else None
        logo = db.get(Asset, end_card["asset_id"]) if end_card else None
        if end_card and not logo:
            raise ValueError("The end-card logo is missing from the asset library; upload it and create the post again")
        sound_tags = {}
        inserts = []
        if brief["operation"] == "image":
            data = storage(settings).read(first.storage_key)
            if hashlib.sha256(data).hexdigest() != first.checksum:
                raise ValueError("Source integrity failed before creating the publishing rendition")
            data = image_rendition(data, profile.image_width, profile.image_height,
                                   source=first.source, generator=first.generator)
            name = f"{job.platform}-{job.id}.jpg"
        else:
            inserts = original_shots(db, brief)
            visuals = [a for a in originals if not a.media["mime"].startswith("audio/")]
            with source_files(settings, visuals + inserts) as files:
                sound_asset = next((a for a in originals if a.media["mime"].startswith("audio/")), None)
                soundtrack = storage(settings).read(sound_asset.storage_key) if sound_asset else None
                if not logo:
                    # Planned before every video ended on the logo: the card is added after the footage.
                    logo = default_logo(db, live=settings.wants_live(job.platform, db.get(Account, job.platform)))
                    if not logo:
                        raise ValueError(NO_LOGO)
                    end_card = {"asset_id": logo.id, "seconds": END_CARD_SECONDS}
                if brief.get("video_segments"):
                    clips = [sequence_clip(db, segment, files, profile) for segment in brief["video_segments"]]
                else:
                    clips = [(files[first.id], 0, brief["duration"])]
                excess = sum(c[2] for c in clips) + end_card["seconds"] - profile.max_duration
                if excess > 0:
                    clips[-1] = clips[-1][:2] + (max(MIN_CLIP_SECONDS, clips[-1][2] - excess),)
                sound_tags = {"video_sound": "music" if sound_asset else "own"}
                if soundtrack is not None:
                    if hashlib.sha256(soundtrack).hexdigest() != sound_asset.checksum:
                        raise ValueError("Soundtrack integrity failed before assembly")
                    if brief.get("audio_fallback"):
                        # Seed Audio has no duration parameter. Keep its original bytes and fit only
                        # the publishing copy, including the logo ending, before full-stream audio review.
                        seconds = sum(c[2] for c in clips) + end_card["seconds"]
                        soundtrack = fit_track(soundtrack, seconds)
                        sound_tags.update(audio_fallback=brief["audio_fallback"], soundtrack_fit={
                            "asset_id": sound_asset.id, "checksum": sound_asset.checksum,
                            "generator": sound_asset.generator, "source_seconds": sound_asset.media["duration"],
                            "seconds": seconds, "looped": sound_asset.media["duration"] < seconds,
                            "crossfade_seconds": 2.0, "fade_out_seconds": 1.5,
                        })
                if soundtrack is None and not any(has_sound(c[0]) for c in clips):
                    # Planned before every video had sound: Runway composes calm ambient music first,
                    # then the video is assembled with it.
                    style = next_ambient_style(db)
                    job.brief = job.brief | {
                        "audio_prompt": AMBIENT_STYLES[style][1],
                        "ambient_style": style,
                        "soundtrack_model": "music",
                        "soundtrack_asset_id": "",
                    }
                    job.stage, job.state = "audio", "queued"
                    db.commit()
                    return
                logo_data = storage(settings).read(logo.storage_key)
                if hashlib.sha256(logo_data).hexdigest() != logo.checksum:
                    raise ValueError("Logo integrity failed before creating the end card")
                card = logo_card(logo_data, profile.video_width, profile.video_height, end_card["seconds"])
                clips.append((card, 0, end_card["seconds"]))
                data = video_sequence(clips, profile.video_width, profile.video_height, soundtrack,
                                      require_full_duration=bool(brief.get("video_segments")),
                                      playback_rates=[s.get("playback_rate", 1) for s in brief["video_segments"]] + [1]
                                      if brief.get("video_segments") else None)
                name = f"{job.platform}-{job.id}.mp4"
        # A multi-clip video records every clip's prompt, not only the first clip's.
        clip_prompts = [f"Clip {s['index'] + 1}: {s['prompt']}" for s in brief.get("video_segments", []) if s.get("prompt")]
        used_media = originals + inserts + ([logo] if logo else [])
        source_ids = brief["reference_ids"] + brief.get("grounded_reference_ids", []) + brief["audio_reference_ids"]
        if brief.get("repair_routes"):
            # The output clips carry their own ancestry. A discarded scene isn't an input to a library cut.
            source_ids = []
        source_ids += [a.id for a in used_media]
        source = "ai_edited" if inserts else first.source
        generator = first.generator
        prompt = " ".join(clip_prompts) if len(clip_prompts) > 1 or (inserts and clip_prompts) else first.prompt
        if inserts:
            ai_inputs = [a for a in used_media if requires_ai_review(a)]
            source = "ai_edited" if ai_inputs else "company"
            generator = first.generator or next((a.generator for a in ai_inputs if a.generator), None)
            prompt = (prompt or brief["prompt"]) if ai_inputs else None
        result = ingest(
            db,
            settings,
            data,
            name,
            source=source,
            generator=generator,
            prompt=prompt,
            rights=combined_rights(first.rights, used_media),
            owner=first.owner,
            rights_expire_at=min(
                [a.rights_expire_at for a in used_media if a.rights_expire_at], default=None
            ),
            demo_only=any(a.demo_only for a in used_media),
            tags=first.tags
            | {
                "source_asset_ids": list(dict.fromkeys(source_ids)),
                "rendition": {"platform": job.platform, "original_asset_id": first.id},
                "media_job_id": job.id,
                **({"replaces": brief["replaces"]} if brief.get("replaces") else {}),
                **({"repair_routes": brief["repair_routes"],
                    "creative_brief": {k: brief.get(k, "") for k in ("prompt", "concept", "kind")},
                    **({"asset_type": "ai_concept"} if source in {"ai_generated", "ai_edited"} else {})}
                   if brief.get("repair_routes") else {}),
                "generated_audio": any(a.media["mime"].startswith("audio/") and requires_ai_review(a) for a in originals),
                **({"video_sequence": brief["video_segments"], "requested_duration": brief["duration"]}
                   if brief.get("video_segments") else {}),
                **({"end_card": {
                    "asset_id": logo.id,
                    "checksum": logo.checksum,
                    "seconds": end_card["seconds"],
                    "note": "The final seconds show Tudelu's official logo, supplied as company media and "
                    "added by the app after generation.",
                }} if logo else {}),
                **sound_tags,
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
    _complete_job(db, job, result)


def _complete_job(db, job, result):
    brief = job.brief
    job.output_asset_id = result.id
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
            human_review_required=not bool(job.campaign_id),
        )
        post.prompted_by = brief.get("prompted_by")
        post.prompted_by_label = brief.get("prompted_by_label")
        job.post_id = post.id
        replaced = brief.get("replaces")
        held = db.get(Post, replaced["post_id"]) if replaced else None
        if held and held.status == "held":
            if replaced.get("request"):
                reason = f"Replaced by post {post.id} with the media changes requested: {replaced['request'][:300]}"
            else:
                reason = (
                    f"Replaced by post {post.id} after "
                    f"{replaced.get('label') or 'clip ' + str(replaced['clip_index'] + 1)} "
                    + ("were" if replaced.get("count", 1) > 1 else "was")
                    + " regenerated"
                    + (" automatically" if replaced.get("auto_attempt") else "")
                )
            transition(db, held, "failed", reason, replaced.get("requested_by") or "admin")
    job.state, job.error = "complete", ""
    db.commit()


def _reuse_render(db, settings, job, source):
    claimed = db.execute(update(MediaJob).where(MediaJob.id == job.id, MediaJob.state == "queued")
                         .values(state="rendering"))
    if not claimed.rowcount:
        db.rollback()
        return
    if match := compatible_render(db, job, source, profiles(settings)[job.platform]):
        renderer, asset = match
        job.brief = job.brief | {"shared_render_from": renderer.id, "shared_render_platform": renderer.platform}
        _complete_job(db, job, asset)
        return
    # The completed file doesn't fit this destination. Assemble its local rendition from the
    # same original clips, edit decisions and music without sending another provider request.
    job.brief = {k: v for k, v in source.brief.items()
                 if k not in {"usage_checked", "failed_tasks", "active_generation", "active_model",
                              "active_estimate", "runway_retry_at"}} | {"platforms": [job.platform]}
    job.source_asset_ids = list(source.source_asset_ids)
    db.commit()
    _finish(db, settings, job)


def fitted_copy(
    settings,
    asset,
    profile,
    platform,
    *,
    duration=None,
    soundtrack=None,
    logo=None,
    card_seconds=0,
    ambient="",
    replace_sound=False,
    seed=0,
):
    """Bytes, file extension and sound tags of a platform copy of real media, keeping its own shape.

    Media too small for the platform is padded to the platform's size instead. A video ends on the
    logo card when one is given. It keeps its own sound unless a soundtrack is given. The `ambient`
    music (see real_ambient) replaces it when `replace_sound` is set or the video is silent.
    """
    data = storage(settings).read(asset.storage_key)
    if hashlib.sha256(data).hexdigest() != asset.checksum:
        raise ValueError(f"{asset.original_name} changed in storage; upload it again")
    media = asset.media
    if media["mime"].startswith("image/"):
        size = fitted_size(
            media["width"], media["height"], profile.image_width, profile.image_height,
            profile.image_min_ratio, profile.image_max_ratio,
        )
        if size[0] < profile.min_width or size[1] < profile.min_height:
            size = (profile.image_width, profile.image_height)
        return image_rendition(data, *size, source=asset.source, generator=asset.generator), "jpg", {}
    size = fitted_size(
        media["width"], media["height"], profile.video_width, profile.video_height,
        profile.video_min_ratio, profile.video_max_ratio,
    )
    # Facebook Reels need at least 540x960 in either orientation.
    small = size[0] < profile.min_width or size[1] < profile.min_height or (
        platform == "facebook" and (min(size) < 540 or max(size) < 960)
    )
    if small:
        size = (profile.video_width, profile.video_height)
    # The requested length includes the logo card; the footage never runs past the video's own end.
    card = card_seconds if logo else 0
    total = min(duration or media.get("duration", 0) + card, profile.max_duration)
    footage = min(total - card, media.get("duration", 0))
    sound_tags = {"video_sound": "upload" if soundtrack is not None else "own"}
    if soundtrack is None and ambient and (replace_sound or not has_sound(data)):
        soundtrack, sound_tags = ambient_sound(settings, ambient, footage + card, seed)
    if logo is None:
        return video_rendition(data, *size, footage, soundtrack), "mp4", sound_tags
    logo_data = storage(settings).read(logo.storage_key)
    if hashlib.sha256(logo_data).hexdigest() != logo.checksum:
        raise ValueError("Logo integrity failed before creating the end card")
    ending = logo_card(logo_data, *size, card)
    return video_sequence([(data, 0, footage), (ending, 0, card)], *size, soundtrack), "mp4", sound_tags


def real_ambient(db, *, live=False, public=True, track_id="", style=""):
    """Ambient music for a real video: a library track (the chosen one if still usable, otherwise the
    least recently used), or the app's own music in `style` when the library has none."""
    tracks = music_tracks(db, live=live, public=public)
    track = next((t for t in tracks if t.id == track_id), None) or (tracks[0] if tracks else None)
    return {"track": track, "style": style or next_ambient_style(db)}


def ambient_sound(settings, ambient, seconds, seed=0):
    """Soundtrack bytes and copy tags for the ambient music a real video gets."""
    track = ambient.get("track")
    if track:
        data = storage(settings).read(track.storage_key)
        if hashlib.sha256(data).hexdigest() != track.checksum:
            raise ValueError(f"{track.original_name} changed in storage; upload it again")
        return fit_track(data, seconds), {"video_sound": "ambient", "ambient_music": track_tag(track, seconds)}
    style = ambient["style"]
    return ambient_track(style, seconds, seed), {
        "video_sound": "ambient",
        "ambient_music": ambient_tag(style, seconds),
    }


def music_asset(db, sound_tags):
    """The library track a copy's ambient music came from, if any."""
    track_id = (sound_tags.get("ambient_music") or {}).get("asset_id")
    return db.get(Asset, track_id) if track_id else None


def platform_copy(db, settings, asset, platform):
    """Use real media as it is when the platform accepts it, otherwise a resized copy linked to it.

    A video always gets sound and the logo ending as well (see finish_video).
    """
    from .gate import media_errors

    if asset.media["mime"].startswith("video/"):
        return finish_video(db, settings, asset, platform)
    profile = profiles(settings)[platform]
    if not media_errors([asset], profile, platform):
        return asset
    data, extension, _ = fitted_copy(settings, asset, profile, platform)
    copy = ingest(
        db,
        settings,
        data,
        f"{platform}-{asset.id}.{extension}",
        source=asset.source,
        rights=asset.rights,
        owner=asset.owner,
        rights_expire_at=asset.rights_expire_at,
        customer_permission=asset.customer_permission,
        permission_reference=asset.permission_reference,
        demo_only=asset.demo_only,
        tags=asset.tags
        | {"source_asset_ids": [asset.id], "rendition": {"platform": platform, "original_asset_id": asset.id}},
    )
    errors = media_errors([copy], profile, platform)
    if errors:
        raise ValueError(f"{asset.original_name} can't be fitted for {platform.title()}: " + ", ".join(errors))
    return copy


def finish_video(db, settings, asset, platform):
    """A video that follows the house rules: it has sound and ends with a cut to the Tudelu logo.

    Returns the video itself when it already does and fits the platform, otherwise a copy linked to
    it. The copy keeps the video's shape and its own sound; a silent video gets the app's ambient
    music. Nothing is sent to Runway, so real media stays real. Images are returned unchanged.
    """
    from .gate import media_errors
    from .idea_plan import _platform_names
    from .video_rules import video_rule_errors

    if not asset.media["mime"].startswith("video/"):
        return asset
    profile = profiles(settings)[platform]
    if not video_rule_errors([asset]) and not media_errors([asset], profile, platform):
        return asset
    name = _platform_names([platform])
    ended = bool((asset.tags or {}).get("end_card"))
    logo = None if ended else default_logo(db, live=settings.wants_live(platform, db.get(Account, platform)))
    if not ended and not logo:
        raise ValueError(NO_LOGO)
    card = 0 if ended else END_CARD_SECONDS
    length = asset.media.get("duration", 0)
    if length + card > profile.max_duration + 0.1:
        limit = (
            f"With the {card}-second logo ending, videos for {name} can be at most "
            f"{profile.max_duration - card:g} seconds."
            if card
            else f"Videos for {name} can be at most {profile.max_duration:g} seconds."
        )
        raise ValueError(
            f"{asset.original_name} is {length:.0f} seconds long. {limit} Trim it, import it again and "
            "create the post from the shorter video."
        )
    live = settings.wants_live(platform, db.get(Account, platform))
    data, extension, sound_tags = fitted_copy(
        settings,
        asset,
        profile,
        platform,
        logo=logo,
        card_seconds=card,
        ambient=real_ambient(db, live=live, public=asset.rights != "not_public"),
        seed=seed_for(asset.id, platform),
    )
    music = music_asset(db, sound_tags)
    tags = (asset.tags or {}) | {
        "source_asset_ids": [asset.id] + [a.id for a in (logo, music) if a],
        "rendition": {"platform": platform, "original_asset_id": asset.id},
        **sound_tags,
    }
    if logo:
        tags["end_card"] = {
            "asset_id": logo.id,
            "checksum": logo.checksum,
            "seconds": card,
            "note": "The final seconds show Tudelu's official logo, supplied as company media and added by the app.",
        }
    used = [a for a in (asset, logo, music) if a]
    copy = ingest(
        db,
        settings,
        data,
        f"{platform}-{asset.id}.{extension}",
        source=asset.source,
        generator=asset.generator,
        prompt=asset.prompt,
        rights=combined_rights(asset.rights, used[1:]) if used[1:] else asset.rights,
        owner=asset.owner,
        rights_expire_at=min([a.rights_expire_at for a in used if a.rights_expire_at], default=None),
        customer_permission=asset.customer_permission,
        permission_reference=asset.permission_reference,
        demo_only=any(a.demo_only for a in used),
        tags=tags,
    )
    errors = media_errors([copy], profile, platform)
    if errors:
        raise ValueError(f"{asset.original_name} can't be fitted for {name}: " + ", ".join(errors))
    return copy


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
    runway = provider or Runway(settings)
    configured = settings
    from . import usage

    try:
        for job in runnable_jobs(db, settings.media_jobs_per_tick, now()):
            # The previous job has committed; save its recorded requests before starting this one.
            usage.flush(db)
            usage.attribute(media_job_id=job.id, platform=job.platform, post_id=job.post_id)
            settings = job_settings(configured, job.brief)
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
                        estimate = (task.get("estimatedCost") or {}).get("credits")
                        if estimate is not None and job.brief.get("active_estimate") != estimate:
                            # Kept in case the finished task doesn't report its final cost.
                            job.brief = job.brief | {"active_estimate": estimate}
                            db.commit()
                        continue
                    usage.record_runway_task(job, task)
                    if task.get("status") in {"FAILED", "CANCELED", "CANCELLED"}:
                        # Kept as failed with its task ID; the In progress page offers a new submission.
                        raise ValueError(runway_failure(job, task))
                    if task.get("status") != "SUCCEEDED" or not task.get("output"):
                        raise ValueError("Runway returned an invalid task result")
                    _save_output(db, settings, job, runway.download(task["output"][0]))
                    continue
                brief = CreativeBrief.model_validate(
                    {k: v for k, v in job.brief.items() if k in CreativeBrief.model_fields},
                    context={"stored": True},
                )
                validate_sources(db, brief, live=settings.wants_live(job.platform, db.get(Account, job.platform)))
                if source_id := job.brief.get("shared_render_from"):
                    _reuse_render(db, settings, job, db.get(MediaJob, source_id))
                    continue
                if brief.as_uploaded or brief.operation == "real":
                    if brief.operation == "real" and "real_edit" not in job.brief:
                        from .real_edit import plan_edit

                        # The media and edit values are saved before rendering; a resume reuses them.
                        plan_edit(db, settings, job)
                    job.state = "rendering"
                    db.commit()
                    _finish(db, settings, job)
                    continue
                if needs_prompt(job):
                    # Written once and saved before any paid request; resume reuses it.
                    job.brief = job.brief | write_prompt(db, settings, job)
                    db.commit()
                if job.brief.get("original_candidates"):
                    original_shots(db, job.brief)
                save_segment_prompt(db, job)
                from .product_rules import enforce_job_prompt

                enforce_job_prompt(db, job)
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
                from types import SimpleNamespace

                from .reference_grounding import prepare as prepare_references
                from .scene_generation import generation_inputs
                from .video_repair import repair_payload

                targeted = repair_payload(db, settings, job, runway)
                if targeted:
                    endpoint, payload, phase = targeted
                else:
                    grounded_references = prepare_references(db, settings, job)
                    generation_brief, grounded_references, phase = generation_inputs(db, settings, job, grounded_references)
                    ids = brief.audio_reference_ids if job.stage == "audio" else brief.reference_ids
                    segment = current_segment(job) if job.stage == "visual" else None
                    if job.stage == "audio" and job.brief.get("soundtrack_model") == "music":
                        ids = []
                    if brief.operation == "audio":
                        ids = list(dict.fromkeys(ids + brief.audio_reference_ids))[:3]
                    style_uri = None
                    if grounded_references is not None:
                        references = [
                            runway.reference(SimpleNamespace(**r))
                            for r in grounded_references
                            if r.get("role") != "style"
                        ]
                        style_uri = next(
                            (runway.reference(SimpleNamespace(**r)) for r in grounded_references if r.get("role") == "style"),
                            None,
                        )
                    elif job.stage == "visual" and brief.operation == "video_edit" and current_segment(job):
                        # Never send the unsplit original to Runway for a segmented edit.
                        references = [runway.reference(edit_reference(db, settings, job))]
                    else:
                        references = [runway.reference(db.get(Asset, i)) for i in ids]
                    endpoint, payload = payload_for(
                        settings, generation_brief, job.platform, references, audio=job.stage == "audio", style=style_uri
                    )
                db.refresh(control)
                db.refresh(account)
                if control.killed or account.paused:
                    continue
                job.brief = job.brief | {
                    "active_generation": phase, "active_model": payload.get("model", ""), "active_estimate": None,
                }
                job.brief = {k: v for k, v in job.brief.items() if k != "runway_retry_at"}
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
                from .recovery import auto_retry

                # A scene the quality review rejected is corrected and made again without waiting.
                auto_retry(db, settings, job)
    finally:
        usage.attribute()
        usage.flush(db)
        if provider is None:
            runway.close()


def resume_job(job):
    if job.state not in {"failed", "uncertain"}:
        raise ValueError("Only failed or uncertain jobs can be resumed")
    if job.brief.get("render_peers"):
        job.state, job.error = "queued", ""
        return
    from .reference_grounding import current_plan

    plan = current_plan(job) or {}
    if not job.task_id and plan.get("scene_reference") and plan.get("scene_check", {}).get("passed") is False:
        raise ValueError("This scene was rejected. Use Generate corrected scene so its review findings are addressed.")
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
