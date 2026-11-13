"""Turn an admin's free-text idea into a reviewable media plan before anything is generated.

The plan sets the format (image, video or video edit), the finished length, the clips (a setting
and a Tudelu system for each), the soundtrack direction and an optional logo end card. Explicit
signals in the idea, such as "30 second reel", "music" or "logo at the end", always win over a
model's reading, so an idea that asks for video can no longer become a single image by default.
"""

import json
import math
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from .db import now
from .facts import Facts, active
from .models import FactSheet
from .provider_errors import ProviderError

END_CARD_SECONDS = 3
MAX_CLIPS = 9
# Runway gen4.5 generates 2-10 second clips; longer videos join several clips.
MIN_CLIP_SECONDS = 2
MAX_CLIP_SECONDS = 10

FORMATS = {
    "auto": "Let the idea decide",
    "image": "Image",
    "video": "Video",
    "video_edit": "Edit an uploaded video",
}
STYLES = {
    "auto": "Let the idea decide",
    "promotional": "Promotional",
    "non_promotional": "Editorial / non-promotional",
}

_VIDEO = re.compile(
    r"\b(videos?|clips?|reels?|footage|films?|montage|animat\w*|time-?lapse|b-roll|slow[- ]?motion|"
    r"tiktoks?|cinematic series|fly-?through|walk-?through)\b",
    re.I,
)
_IMAGE = re.compile(r"\b(photos?|photographs?|images?|pictures?|stills?|posters?|graphics?)\b", re.I)
_SOUND = re.compile(
    r"\b(music|musical|soundtrack|song|melody|instrumental|piano|guitar|strings|beats?|audio|"
    r"ambient (?:sound|track|audio)s?|sound ?bed)\b",
    re.I,
)
_NEGATED = re.compile(r"\b(no|without|silent|mute|muted)\b(\s+\w+){0,2}\s*$", re.I)
_VOCALS = re.compile(r"\b(vocals?|singing|sung|lyrics|voice-?over|narration|narrator|choir)\b", re.I)
_LOGO = re.compile(r"\b(logo|end ?card|end slate|brand(?:ing)? (?:card|slate|sting))\b", re.I)
_EDIT = re.compile(r"\b(edit|re-?edit|recut|re-?cut|trim|restyle|rework|transform)\b", re.I)
_ALL_PRODUCTS = re.compile(
    r"\b(all|every|each|whole|full|entire)\b[\w\s]{0,30}?\b(products?|systems?|offerings?|range|line-?up|"
    r"solutions?|catalog(?:ue)?)\b|\bline-?up\b",
    re.I,
)
_EDITORIAL = re.compile(
    r"\b(editorial|inspiration|inspiring|behind the scenes|educational|how-to|tips?|non-?promotional)\b", re.I
)
_SECONDS = re.compile(r"(?<![\d.])(\d{1,3})\s*-?\s*(?:s|secs?|seconds?)\b", re.I)
_MINUTES = re.compile(r"(?<![\d.])(\d{1,2})\s*-?\s*(?:mins?|minutes?)\b", re.I)
_LEADING_VERB = re.compile(
    r"^(please\s+)?(create|make|generate|produce|build|design|show|film|shoot|render|give me|i want|we want|"
    r"we need|i need)\s+(a|an|the|some)?\s*",
    re.I,
)
_ARTICLE = re.compile(r"^(a|an|the|in|at|on|by|near|inside|outside)\s+", re.I)


def _text(value, limit):
    return " ".join(str(value or "").split())[:limit]


class Shot(BaseModel):
    setting: str = Field(default="", max_length=160)
    system: str = Field(default="", max_length=120)

    @field_validator("setting", mode="before")
    @classmethod
    def clean_setting(cls, value):
        return _text(value, 160)

    @field_validator("system", mode="before")
    @classmethod
    def clean_system(cls, value):
        return _text(value, 120)


