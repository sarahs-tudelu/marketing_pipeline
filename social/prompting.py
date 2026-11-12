"""Write each Runway prompt from the admin idea and Tudelu's own sources.

Sources, by authority:
1. Rules: the selected approved facts version (products, brand voice, banned phrases, relevant
   facts), the platform profile, and fixed visual rules that mirror the vision gate.
2. tudelu.com: the batch's saved homepage snapshot (systems catalog, headings, image descriptions).
3. Uploaded company media marked Public: observed visual summaries of real products.
4. Previously generated Runway media: observed summaries and what happened to their posts.

The written prompt must depict one Tudelu system named in sources 1-3 (or the explicitly
selected references). Previous generations guide continuity and variety only; they never
establish what Tudelu sells. Prompts are written in the worker before the paid Runway request.
"""

import json
import re

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from .config import profiles
from .db import now
from .facts import Facts, active
from .models import Asset, FactSheet, MediaJob, Post
from .provider_errors import ProviderError

# promptText limit for gen4_image, gen4.5 and aleph2, in UTF-16 code units (Runway OpenAPI).
PROMPT_LIMIT = 1000
UPLOAD_LIMIT = 8
PREVIOUS_LIMIT = 6
# Bounds the extra OpenAI vision calls made for uploads that have no summary yet.
NEW_SUMMARY_LIMIT = 6

VISUAL_RULES = [
    "Show exactly one Tudelu system from the supplied sources as the clear, dominant subject.",
    "Match how tudelu.com and the uploaded media show that system: form, proportions, materials, "
    "finish and how it moves or operates. Never substitute a different kind of building or structure.",
    "Do not depict systems marked 'Coming soon' unless the admin idea names that system.",
    "Unoccupied scenes: the vision gate rejects unexpected people.",
    "Plain, unmarked surfaces: the vision gate rejects readable text, signage, logos and watermarks.",
    "Photorealistic architectural photography with believable scale, physics and daylight.",
]

WRITER_INSTRUCTIONS = (
    "You write the single promptText sent to Runway for one Tudelu social media image or video. "
    'Return only JSON {"system":"...","prompt":"...","sources_used":["..."],"problem":""}. '
    "system is the Tudelu system the media depicts, copied exactly as it is named in website, rules.products, "
    "rules.facts, uploaded_media or selected_references. Choose the system that best fits admin_idea; if "
    "product is not 'company', depict that product. If the idea cannot be shown with a system Tudelu offers "
    "in those sources, return empty system and prompt and explain in problem. "
    "Ground the system's appearance in uploaded_media and selected_references summaries first (they observe "
    "real media), then in the website image descriptions. Use previous_generations only to keep a consistent "
    "look and to choose a different composition, viewpoint and setting from them; they may contain mistakes, "
    "and those with held, failed or deleted posts must never be copied. They are not evidence of what Tudelu sells. "
    "Follow visual_rules, rules.brand_voice and the direction. Respect rules.banned_phrases. "
    "Runway models do not support negative prompts: describe only what should appear, using positive "
    "wording such as 'plain unmarked surfaces' or 'an empty terrace', never 'no text' or 'without people'. "
    "Write one coherent scene in plain descriptive sentences: subject, setting, composition, lighting, mood. "
    "For video, describe one continuous shot with a specific camera movement and how the system moves. "
    "For video with a starting image (has_start_image), describe the motion and camera only, preserving the image. "
    "For video_edit, describe the change to apply to the supplied video while preserving the system's geometry. "
    "Keep prompt under 900 characters. sources_used lists which sources informed it, such as "
    "'website', 'rules', 'uploaded:<asset_id>', 'reference:<asset_id>', 'previous:<asset_id>'."
)


class WrittenPrompt(BaseModel):
    system: str = Field(default="", max_length=200)
    prompt: str = Field(default="", max_length=6000)
    sources_used: list[str] = Field(default_factory=list, max_length=40)
    problem: str = Field(default="", max_length=2000)


def utf16_trim(text, limit=PROMPT_LIMIT):
    """Fit Runway's UTF-16 limit, preferring to end on a complete sentence."""
    text = " ".join(text.split())
    if len(text.encode("utf-16-le")) <= limit * 2:
        return text
    cut = text.encode("utf-16-le")[: limit * 2].decode("utf-16-le", errors="ignore")
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return cut[: end + 1] if end > limit // 2 else cut


