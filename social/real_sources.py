"""Eligible original media and candidate selection for real-project posts."""

from functools import cache
from itertools import islice

from sqlalchemy import select

from .config import REAL_MEDIA_ONLY
from .db import now
from .library import products
from .models import Account, MediaCampaign, MediaJob
from .product_rules import normalize
from .provenance import requires_ai_review
from .storage import load_assets
from .video_rules import MUSIC_TAG

PHOTO_CANDIDATES = 8  # Photos the editor chooses from for a photo post.
VIDEO_CANDIDATES = 6  # Videos (and up to four photos) it chooses from for a video.
VIDEO_PHOTOS = 4


def real_asset(asset, *, live=False, public=True):
    """Company photos and videos a real-project post may show: real, cleared, not a logo, track or copy."""
    tags = asset.tags or {}
    return bool(
        asset.media.get("mime", "").startswith(("image/", "video/"))
        and _cleared(asset, live=live, public=public)
        and not tags.get("brand_logo")
        and not tags.get("rendition")
        and not tags.get("edit_interval")
    )


def real_sound(asset, *, live=False, public=True):
    """Recorded company audio that can be placed in a real-project video as it is."""
    return bool(
        asset.media.get("mime", "").startswith("audio/")
        and _cleared(asset, live=live, public=public)
        and not (asset.tags or {}).get(MUSIC_TAG)
    )


def _cleared(asset, *, live, public):
    tags = asset.tags or {}
    return (
        not requires_ai_review(asset)
        and bool((asset.rights or "").strip())
        and not (public and asset.rights == "not_public")
        and not (asset.rights_expire_at and asset.rights_expire_at <= now())
        and not (live and asset.demo_only)
        and not (tags.get("real_customer_content") and not (asset.customer_permission and asset.permission_reference))
    )


@cache
def _product_aliases():
    from .reference_grounding import reference_config

    profiles = reference_config("visual-guidance.builder.yaml")["products"]
    return {
        normalize(name): profile["product_id"]
        for profile in profiles
        for name in (profile["product_id"], profile["name"], *profile["aliases"])
    }


def _product_rank(asset, product):
    """0 for the campaign's product (or any, for company-wide), 1 untagged, None for another product."""
    if product.casefold() == "company":
        return 0
    names = products(asset)
    if not names:
        return 1
    aliases = _product_aliases()
    key = normalize(product)
    family = aliases.get(key, key)
    return 0 if any(aliases.get(tag, tag) == family for tag in map(normalize, names)) else None


def recent_uses(db):
    """Asset ID → how many real-project jobs ago it was last shown (0 is the newest)."""
    uses = {}
    jobs = db.scalars(select(MediaJob).order_by(MediaJob.created_at.desc()).limit(80)).all()
    for rank, job in enumerate(j for j in jobs if (j.brief or {}).get("operation") == "real"):
        for item in (job.brief.get("real_edit") or {}).get("items", []):
            uses.setdefault(item["asset_id"], rank)
    return uses


