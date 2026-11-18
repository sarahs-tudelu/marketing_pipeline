"""What's in the asset library: what each file is, filters and sorting for the library page and the media
pickers, counts of what's there, and deleting files.

Deleting a file that nothing uses removes its record and its stored bytes for good. A file that posts or
media jobs still use, or that the app made copies from, is marked removed instead: it leaves the library
and is never chosen for new posts, while the posts that use it keep it. Removed files are listed under
Source: Deleted and can be restored. A deleted Google Drive import isn't imported again unless an admin
asks for it on the Drive import panel.
"""

import json
import math
from urllib.parse import urlencode

from sqlalchemy import select

from .db import now
from .models import Asset, AssetEdit, DriveImport, MediaCampaign, MediaJob, Post
from .monitoring import event
from .storage import load_assets, storage

KINDS = {"photo": "Photos", "video": "Videos", "audio": "Audio"}
FORMATS = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WebP",
    "image/gif": "GIF",
    "video/mp4": "MP4",
    "video/quicktime": "MOV",
    "video/webm": "WebM",
    "audio/mpeg": "MP3",
    "audio/wav": "WAV",
    "audio/mp4": "M4A",
    "audio/ogg": "OGG",
    "audio/flac": "FLAC",
}
ORIGINS = {
    "drive": "Google Drive imports",
    "upload": "Uploads",
    "ai_generated": "AI-generated",
    "ai_edited": "AI-edited",
    "copy": "Copies made for posts",
}
# The library page's other Source choices.
VIEWS = {"": "Library files", "all": "Everything, with post copies", "deleted": "Deleted, kept for posts"}
SORTS = {
    "newest": "Newest first",
    "oldest": "Oldest first",
    "name": "Name A–Z",
    "longest": "Longest first",
    "largest": "Largest file first",
}
RIGHTS = {"public": "Public", "not_public": "Not public"}
NO_PRODUCT = "none"  # The Product choice for files tagged company-wide or not tagged.
PAGE_SIZE = 60
MAX_DELETE = 200


def mime(asset):
    return (asset.media or {}).get("mime", "")


def kind(asset):
    return {"image": "photo", "video": "video", "audio": "audio"}.get(mime(asset).split("/")[0], "")


def is_copy(asset):
    """A platform copy, edited post or edit segment the app made from other files."""
    tags = asset.tags or {}
    return bool(tags.get("rendition") or tags.get("edit_interval"))


def origin(asset):
    if is_copy(asset):
        return "copy"
    if asset.source in ("ai_generated", "ai_edited"):
        return asset.source
    return "drive" if (asset.tags or {}).get("google_drive_file_id") else "upload"


def products(asset):
    """The product tags on a file; "company" (company-wide) isn't a product."""
    tags = asset.tags or {}
    value = tags.get("product_family") or tags.get("product")
    names = value if isinstance(value, list) else [value] if value else []
    return [n for n in (str(v).strip() for v in names) if n and n.casefold() != "company"]


def product_options(assets):
    """Each product tag in use once, in its first spelling, sorted."""
    seen = {}
    for asset in assets:
        for name in products(asset):
            seen.setdefault(name.casefold(), name)
    return sorted(seen.values(), key=str.casefold)


def duration(asset):
    return float((asset.media or {}).get("duration") or 0)


def clock(seconds):
    minutes, seconds = divmod(round(seconds), 60)
    return f"{minutes}:{seconds:02d}"


def label(asset):
    """'Video · MOV · 0:42' or 'Photo · JPEG · 4032 × 3024'."""
    found = kind(asset)
    name = FORMATS.get(mime(asset), mime(asset).split("/")[-1].upper())
    if found in ("video", "audio"):
        return f"{found.title()} · {name} · {clock(duration(asset))}"
    media = asset.media or {}
    return f"Photo · {name} · {media.get('width', 0)} × {media.get('height', 0)}"


def search_text(asset):
    """What a search box matches: the name, products, campaign and the description of what it shows."""
    tags = asset.tags or {}
    summary = (asset.media or {}).get("visual_summary")
    parts = [asset.original_name.replace("_", " ").replace("-", " "), " ".join(products(asset)),
             str(tags.get("campaign", ""))]
    if isinstance(summary, dict):
        parts += [str(summary.get("summary", ""))[:600], " ".join(map(str, summary.get("visible_text", [])[:5]))]
    return " ".join(parts).casefold()


def choices(params):
    """The library page's filters from its query string, each checked against the allowed values."""

    def get(key):
        return " ".join(str(params.get(key, "") or "").split())[:200]

    page = get("page")
    return {
        "type": get("type") if get("type") in KINDS or get("type") in FORMATS else "",
        "product": get("product"),
        "source": get("source") if get("source") in ORIGINS or get("source") in VIEWS else "",
        "rights": get("rights") if get("rights") in RIGHTS else "",
        "q": get("q"),
        "sort": get("sort") if get("sort") in SORTS else "newest",
        "page": max(1, int(page)) if page.isdigit() else 1,
    }