class IdeaPlan(BaseModel):
    title: str = Field(default="", max_length=200)
    operation: Literal["image", "video", "video_edit"] = "image"
    duration: int = Field(default=10, ge=3, le=90)
    kind: Literal["promotional", "non_promotional"] = "promotional"
    shots: list[Shot] = Field(default_factory=list, max_length=30)
    soundtrack: str = Field(default="", max_length=1800)
    end_card: bool = False
    available_systems: list[str] = Field(default_factory=list, max_length=60)
    notes: list[str] = Field(default_factory=list, max_length=20)
    planner: str = ""

    @field_validator("title", mode="before")
    @classmethod
    def clean_title(cls, value):
        return _text(value, 200)

    @field_validator("duration", mode="before")
    @classmethod
    def clamp_duration(cls, value):
        return max(3, min(90, int(value))) if isinstance(value, int | float) else value

    @field_validator("notes", "available_systems", mode="before")
    @classmethod
    def clean_lists(cls, value):
        return [_text(v, 200) for v in value if _text(v, 200)][:20] if isinstance(value, list) else value


PLANNER_INSTRUCTIONS = (
    "You plan one Tudelu social media post from an admin's idea before any media is generated. "
    'Return only JSON {"title":"...","operation":"image|video|video_edit","duration":30,'
    '"kind":"promotional|non_promotional","shots":[{"setting":"...","system":"..."}],"soundtrack":"...",'
    '"end_card":false,"available_systems":["..."],"notes":["..."]}. '
    "operation: use requested_format unless it is auto. For auto, choose video when the idea asks for motion, "
    "clips, a reel, footage, music or a length in seconds; video_edit only when has_video_reference is true and "
    "the idea asks to edit that video; otherwise image. "
    "duration: the finished length in whole seconds (3-90) for video, including any end card. Use the length "
    "stated in the idea; otherwise about 6 seconds per clip, or 10 for a single shot. "
    "shots: for video, one entry per clip in viewing order, at most max_clips. Each clip lasts at least "
    "2 seconds. For an image, exactly one entry. setting is a short concrete place with time of day and mood, "
    "for example 'residential backyard in soft morning light'. system is a Tudelu system copied exactly as "
    "named in catalog (website, products or facts). Never invent a system and never pick one labeled "
    "'Coming soon' unless the idea names it. When the idea asks for all or several products, give each clip a "
    "different available system. Use an empty system to let the later prompt writer choose. "
    "soundtrack: a music direction (mood, instruments, tempo; no artist or song names) only when the idea asks "
    "for music or sound; otherwise empty. "
    "end_card: true when the idea asks for the logo or brand at the end of a video. The generator never draws "
    "logos; the app appends the official uploaded logo as an end card. "
    "kind: use requested_style unless it is auto; introductions, launches and offers are promotional. "
    "available_systems: every system the catalog shows as currently offered (not 'Coming soon'). "
    "title: a short internal post name, at most 60 characters. "
    "notes: 2-6 short plain-language notes on how you read the idea, for example "
    "\"'30 second reel' -> a 30-second vertical video\"."
)


def _clauses(text):
    return [c.strip() for c in re.split(r"[.;:!?\n]+", text) if c.strip()]


def _list_items(clause):
    items = [i.strip(" -'\"") for i in re.split(r",|\band\b|\bor\b|/", clause)]
    items = [
        _ARTICLE.sub("", i).strip()
        for i in items
        if i and not re.fullmatch(r"etc\.?|and so on|more", i, re.I)
    ]
    return [i for i in items if i]


def _name_pattern(key):
    """Whole-word match for a catalog name in singular or plural form."""
    stem = key[:-1] if key.endswith("s") and not key.endswith("ss") else key
    return re.compile(r"(?<!\w)" + re.escape(stem) + r"s?(?!\w)")