def real_choice(db, brief, sources, profile, platform, turn, *, live=False, drive_only=False, keep_order=False,
                library=None, scores=None):
    """The real-project post for a platform: (format, media IDs, audio IDs), or None when nothing fits.

    The real photos and videos picked for the brief are used; with none picked, the library's. Media not
    shown recently comes first. Formats alternate between video and photo by turn; TikTok gets video.
    `drive_only` (Pinterest) keeps only media imported from Google Drive, audio included. `keep_order` keeps
    the brief's order of the media it picked; a brief's `real_format` is used when the platform allows it.
    """
    public = brief.rights != "not_public"

    def fits(asset, check):
        return check(asset, live=live, public=public) and (
            not drive_only or bool((asset.tags or {}).get("google_drive_file_id"))
        )

    media = [a for a in sources if a.id in brief.reference_ids and fits(a, real_asset)]
    picked = bool(media)
    if not brief.reference_ids:
        if library is None:
            from .media_search import library_media

            media = library_media(db, product=brief.product, live=live, public=public, drive_only=drive_only, limit=None)
        else:
            media = [a for a in library if fits(a, real_asset)]
    uses = recent_uses(db)
    if not (picked and keep_order):
        media.sort(key=lambda a: (-(scores or {}).get(a.id, 0), _product_rank(a, brief.product) or 0,
                                  a.id in uses, -uses.get(a.id, 0)))
    photos = [a for a in media if a.media["mime"].startswith("image/")]
    videos = [a for a in media if a.media["mime"].startswith("video/")]
    photo_posts = bool(photos and profile.image_formats)
    formats = (["video"] if videos or (photos and (not photo_posts or brief.real_format == "video")) else [])
    formats += ["photo"] if photo_posts else []
    if not formats:
        return None
    chosen = brief.real_format if brief.real_format in formats else formats[turn % len(formats)]
    if chosen == "photo":
        ids = [a.id for a in photos[:PHOTO_CANDIDATES]]
    else:
        ids = [a.id for a in videos[:VIDEO_CANDIDATES]]
        ids += [a.id for a in photos[: (VIDEO_CANDIDATES if videos else PHOTO_CANDIDATES)]]
        if scores:
            ids.sort(key=lambda asset_id: -scores.get(asset_id, 0))
    sounds = []
    if chosen == "video":
        sounds = [a.id for a in sources if a.id in brief.audio_reference_ids and fits(a, real_sound)]
    return chosen, ids, sounds[:3]


def candidate_batches(photos, videos, video):
    photos, videos = iter(photos), iter(videos)
    while True:
        clips = list(islice(videos, VIDEO_CANDIDATES)) if video else []
        group = clips + list(islice(photos, VIDEO_PHOTOS if clips else PHOTO_CANDIDATES))
        if not group:
            return
        yield group


def _job_scope(db, job):
    brief = job.brief
    scope = brief.get("real_library_scope")
    if scope is not None:
        return scope
    # Older jobs saved only their first shortlist. Recover the campaign scope when it still applies.
    campaign = db.get(MediaCampaign, job.campaign_id) if job.campaign_id else None
    if campaign and all(campaign.brief.get(k) == brief.get(k) for k in ("prompt", "product", "facts_id", "rights")):
        scope = campaign.brief.get("reference_ids", [])
        if not scope or set(brief["reference_ids"]).issubset(scope):
            return scope
    return None


def job_batches(db, settings, job, initial, video):
    """Inspect unseen candidates first, within the original scope, even when resuming an older job."""
    from .media_search import campaign_scores, library_media

    brief = job.brief
    live = settings.wants_live(job.platform, db.get(Account, job.platform)) or brief["execution_mode"] == "live"
    public = brief["rights"] != "not_public"
    drive_only = job.platform in REAL_MEDIA_ONLY

    def eligible(a):
        return (a and not a.removed_at and real_asset(a, live=live, public=public)
                and (not drive_only or a.tags.get("google_drive_file_id")))

    reviewed = {key: index for index, key in enumerate(brief.get("reviewed_original_ids", []))}
    first = [a for a in initial if eligible(a)]
    fresh = [a for a in first if a.id not in reviewed]
    if fresh:
        yield fresh
    seen = {a.id for a in first}
    scope = _job_scope(db, job)
    pool = []
    if scope:
        remaining = [i for i in dict.fromkeys(scope) if i not in seen]
        pool = [a for a in load_assets(db, remaining) if eligible(a)]
    elif scope == []:
        pool = [a for a in library_media(db, product=brief["product"], public=public, live=live,
                                        drive_only=drive_only, limit=None) if a.id not in seen]
    pool.extend(a for a in first if a.id in reviewed)
    scores = campaign_scores(settings, brief["product"] + " " + brief["prompt"], pool)
    pool.sort(key=lambda a: (reviewed.get(a.id, -1), -scores[a.id]))
    yield from candidate_batches((a for a in pool if a.media["mime"].startswith("image/")),
                                 (a for a in pool if a.media["mime"].startswith("video/")), video)


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