def matches(asset, chosen):
    source = chosen["source"]
    if (asset.removed_at is not None) != (source == "deleted"):
        return False
    if source in ORIGINS and origin(asset) != source:
        return False
    if source == "" and is_copy(asset):
        return False
    wanted = chosen["type"]
    if (wanted in KINDS and kind(asset) != wanted) or (wanted in FORMATS and mime(asset) != wanted):
        return False
    names = [n.casefold() for n in products(asset)]
    if chosen["product"] == NO_PRODUCT and names:
        return False
    if chosen["product"] not in ("", NO_PRODUCT) and chosen["product"].casefold() not in names:
        return False
    if chosen["rights"] and asset.rights != chosen["rights"]:
        return False
    text = search_text(asset) if chosen["q"] else ""
    return all(word in text for word in chosen["q"].casefold().split())


def ordered(assets, sort):
    key, reverse = {
        "newest": (lambda a: a.created_at, True),
        "oldest": (lambda a: a.created_at, False),
        "name": (lambda a: a.original_name.casefold(), False),
        "longest": (duration, True),
        "largest": (lambda a: (a.media or {}).get("bytes") or 0, True),
    }[sort]
    return sorted(assets, key=key, reverse=reverse)


def summary(assets):
    """The library check: files by type and where they came from, and what keeps them out of posts."""
    files = [a for a in assets if a.removed_at is None and not is_copy(a)]
    rows = []
    for key, name in KINDS.items():
        found = [a for a in files if kind(a) == key]
        rows.append({
            "kind": key,
            "label": name,
            "total": len(found),
            "origins": {o: n for o in ORIGINS if (n := sum(origin(a) == o for a in found))},
            "not_public": sum(a.rights == "not_public" for a in found),
            "undescribed": 0 if key == "audio" else sum(not (a.media or {}).get("visual_summary") for a in found),
        })
    return {
        "rows": rows,
        "copies": sum(a.removed_at is None and is_copy(a) for a in assets),
        "deleted": sum(a.removed_at is not None for a in assets),
    }


def library_page(db, params, *, allowed=None):
    """The asset library page: one page of the files that match its filters, and the library check."""
    chosen = choices(params)
    every = [a for a in db.scalars(select(Asset).order_by(Asset.created_at.desc())) if allowed is None or allowed(a)]
    found = ordered([a for a in every if matches(a, chosen)], chosen["sort"])
    pages = max(1, math.ceil(len(found) / PAGE_SIZE))
    page = min(chosen["page"], pages)
    present = {mime(a) for a in every if a.removed_at is None}

    def link(number):
        values = {k: v for k, v in chosen.items() if v and k != "page" and not (k == "sort" and v == "newest")}
        return "/admin/assets?" + urlencode(values | ({"page": number} if number > 1 else {})) + "#library"

    return {
        "previous": link(page - 1) if page > 1 else "",
        "next": link(page + 1) if page < pages else "",
        "filtered": any(v for k, v in chosen.items() if k not in ("page", "sort")),
        "filters": chosen | {"page": page},
        "assets": found[(page - 1) * PAGE_SIZE : page * PAGE_SIZE],
        "total": len(found),
        "first": (page - 1) * PAGE_SIZE + 1,
        "page": page,
        "pages": pages,
        "products": product_options([a for a in every if a.removed_at is None]),
        "formats": {k: v for k, v in FORMATS.items() if k in present},
        "check": summary(every),
    }


def picker(assets, *, matches=None):
    """A media picker's entries, with the products, types and sources present among them.

    `matches` maps an asset ID to (rank, why) for files a library search found; they sort first.
    """
    matches = matches or {}
    entries = [
        {
            "asset": a,
            "kind": kind(a),
            "origin": origin(a),
            "products": products(a),
            "text": search_text(a)[:400],
            "created": a.created_at.isoformat() if a.created_at else "",
            "seconds": round(duration(a), 1),
            "label": label(a),
            "match": matches.get(a.id),
        }
        for a in assets
    ]
    return {
        "entries": entries,
        "products": product_options(assets),
        "kinds": {k: v for k, v in KINDS.items() if any(e["kind"] == k for e in entries)},
        "origins": {k: v for k, v in ORIGINS.items() if any(e["origin"] == k for e in entries)},
        "matched": any(e["match"] for e in entries),
        "sorts": ({"match": "Best match first"} if any(e["match"] for e in entries) else {})
        | {k: v for k, v in SORTS.items() if k != "largest"},
    }


def pickable(asset):
    """Library files a person can choose for a post: not deleted, a post copy or the logo."""
    return asset.removed_at is None and not is_copy(asset) and not (asset.tags or {}).get("brand_logo")


def _uses(db):
    """Everything that can point at a file: posts' media, media jobs, and other files' tags."""
    posts = [set(ids or []) for ids in db.scalars(select(Post.asset_ids))]
    jobs = [
        json.dumps([sources, output, brief], default=str)
        for sources, output, brief in db.execute(
            select(MediaJob.source_asset_ids, MediaJob.output_asset_id, MediaJob.brief)
        )
    ]
    tags = [(i, json.dumps(t or {}, default=str)) for i, t in db.execute(select(Asset.id, Asset.tags))]
    return posts, jobs, tags