def _summary(asset):
    saved = asset.media.get("visual_summary")
    return saved.get("summary", "") if isinstance(saved, dict) else ""


def _usable(asset, at, fixture):
    return (
        asset.media.get("mime", "").startswith(("image/", "video/"))
        and (fixture or not asset.demo_only)
        and not (asset.rights_expire_at and asset.rights_expire_at <= at)
        and not (asset.tags.get("real_customer_content") and not asset.customer_permission)
    )


def _describe(db, settings, asset, budget):
    """Return the asset's observed summary, creating one within the per-prompt budget."""
    from .visual_context import current_summary, ensure_visual_summary

    if current_summary(asset, settings):
        return _summary(asset)
    if budget[0] <= 0:
        return _summary(asset)
    budget[0] -= 1
    try:
        ensure_visual_summary(db, settings, asset)
    except Exception:
        # A missing description only narrows the context; it never blocks generation.
        return _summary(asset)
    return _summary(asset)


def gather_sources(db, settings, job):
    """Collect the writer context and a provenance record for one queued visual job."""
    brief = job.brief
    at = now()
    fixture = brief["execution_mode"] == "fixture"
    summary_settings = settings.model_copy(update={"ai_mode": brief["execution_mode"]})
    budget = [NEW_SUMMARY_LIMIT]
    profile = profiles(settings)[job.platform]

    facts = Facts.model_validate(db.get(FactSheet, brief["facts_id"]).document)
    scope = {brief["product"], "company"}
    rules = {
        "facts_version": facts.version,
        "brand_voice": facts.brand_voice,
        "banned_phrases": facts.banned_phrases,
        "products": facts.products,
        "facts": [f.text for f in facts.facts if active(f, at) and f.product in scope][:20],
    }

    selected = []
    for asset_id in brief["reference_ids"]:
        asset = db.get(Asset, asset_id)
        if asset:
            selected.append(
                {
                    "asset_id": asset.id,
                    "name": asset.original_name,
                    "tags": asset.tags,
                    "summary": _describe(db, summary_settings, asset, [1]),
                }
            )

    candidates = [
        a
        for a in db.scalars(
            select(Asset)
            .where(Asset.source == "company", Asset.rights == "public")
            .order_by(Asset.created_at.desc())
            .limit(60)
        ).all()
        if _usable(a, at, fixture) and a.id not in brief["reference_ids"]
    ]
    if brief["product"] != "company":
        candidates.sort(key=lambda a: a.tags.get("product") != brief["product"])
    uploaded = []
    for asset in candidates[:UPLOAD_LIMIT]:
        uploaded.append(
            {
                "asset_id": asset.id,
                "name": asset.original_name,
                "tags": asset.tags,
                "summary": _describe(db, summary_settings, asset, budget),
            }
        )

    prefix = "fixture/runway/" if fixture else "runway/"
    generated = [
        a
        for a in db.scalars(
            select(Asset)
            .where(Asset.source.in_(["ai_generated", "ai_edited"]), Asset.generator.like(prefix + "%"))
            .order_by(Asset.created_at.desc())
            .limit(60)
        ).all()
        if "rendition" not in a.tags and _summary(a) and _usable(a, at, fixture)
    ][:PREVIOUS_LIMIT]
    jobs = {
        j.id: j
        for j in db.scalars(
            select(MediaJob).where(MediaJob.id.in_([a.tags.get("media_job_id") for a in generated]))
        ).all()
    }
    statuses = dict(
        db.execute(
            select(Post.id, Post.status).where(Post.id.in_([j.post_id for j in jobs.values() if j.post_id]))
        ).all()
    )
    previous = []
    for asset in generated:
        source_job = jobs.get(asset.tags.get("media_job_id"))
        post_id = source_job.post_id if source_job else None
        previous.append(
            {
                "asset_id": asset.id,
                "summary": _summary(asset),
                "platform": source_job.platform if source_job else None,
                "concept": asset.tags.get("creative_brief", {}).get("concept"),
                "post_status": statuses.get(post_id, "no post") if post_id else "no post",
                "created_at": asset.created_at.isoformat(),
            }
        )

    website = brief.get("website_snapshot") or {}
    # Mirrors the ratios chosen in runway.payload_for.
    if brief["operation"] == "image":
        orientation = "portrait" if profile.image_height > profile.image_width else "square"
    else:
        orientation = "portrait" if profile.video_height > profile.video_width else "landscape"
    context = {
        "admin_idea": brief["prompt"],
        "campaign": brief["campaign"],
        "product": brief["product"],
        "content_kind": brief["kind"],
        "direction": brief.get("direction", ""),
        "operation": brief["operation"],
        "has_start_image": brief["operation"] == "video" and bool(brief["reference_ids"]),
        "duration_seconds": brief["duration"] if brief["operation"] != "image" else None,
        "platform": job.platform,
        "platform_tone": profile.tone,
        "orientation": orientation,
        "visual_rules": VISUAL_RULES,
        "rules": rules,
        "website": {k: website.get(k) for k in ("url", "text", "image_descriptions") if website.get(k)},
        "uploaded_media": uploaded,
        "selected_references": selected,
        "previous_generations": previous,
    }
    provenance = {
        "facts_version": facts.version,
        "website": {k: website.get(k) for k in ("url", "sha256", "fetched_at", "demo_only") if k in website}
        or None,
        "uploaded_asset_ids": [u["asset_id"] for u in uploaded],
        "reference_ids": [s["asset_id"] for s in selected],
        "previous_asset_ids": [p["asset_id"] for p in previous],
    }
    return context, provenance


