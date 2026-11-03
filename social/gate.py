import hashlib
import math
import re
from datetime import timedelta
from urllib.parse import urljoin

import httpx
from sqlalchemy import func, select

from .config import profiles
from .db import now, uid
from .facts import Facts, active, content_digest, render
from .lifecycle import transition
from .models import Asset, CheckResult, FactSheet, Post
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
            if m.get("bit_rate", 0) > 25_000_000:
                errors.append(f"{a.id}: bitrate")
        elif (
            m["mime"] not in profile.image_formats
            or m.get("frames", 1) != 1
            or m.get("mode") not in {"RGB", "RGBA"}
        ):
            errors.append(f"{a.id}: image format/color/animation")
    return errors


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
            passed, details = False, {"error_type": type(exc).__name__, "reason": "Check could not complete"}
        result = CheckResult(post_id=post.id, run_id=run_id, name=name, passed=bool(passed), details=details)
        db.add(result)
        results.append(result)

    def grounding():
        expected = render(post.parts, profile, facts, post.platform, post.ai_disclosure, settings)
        ids = list(dict.fromkeys(p["fact_id"] for p in post.parts))
        allowed_products = {post.product, "company"}
        relevant = all(f.product in allowed_products for f in facts.facts if f.id in ids)
        return expected == post.caption and ids == post.fact_ids and relevant, {
            "fact_ids": ids,
            "exact_approved_wording": expected == post.caption,
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
    needed = any(a.source != "company" for a in assets)
    check(
        "disclosure",
        lambda: (
            post.ai_disclosure == needed and (not needed or settings.disclosure_text in post.caption),
            {
                "required": needed,
                "method": {"instagram": "is_ai_generated", "x": "made_with_ai", "tiktok": "manual_label"}.get(
                    post.platform, "caption"
                ),
            },
        ),
    )
    check(
        "rights",
        lambda: (all(a.rights and (not a.rights_expire_at or at < a.rights_expire_at) for a in assets), {}),
    )
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
                Post.id != post.id, Post.platform != post.platform, Post.caption == post.caption
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
        "fact_ids": post.fact_ids,
        "facts": facts.model_dump(mode="json"),
        "campaign": post.campaign,
        "platform": post.platform,
        "at": at.isoformat(),
        "assets": [{"source": a.source, "tags": a.tags, "rights": a.rights} for a in assets],
    }

    def model_review():
        review = providers.review(context)
        return (
            review.score >= settings.review_min_score and not review.unsupported_claims,
            review.model_dump(),
        )

    check("model_review", model_review)

    def vision_review():
        if not needed:
            return True, {"reason": "Company media; AI vision check not required"}
        result = providers.vision(assets, context)
        approved = {text for f in selected for text in [f.text, *f.variants]}
        return result.passed and not (
            result.garbled_text or result.misused_logos or result.unexpected_people
        ) and all(c in approved for c in result.visible_claims), result.model_dump()

    check("vision_review", vision_review)
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
    if post.status != "generated":
        raise ValueError("Only generated posts can enter the gate")
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
