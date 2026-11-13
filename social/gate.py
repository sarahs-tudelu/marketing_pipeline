import hashlib
import math
import re
from datetime import timedelta
from urllib.parse import urljoin

import httpx
from sqlalchemy import func, select

from .audio_review import review_audio
from .config import profiles
from .db import now, uid
from .facts import Facts, active, caption_fact_ids, composed, content_digest, part_fact_ids, render
from .image_provenance import verify_asset_metadata
from .lifecycle import PostConflict, lock_post, transition
from .models import Asset, CheckResult, FactSheet, Post
from .provenance import asset_source_error, asset_type, requires_ai_review
from .provider_errors import ProviderError
from .providers import Providers
from .security import public_url
from .storage import storage

URL_RE = re.compile(r"https?://[^\s<>]+")
TESTIMONIAL_RE = re.compile(
    r"testimonial|review|customer\s+(?:says|said|loves)|[\u201c\u201d\"]|\b[1-5][ -]star\b", re.I
)


def weighted_length(text, platform):
    if platform != "x":
        return len(text)
    # Conservative upper bound for non-Latin characters/emoji; URLs use t.co's 23 characters.
    clean = URL_RE.sub("x" * 23, text)
    return sum(
        1
        if ord(c) <= 0x10FF
        or 0x2000 <= ord(c) <= 0x200D
        or 0x2010 <= ord(c) <= 0x201F
        or 0x2032 <= ord(c) <= 0x2037
        else 2
        for c in clean
    )


def check_urls(caption, allowed, client=None):
    urls = URL_RE.findall(caption)
    if any(url not in allowed for url in urls):
        return False, {"reason": "URL not in canonical facts", "urls": urls}
    client = client or httpx.Client(timeout=15, follow_redirects=False)
    for url in urls:
        current = url
        for _ in range(6):
            public_url(current)
            with client.stream("GET", current) as response:
                if response.is_redirect:
                    current = urljoin(current, response.headers["location"])
                    continue
                if not 200 <= response.status_code < 300:
                    return False, {"url": url, "http_status": response.status_code}
                break
        else:
            return False, {"reason": "Too many redirects"}
    return True, {"urls": urls}


def media_errors(assets, profile, platform):
    errors = []
    if not assets or len(assets) > profile.max_assets:
        errors.append("Invalid asset count")
    videos = [a for a in assets if a.media["mime"].startswith("video/")]
    if videos and len(assets) != 1:
        # Instagram supports mixed carousels; conservative implementation supports image carousels.
        errors.append("Use one video or an image-only carousel")
    if platform == "tiktok" and len(videos) != 1:
        errors.append("TikTok inbox adapter requires one video")
    for a in assets:
        m = a.media
        video = m["mime"].startswith("video/")
        ratio = m["width"] / max(m["height"], 1)
        low, high = (
            (profile.video_min_ratio, profile.video_max_ratio)
            if video
            else (profile.image_min_ratio, profile.image_max_ratio)
        )
        if not profile.min_width <= m["width"] <= profile.max_width or m["height"] < profile.min_height:
            errors.append(f"{a.id}: dimensions")
        if not low <= ratio <= high:
            errors.append(f"{a.id}: aspect ratio")
        if m["bytes"] > (profile.video_max_bytes if video else profile.image_max_bytes):
            errors.append(f"{a.id}: file size")
        if video:
            if m["mime"] != "video/mp4" or m.get("codec") != "h264" or m.get("pixel_format") != "yuv420p":
                errors.append(f"{a.id}: requires MP4 H.264 yuv420p")
            if not profile.min_duration <= m["duration"] <= profile.max_duration:
                errors.append(f"{a.id}: duration")
            if not 23 <= m.get("fps", 0) <= 60 or any(c != "aac" for c in m.get("audio_codecs", [])):
                errors.append(f"{a.id}: frame rate/audio codec")
            # Reels minimum is 540x960 (either orientation).
            if platform == "facebook" and (
                min(m["width"], m["height"]) < 540 or max(m["width"], m["height"]) < 960
            ):
                errors.append(f"{a.id}: Reels need at least 540x960")
            if m.get("bit_rate", 0) > 25_000_000:
                errors.append(f"{a.id}: bitrate")
        elif (
            m["mime"] not in profile.image_formats
            or m.get("frames", 1) != 1
            or m.get("mode") not in {"RGB", "RGBA"}
        ):
            errors.append(f"{a.id}: image format/color/animation")
    return errors