def used_by(asset_id, uses):
    """What still uses a file, as phrases such as '2 posts'; empty when nothing does."""
    posts, jobs, tags = uses

    def phrase(count, noun):
        return f"{count} {noun}{'' if count == 1 else 's'}"

    found = []
    if count := sum(asset_id in ids for ids in posts):
        found.append(phrase(count, "post"))
    if count := sum(asset_id in text for text in jobs):
        found.append(phrase(count, "media job"))
    if count := sum(asset_id in text for other, text in tags if other != asset_id):
        found.append(phrase(count, "file") + " made from it")
    return found


def _drop_from_campaigns(db, asset_id):
    """Recurring campaigns stop using a deleted file. Returns the names of the ones that did."""
    changed = []
    for campaign in db.scalars(select(MediaCampaign)):
        brief = campaign.brief or {}
        if asset_id not in json.dumps(brief, default=str):
            continue
        updated = {
            key: [v for v in value if v != asset_id] if isinstance(value, list) else "" if value == asset_id else value
            for key, value in brief.items()
        }
        if updated != brief:
            campaign.brief = updated
            changed.append(campaign.name)
    return changed


def delete_assets(db, settings, asset_ids, actor):
    """Delete library files: for good when nothing uses them, otherwise kept for the posts that do."""
    ids = list(dict.fromkeys(str(i) for i in asset_ids))[:MAX_DELETE]
    targets = [a for a in load_assets(db, ids) if a is not None and a.removed_at is None]
    if not targets:
        raise ValueError("Choose the files to delete")
    uses = _uses(db)
    deleted, kept, campaigns = [], 0, set()
    for asset in targets:
        campaigns.update(_drop_from_campaigns(db, asset.id))
        imports = db.scalars(select(DriveImport).where(DriveImport.asset_id == asset.id)).all()
        for record in imports:
            # Not imported again on the next scan; the Drive panel can bring it back.
            record.status, record.error, record.retry_at = "deleted", "", None
        if used_by(asset.id, uses):
            asset.removed_at, asset.removed_by = now(), actor
            kept += 1
            continue
        for edit in db.scalars(select(AssetEdit).where(AssetEdit.asset_id == asset.id)):
            db.delete(edit)
        for record in imports:
            record.asset_id = None
        db.flush()  # The history and import rows point at the file; clear them before deleting it.
        deleted.append(asset.storage_key)
        db.delete(asset)
    db.commit()
    failed = 0
    keys = set(deleted)
    # Identical files share stored bytes; keep them while another record uses them.
    shared = set(db.scalars(select(Asset.storage_key).where(Asset.storage_key.in_(keys)))) if keys else set()
    for key in keys - shared:
        try:
            storage(settings).delete(key)
        except Exception as exc:  # noqa: BLE001 - The records are gone; the bytes are only unreachable.
            failed += 1
            event("asset_file_delete_failed", error_type=type(exc).__name__)
    event("assets_deleted", deleted=len(deleted), kept=kept, actor=actor)
    return {"deleted": len(deleted), "kept": kept, "campaigns": sorted(campaigns), "storage_failed": failed}


def restore_assets(db, asset_ids):
    """Bring deleted files that were kept for posts back into the library."""
    restored = 0
    for asset in load_assets(db, list(dict.fromkeys(str(i) for i in asset_ids))[:MAX_DELETE]):
        if asset is None or asset.removed_at is None:
            continue
        asset.removed_at, asset.removed_by = None, ""
        for record in db.scalars(
            select(DriveImport).where(DriveImport.asset_id == asset.id, DriveImport.status == "deleted")
        ):
            record.status = "imported"
        restored += 1
    db.commit()
    return restored


def outcome(result):
    """The notice shown after deleting files."""

    def files(count):
        return f"{count} file{'' if count == 1 else 's'}"

    parts = []
    if result["deleted"]:
        parts.append(f"Deleted {files(result['deleted'])}.")
    if result["kept"]:
        parts.append(
            f"{files(result['kept'])} {'is' if result['kept'] == 1 else 'are'} used by posts or media jobs, so "
            f"{'it was' if result['kept'] == 1 else 'they were'} removed from the library and from new posts but "
            "kept for those posts. Choose Source: Deleted to see or restore them."
        )
    if result["campaigns"]:
        # The notice travels in the session cookie, so long lists are shortened.
        names = [n[:60] for n in result["campaigns"][:5]]
        more = len(result["campaigns"]) - len(names)
        parts.append(
            "These recurring campaigns no longer use them: " + ", ".join(names) + (f" and {more} more" if more else "") + "."
        )
    if result["storage_failed"]:
        parts.append(
            f"{files(result['storage_failed'])} couldn't be removed from media storage; check its delete "
            "permission. Their library records are gone."
        )
    return " ".join(parts)