def grounding_text(context):
    """Everything that may name a system Tudelu offers. Previous generations are excluded."""
    parts = [
        context["website"].get("text", ""),
        " ".join(context["website"].get("image_descriptions", [])),
        json.dumps(context["rules"]["products"]),
        " ".join(context["rules"]["facts"]),
        json.dumps(context["uploaded_media"]),
        json.dumps(context["selected_references"]),
    ]
    if context["product"] != "company":
        parts.append(context["product"])
    return " ".join(parts).casefold()


def grounded(name, text):
    """Whole-word match, allowing a singular form of a plural catalog name such as Pergolas."""
    name = name.casefold()
    return len(name) >= 4 and bool(re.search(r"(?<!\w)" + re.escape(name) + r"s?(?!\w)", text))


def _fixture_prompt(context):
    products = [p.get("name") for p in context["rules"]["products"] if isinstance(p, dict) and p.get("name")]
    system = (
        context["product"] if context["product"] != "company" else (products or ["architectural system"])[0]
    )
    prompt = (
        f"{context['admin_idea'].rstrip('. ')}. Subject: Tudelu {system}. {context['direction']} "
        f"{context['orientation'].capitalize()} framing, an unoccupied setting with plain, unmarked surfaces."
    )
    return WrittenPrompt(system=system, prompt=prompt, sources_used=["rules"])


def write_prompt(db, settings, job):
    """Return brief fields for the composed Runway prompt and its provenance."""
    context, provenance = gather_sources(db, settings, job)
    if job.brief["execution_mode"] == "fixture":
        written, writer = _fixture_prompt(context), "fixture/deterministic"
    else:
        from .providers import Claude

        try:
            written = WrittenPrompt.model_validate(Claude(settings).call(WRITER_INSTRUCTIONS, context))
        except (ValidationError, json.JSONDecodeError, TypeError):
            raise ProviderError(
                "The Runway prompt writer returned an unusable response. Resume the job to try again; "
                "nothing was sent to Runway.",
                provider="Claude",
                category="response",
            ) from None
        writer = settings.anthropic_model
        if not written.system.strip() or not written.prompt.strip():
            raise ValueError(
                "This idea could not be matched to a Tudelu system found on tudelu.com, in the approved "
                "facts or in uploaded media. Nothing was sent to Runway. "
                + (written.problem.strip()[:400] or "Edit the idea to name a Tudelu system.")
            )
        name = re.sub(r"\s+", " ", written.system).strip()
        if not grounded(name, grounding_text(context)):
            raise ValueError(
                f"The prompt writer chose '{name[:80]}', which is not named on tudelu.com, in the approved "
                "facts or in uploaded media. Nothing was sent to Runway. Resume to try again, or edit the idea."
            )
    prompt = utf16_trim(written.prompt)
    return {
        "generation_prompt": prompt,
        "depicted_system": written.system.strip(),
        "prompt_writer": {
            "writer": writer,
            "written_at": now().isoformat(),
            "sources": provenance,
            "sources_used": written.sources_used,
            "visual_rules": VISUAL_RULES,
        },
    }