def check_disclosure(post, assets, live=False, *, settings=None):
    needed = any(requires_ai_review(a) for a in assets)
    reasons = [f"{a.id}: {error}" for a in assets if (error := asset_source_error(a))]
    if post.ai_disclosure != needed:
        reasons.append("Incorrect AI disclosure flag for asset provenance")
    # Facebook videos publish as Reels, whose finish call carries is_ai_generated.
    # Facebook photos have no API label field; Meta labels them from embedded metadata.
    facebook_video = post.platform == "facebook" and any(a.media["mime"].startswith("video/") for a in assets)
    method = {"instagram": "is_ai_generated", "x": "made_with_ai", "tiktok": "manual_label"}.get(
        post.platform, "is_ai_generated" if facebook_video else "preserved_metadata"
    )
    evidence = [verify_asset_metadata(a, settings) for a in assets if requires_ai_review(a)] if method == "preserved_metadata" else []
    if live:
        reasons.extend(f"{e['asset_id']}: {e['reason']}" for e in evidence if not e["byte_verified"])
    return not reasons, {
        "required": needed,
        "method": method,
        "caption_disclosure": False,
        "reasons": reasons,
        "embedded_evidence": evidence,
        "metadata_verified": bool(evidence) and all(e["byte_verified"] for e in evidence),
        "metadata_required_for_live": needed and method == "preserved_metadata",
        "limitations": "Platform detection of embedded metadata is not guaranteed."
        if needed and method == "preserved_metadata"
        else "",
    }