def read_signals(idea, catalog=()):
    """Deterministic readings of explicit requests in the idea."""
    text = " ".join(idea.split())
    seconds = None
    if match := _SECONDS.search(text):
        seconds = int(match.group(1))
    elif match := _MINUTES.search(text):
        seconds = int(match.group(1)) * 60
    elif re.search(r"\bhalf a minute\b", text, re.I):
        seconds = 30
    elif re.search(r"\b(a|one)[- ]minute\b", text, re.I):
        seconds = 60
    sound, silent = "", False
    for clause in _clauses(text):
        if not (found := _SOUND.search(clause)):
            continue
        if _NEGATED.search(clause[: found.start()]):
            silent = True
            continue
        # Keep the music direction itself, not the surrounding request.
        prefix = clause[: found.start()].split(",")[-1]
        prefix = re.split(r"\b(?:with|and|plus|over|under|to)\b", prefix, flags=re.I)[-1]
        sound = (prefix + clause[found.start() :]).strip(" ,")
        break
    names = {n.casefold(): n for n in catalog if n}
    patterns = {key: _name_pattern(key) for key in names}
    # Catalog systems named anywhere in the idea, in the order they appear.
    found = sorted((m.start(), names[k]) for k, p in patterns.items() if (m := p.search(text.casefold())))
    systems = [name for _, name in found]
    settings = []
    for clause in _clauses(text):
        items = _list_items(clause)
        if len(items) < 2 or any(len(i.split()) > 4 for i in items):
            continue
        settings += [i for i in items if not any(p.fullmatch(i.casefold()) for p in patterns.values())]
    return {
        "video": bool(_VIDEO.search(text)) or seconds is not None or bool(sound),
        "image": bool(_IMAGE.search(text)),
        "edit": bool(_EDIT.search(text)),
        "seconds": None if seconds is None else max(3, min(90, seconds)),
        "seconds_requested": seconds,
        "soundtrack": sound[:400],
        "silent": silent,
        "vocals": bool(_VOCALS.search(text)),
        "end_card": bool(_LOGO.search(text)),
        "all_products": bool(_ALL_PRODUCTS.search(text)),
        "editorial": bool(_EDITORIAL.search(text)),
        "settings": list(dict.fromkeys(settings))[:MAX_CLIPS],
        "systems": list(dict.fromkeys(systems))[:MAX_CLIPS],
    }


def default_title(idea):
    first = _clauses(idea)[0] if _clauses(idea) else idea
    first = _LEADING_VERB.sub("", " ".join(first.split()))
    first = first[:1].upper() + first[1:]
    if len(first) <= 60:
        return first or "Post idea"
    return first[:60].rsplit(" ", 1)[0].rstrip(",;") + "…"


def clip_limits(duration, end_card):
    clip_seconds = duration - (END_CARD_SECONDS if end_card else 0)
    return clip_seconds, max(1, min(MAX_CLIPS, clip_seconds // MIN_CLIP_SECONDS))


def catalog_for(db, facts_id, snapshot, product):
    sheet = db.get(FactSheet, facts_id)
    facts = Facts.model_validate(sheet.document) if sheet else None
    products = [
        p.get("name") for p in (facts.products if facts else []) if isinstance(p, dict) and p.get("name")
    ]
    at = now()
    return {
        "website": {k: snapshot.get(k) for k in ("url", "text", "image_descriptions") if snapshot.get(k)},
        "products": facts.products if facts else [],
        "product_names": products,
        "facts": [f.text for f in facts.facts if active(f, at) and f.product in {product, "company"}][:20]
        if facts
        else [],
    }


def _grounding(catalog, product):
    parts = [
        catalog["website"].get("text", ""),
        " ".join(catalog["website"].get("image_descriptions", [])),
        json.dumps(catalog["products"]),
        " ".join(catalog["facts"]),
    ]
    if product != "company":
        parts.append(product)
    return " ".join(parts).casefold()


def _grounded(name, text):
    name = name.casefold()
    return len(name) >= 4 and bool(re.search(r"(?<!\w)" + re.escape(name) + r"s?(?!\w)", text))


def _rule_plan(idea, signals, catalog, product, has_video_reference):
    systems = signals["systems"]
    if not systems and signals["all_products"]:
        systems = catalog["product_names"]
    if not systems and product != "company":
        systems = [product]
    settings = signals["settings"]
    count = max(len(settings), len(systems))
    shots = [
        Shot(
            setting=settings[i % len(settings)] if settings else "",
            system=systems[i % len(systems)] if systems else "",
        )
        for i in range(min(count, MAX_CLIPS))
    ]
    operation = (
        "video_edit" if has_video_reference and signals["edit"] else "video" if signals["video"] else "image"
    )
    end = signals["end_card"] and operation != "image"
    duration = signals["seconds"] or (
        min(90, max(5, len(shots) * 6 + (END_CARD_SECONDS if end else 0))) if len(shots) > 1 else 10
    )
    return IdeaPlan(
        title=default_title(idea),
        operation=operation,
        duration=duration,
        kind="non_promotional" if signals["editorial"] else "promotional",
        shots=shots,
        soundtrack=signals["soundtrack"],
        end_card=end,
        available_systems=catalog["product_names"],
        planner="rules",
    )


def plan_idea(
    db,
    settings,
    *,
    idea,
    requested_format="auto",
    requested_style="auto",
    product="company",
    facts_id="",
    snapshot=None,
    has_video_reference=False,
    has_logo=False,
    platforms=(),
):
    """Return a reviewable IdeaPlan. Live mode asks Claude; explicit signals in the idea always win."""
    if requested_format not in FORMATS:
        raise ValueError("Choose a format: let the idea decide, image, video or edit an uploaded video")
    if requested_style not in STYLES:
        raise ValueError("Choose a content style")
    catalog = catalog_for(db, facts_id, snapshot or {}, product)
    signals = read_signals(idea, catalog["product_names"])
    if settings.ai_mode == "live":
        from .providers import Claude

        context = {
            "idea": idea,
            "requested_format": requested_format,
            "requested_style": requested_style,
            "product_focus": product,
            "platforms": list(platforms),
            "has_video_reference": has_video_reference,
            "logo_available": has_logo,
            "end_card_seconds": END_CARD_SECONDS,
            "max_clips": MAX_CLIPS,
            "explicit_signals": signals,
            "catalog": {k: catalog[k] for k in ("website", "products", "facts")},
        }
        try:
            plan = IdeaPlan.model_validate(Claude(settings).call(PLANNER_INSTRUCTIONS, context))
        except (ValidationError, json.JSONDecodeError, TypeError):
            raise ProviderError(
                "The idea planner returned an unusable response. Try again; nothing was generated.",
                provider="Claude",
                category="response",
            ) from None
        plan.planner = settings.anthropic_model
    else:
        plan = _rule_plan(idea, signals, catalog, product, has_video_reference)
    return finalize(
        plan,
        signals,
        idea,
        requested_format,
        requested_style,
        has_video_reference,
        _grounding(catalog, product),
    )


def finalize(plan, signals, idea, requested_format, requested_style, has_video_reference, grounding):
    notes = [n.strip()[:200] for n in plan.notes if n.strip()][:6]
    operation = plan.operation
    if requested_format != "auto":
        operation = requested_format
    elif operation == "image" and signals["video"]:
        operation = "video"
        notes.append("Your idea asks for motion, clips, music or a length, so this is a video.")
    if operation == "video_edit" and not has_video_reference and requested_format == "auto":
        operation = "video"
    duration = plan.duration
    if signals["seconds"] and operation != "image":
        if duration != signals["seconds"]:
            notes.append(f"Length set to {signals['seconds']} seconds as written in your idea.")
        duration = signals["seconds"]
    if signals["seconds_requested"] and signals["seconds_requested"] > 90 and operation != "image":
        notes.append("Videos can be at most 90 seconds, so the length was shortened.")
    end_card = operation != "image" and (plan.end_card or signals["end_card"])
    if end_card and duration < END_CARD_SECONDS + MIN_CLIP_SECONDS:
        duration = END_CARD_SECONDS + MIN_CLIP_SECONDS
    soundtrack = (
        ""
        if operation == "image" or (signals["silent"] and not signals["soundtrack"])
        else (plan.soundtrack.strip() or signals["soundtrack"])
    )
    if operation == "image" and (signals["soundtrack"] or signals["end_card"]):
        notes.append("Images can't carry music or a logo end card; choose Video to include them.")
    shots, dropped = [], []
    for shot in plan.shots:
        if shot.system and not _grounded(shot.system, grounding):
            dropped.append(shot.system)
            shot = Shot(setting=shot.setting)
        if shot.setting or shot.system:
            shots.append(shot)
    if dropped:
        notes.append(
            "Not found on tudelu.com or in the approved facts, so the prompt writer will choose instead: "
            + ", ".join(dict.fromkeys(dropped))[:200]
        )
    if operation == "image":
        shots = shots[:1]
    elif operation == "video_edit":
        shots = []
    else:
        clip_seconds, max_clips = clip_limits(duration, end_card)
        if len(shots) > max_clips:
            notes.append(
                f"{len(shots)} clips don't fit in {clip_seconds} seconds of footage (each clip needs at least "
                f"{MIN_CLIP_SECONDS} seconds), so the first {max_clips} are kept. Lengthen the video to keep more."
            )
            shots = shots[:max_clips]
    systems = [
        s
        for s in dict.fromkeys(" ".join(s.split()) for s in plan.available_systems)
        if _grounded(s, grounding)
    ]
    kind = requested_style if requested_style != "auto" else plan.kind
    return IdeaPlan(
        title=(" ".join(plan.title.split()) or default_title(idea))[:200],
        operation=operation,
        duration=duration,
        kind=kind,
        shots=shots,
        soundtrack=soundtrack[:1800],
        end_card=end_card,
        available_systems=systems[:40],
        notes=list(dict.fromkeys(notes))[:8],
        planner=plan.planner,
    )


def logo_assets(assets, *, live=False):
    """Image assets that can serve as the logo end card, explicitly tagged logos first."""
    at = now()
    found = [
        a
        for a in assets
        if a.media.get("mime", "").startswith("image/")
        and a.source == "company"
        and (a.tags.get("brand_logo") or "logo" in a.original_name.casefold())
        and not (a.rights_expire_at and a.rights_expire_at <= at)
        and not (live and a.demo_only)
        and "rendition" not in a.tags
    ]
    return sorted(found, key=lambda a: (not a.tags.get("brand_logo"), -a.created_at.timestamp()))


def clip_seconds_preview(plan):
    """Clip lengths shown on the review page; mirrors queue_jobs and plan_segments."""
    if plan.operation != "video":
        return []
    clip_seconds, _ = clip_limits(plan.duration, plan.end_card)
    count = max(len(plan.shots), math.ceil(clip_seconds / MAX_CLIP_SECONDS), 1)
    base, extra = divmod(clip_seconds, count)
    return [base + (i < extra) for i in range(count)]


def shot_seconds(plan):
    """Seconds of footage each planned shot gets (queue_jobs maps extra clips to the nearest shot)."""
    clips = clip_seconds_preview(plan)
    if len(plan.shots) <= 1:
        return [sum(clips)] if clips else []
    totals = [0] * len(plan.shots)
    for index, seconds in enumerate(clips):
        totals[index * len(plan.shots) // len(clips)] += seconds
    return totals


def _platform_names(platforms):
    names = [{"linkedin": "LinkedIn", "x": "X", "tiktok": "TikTok"}.get(p, p.title()) for p in platforms]
    return (
        ", ".join(names[:-1]) + (" and " if len(names) > 1 else "") + names[-1] if names else "no platforms"
    )


def plan_summary(plan, platforms):
    """One plain sentence describing what will be made."""
    where = _platform_names(platforms)
    if plan.operation == "image":
        return f"One image for {where}."
    if plan.operation == "video_edit":
        extras = [x for x, on in (("music", plan.soundtrack), ("a logo end card", plan.end_card)) if on]
        tail = " with " + " and ".join(extras) if extras else ""
        return f"A {plan.duration}-second edit of your uploaded video{tail}, for {where}."
    clips = clip_seconds_preview(plan)
    parts = [f"{len(clips)} clip{'s' if len(clips) != 1 else ''}"]
    if plan.end_card:
        parts.append(f"a {END_CARD_SECONDS}-second logo end card")
    if plan.soundtrack:
        parts.append("music")
    return (
        f"A {plan.duration}-second video: "
        + ", ".join(parts[:-1])
        + (" and " if len(parts) > 1 else "")
        + parts[-1]
        + f", for {where}."
    )


def work_summary(plan, platforms):
    """What Runway is asked to generate, so cost is visible before anything starts."""
    count = max(1, len(platforms))
    each = "per platform" if count > 1 else "in total"
    if plan.operation == "image":
        return f"1 Runway image {each}."
    if plan.operation == "video_edit":
        work = "Runway edits your uploaded video in segments of up to 30 seconds"
    else:
        clips = clip_seconds_preview(plan)
        footage = sum(clips)
        work = (
            f"{len(clips)} Runway video clip{'s' if len(clips) != 1 else ''} ({footage} seconds of footage)"
        )
    if plan.soundtrack:
        work += f" and 1 instrumental music track ({plan.duration} seconds)"
    summary = f"{work} {each}."
    if count > 1:
        summary += f" {count} platforms are selected, and each gets its own generation."
    if plan.end_card:
        summary += " The logo end card is made from your uploaded logo, with no Runway cost."
    return summary