def evaluate(db, settings, post, at=None, providers=None, url_client=None, live=False):
    at = at or now()
    providers = providers or Providers(settings)
    profile = profiles(settings)[post.platform]
    facts = Facts.model_validate(db.get(FactSheet, post.facts_id).document)
    assets = [db.get(Asset, id) for id in post.asset_ids]
    run_id = uid()
    results = []

    def check(name, fn):
        try:
            passed, details = fn()
        except Exception as exc:
            # Provider errors are written to be shown; anything else stays a type name.
            reason = str(exc) if isinstance(exc, ProviderError) else "Check could not complete"
            passed, details = False, {"error_type": type(exc).__name__, "reason": reason}
        result = CheckResult(post_id=post.id, run_id=run_id, name=name, passed=bool(passed), details=details)
        db.add(result)
        results.append(result)

    def grounding():
        expected = render(post.parts, profile, facts, post.platform, settings)
        ids = caption_fact_ids(post.parts)
        allowed_products = {post.product, "company"}
        relevant = all(f.product in allowed_products for f in facts.facts if f.id in ids)
        return expected == post.caption and ids == post.fact_ids and relevant, {
            "fact_ids": ids,
            "render_matches": expected == post.caption,
            "semantic_review_required": composed(post.parts),
        }

    check("grounding", grounding)
    selected = [f for f in facts.facts if f.id in post.fact_ids]
    check(
        "prices_offers_validity",
        lambda: (
            all(active(f, at) for f in selected),
            {
                "expired_or_future": [f.id for f in selected if not active(f, at)],
                "checked_at": at.isoformat(),
            },
        ),
    )
    check("urls", lambda: check_urls(post.caption, set(facts.canonical_urls), url_client))

    def media_urls():
        if not live:
            return True, {"reason": "Dry run uses local media"}
        pulled = (
            post.platform == "instagram"
            or (post.platform == "facebook" and assets[0].media["mime"].startswith("image/"))
            or (post.platform == "tiktok" and settings.tiktok_transfer == "url")
        )
        urls = [storage(settings).url(a.storage_key) for a in assets] if pulled else []
        return check_urls("\n".join(urls), set(urls), url_client)

    check("media_urls", media_urls)
    check(
        "length",
        lambda: (
            weighted_length(post.caption, post.platform) <= profile.max_length,
            {"length": weighted_length(post.caption, post.platform), "limit": profile.max_length},
        ),
    )
    hashtags = re.findall(r"#\w+", post.caption)
    check(
        "hashtags",
        lambda: (
            len(hashtags) <= profile.hashtag_limit
            and all(t.lstrip("#") in facts.hashtags.get(post.platform, []) for t in hashtags),
            {"hashtags": hashtags},
        ),
    )
    check(
        "mentions_links",
        lambda: (
            (profile.allow_mentions or not re.search(r"@\w+", post.caption))
            and (profile.allow_links or not URL_RE.search(post.caption))
            and (post.platform != "x" or settings.x_include_links or not URL_RE.search(post.caption)),
            {},
        ),
    )
    banned = [p for p in facts.banned_phrases if p.casefold() in post.caption.casefold()]
    check("banned_phrases", lambda: (not banned, {"matches": banned}))
    check(
        "testimonials",
        lambda: (
            not (TESTIMONIAL_RE.search(post.caption) or any(f.kind == "testimonial" for f in selected))
            or any(
                a.tags.get("real_customer_content") and a.customer_permission and a.permission_reference
                for a in assets
            ),
            {},
        ),
    )
    needed = any(requires_ai_review(a) for a in assets)
    check("disclosure", lambda: check_disclosure(post, assets, live, settings=settings))

    def rights():
        from .asset_edit import ancestry

        sources, missing = ancestry(db, assets)
        # Not public media supports dry runs only; legacy unclassified text keeps its prior behavior.
        private = [a.id for a in sources if a.rights == "not_public"] if live else []
        current = all(a.rights and (not a.rights_expire_at or at < a.rights_expire_at) for a in sources)
        unpermitted = [a.id for a in sources if a.tags.get("real_customer_content") and not (a.customer_permission and a.permission_reference)] if live else []
        details = {}
        if private:
            details["not_public_for_live"] = private
        if missing:
            details["missing_source_assets"] = missing
        if unpermitted:
            details["missing_customer_permission"] = unpermitted
        return current and not private and not missing and not unpermitted, details

    check("rights", rights)
    check("media", lambda: (not (errors := media_errors(assets, profile, post.platform)), {"errors": errors}))
    check(
        "provenance_integrity",
        lambda: (
            all(
                hashlib.sha256(storage(settings).read(a.storage_key)).hexdigest() == a.checksum
                for a in assets
            ),
            {"byte_preservation": True},
        ),
    )

    def duplicates():
        vector, model = providers.embedding(post.caption)
        post.embedding, post.embedding_model = vector, model
        candidates = db.scalars(
            select(Post).where(
                Post.id != post.id,
                Post.platform == post.platform,
                func.coalesce(Post.published_at, Post.scheduled_at, Post.created_at)
                >= at - timedelta(days=settings.duplicate_days),
                Post.status.in_(["approved", "scheduled", "publishing", "published", "awaiting_manual"]),
            )
        ).all()
        similarities = []
        for previous in candidates:
            other = previous.embedding
            if previous.embedding_model != model or not other:
                other, _ = providers.embedding(previous.caption)
            cosine = sum(a * b for a, b in zip(vector, other)) / max(
                math.sqrt(sum(a * a for a in vector) * sum(b * b for b in other)), 1e-12
            )
            similarities.append({"post_id": previous.id, "similarity": cosine})
        maximum = max([s["similarity"] for s in similarities], default=0)
        cross = db.scalar(
            select(Post.id).where(
                Post.id != post.id,
                Post.platform != post.platform,
                Post.caption == post.caption,
                Post.status != "deleted",
            )
        )
        return maximum < settings.duplicate_threshold and not cross, {
            "maximum": maximum,
            "embedding_model": model,
            "cross_platform_identical": bool(cross),
        }

    check("duplicates", duplicates)
    context = {
        "caption": post.caption,
        "caption_parts": post.parts,
        "fact_ids": post.fact_ids,
        "facts": facts.model_dump(mode="json"),
        "campaign": post.campaign,
        "platform": post.platform,
        "product": post.product,
        "content_kind": post.kind,
        "execution_mode": "live" if live else "dry_run",
        "public_publication": live,
        "at": at.isoformat(),
        "renderer_context": {
            "lead_in": "" if composed(post.parts) else profile.lead_in,
            "required_disclaimers": facts.required_disclaimers,
            "hashtags": facts.hashtags.get(post.platform, [])[: profile.hashtag_limit],
            "automatic_caption_disclosure": False,
            "disclosure_check": next(c.details | {"passed": c.passed} for c in results if c.name == "disclosure"),
        },
        "assets": [
            {
                "asset_id": a.id,
                "source": a.source,
                "asset_type": asset_type(a),
                "media": a.media,
                "product_family": a.tags.get("product_family") or a.tags.get("product"),
                "tags": a.tags,
                "rights": a.rights,
                "owner": a.owner,
                "rights_status": a.tags.get("rights_status") or a.rights,
                "rights_expire_at": a.rights_expire_at,
                "source_or_creation_record": {
                    "generator": a.generator,
                    "prompt": a.prompt,
                    "checksum": a.checksum,
                },
                "demo_only": a.demo_only,
            }
            for a in assets
        ],
    }

    def model_review():
        review = providers.review(context)
        coverage = not composed(post.parts) or (
            [c.part_index for c in review.part_checks] == list(range(len(post.parts)))
            and all(
                c.supported and c.fact_ids == part_fact_ids(post.parts[c.part_index]) and c.reasons
                for c in review.part_checks
            )
        )
        return (
            review.score >= settings.review_min_score and not review.unsupported_claims and coverage,
            review.model_dump() | {"complete_citation_review": bool(coverage)},
        )

    def vision_review():
        if not needed:
            return True, {"reason": "Company media; AI vision check not required"}
        result = providers.vision(assets, context)
        approved = {text for f in selected for text in [f.text, *f.variants]}
        return result.passed and not (
            result.garbled_text or result.misused_logos or result.unexpected_people
        ) and all(c in approved for c in result.visible_claims), result.model_dump()

    check("audio_review", lambda: review_audio(db, settings, assets, context, facts, at, providers))
    audio_check = results[-1]
    context["audio_review"] = {"passed": audio_check.passed, "details": audio_check.details}
    for record, asset in zip(context["assets"], assets):
        record["media"] = asset.media
    check("vision_review", vision_review)
    visual_check = results[-1]
    context["visual_review"] = {"passed": visual_check.passed, "details": visual_check.details}
    check("model_review", model_review)
    check(
        "live_readiness",
        lambda: (
            not live
            or (
                settings.ai_mode == "live"
                and settings.embedding_provider == "openai"
                and not facts.demo_only
                and not any(a.demo_only for a in assets)
            ),
            {"live": live, "demo_facts": facts.demo_only, "ai_mode": settings.ai_mode},
        ),
    )
    db.flush()
    return results, assets


def run_gate(db, settings, post, **kwargs):
    lock_post(db, post)
    if post.status != "generated":
        db.rollback()
        raise PostConflict("This post has already changed status. Refresh its page before running checks.")
    results, assets = evaluate(db, settings, post, **kwargs)
    transition(db, post, "checked", "Automated checks completed")
    failures = [c.name for c in results if not c.passed]
    if failures:
        transition(db, post, "held", ", ".join(failures))
    else:
        post.approved_digest = content_digest(post, assets)
        transition(db, post, "approved", "All checks passed")
    db.commit()
    return results
