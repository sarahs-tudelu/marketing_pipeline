"""Audited edits to library metadata; original media bytes remain immutable."""

import hashlib
import json
from copy import deepcopy
from datetime import timezone
from pathlib import PurePath

from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from .lifecycle import PostConflict, lock_busy, lock_post, transition
from .models import Asset, AssetEdit, MediaJob, Post
from .provenance import requires_ai_review, source_error
from .rights import owner_value, usage_rights

FIELDS = (
    "original_name",
    "source",
    "rights",
    "owner",
    "rights_expire_at",
    "generator",
    "prompt",
    "tags",
    "customer_permission",
    "permission_reference",
)
PROTECTED_TAGS = frozenset(
    {
        "source_asset_ids",
        "source_checksum",
        "reference_provenance",
        "google_drive_file_id",
        "google_drive_folder_id",
        "imported_bucket_key",
        "runway_task_id",
        "media_job_id",
        "rendition",
        "end_card",
        "video_segment",
        "video_sequence",
        "replaces",
    }
)


class AssetEditConflict(ValueError):
    pass


def snapshot(asset):
    result = {name: deepcopy(getattr(asset, name)) for name in FIELDS}
    if result["rights_expire_at"]:
        result["rights_expire_at"] = result["rights_expire_at"].isoformat()
    return result


def revision(asset):
    return hashlib.sha256(json.dumps(snapshot(asset), sort_keys=True).encode()).hexdigest()


def form_values(asset):
    values = snapshot(asset)
    values["tags"] = json.dumps(
        {
            k: v
            for k, v in asset.tags.items()
            if k not in PROTECTED_TAGS | {"brand_logo", "real_customer_content"}
        },
        indent=2,
    )
    values["rights_expire_at"] = values["rights_expire_at"] or ""
    values["customer_permission"] = "on" if asset.customer_permission else ""
    values["brand_logo"] = "on" if asset.tags.get("brand_logo") else ""
    values["real_customer_content"] = "on" if asset.tags.get("real_customer_content") else ""
    values["generator"], values["prompt"] = asset.generator or "", asset.prompt or ""
    values["revision"] = revision(asset)
    return values


def ancestry(db, assets):
    """Resolve original sources for rights checks, including nested renditions."""
    found = {a.id: a for a in assets}
    queue = list(assets)
    missing = set()
    for asset in queue:
        for key in (asset.tags or {}).get("source_asset_ids", []):
            if key in found or key in missing:
                continue
            source = db.get(Asset, key)
            if source is None:
                missing.add(key)
            else:
                found[key] = source
                queue.append(source)
    return list(found.values()), sorted(missing)


def descendants(db, asset_id):
    found = {asset_id}
    candidates = db.scalars(select(Asset)).all()
    while True:
        added = {a.id for a in candidates if found.intersection((a.tags or {}).get("source_asset_ids", []))}
        if added <= found:
            return found
        found.update(added)


def edit_asset(db, asset, values, *, original_name, expected_revision, actor):
    """Lock before reading current metadata; an old browser form cannot overwrite another save."""
    try:
        if db.get_bind().dialect.name == "sqlite":
            db.execute(update(Asset).where(Asset.id == asset.id).values(owner=Asset.owner))
        else:
            db.execute(select(Asset.id).where(Asset.id == asset.id).with_for_update(nowait=True))
        db.refresh(asset)
    except OperationalError as exc:
        if lock_busy(exc):
            raise AssetEditConflict(
                "This media is being updated. Reload its details and try again."
            ) from None
        raise
    if expected_revision != revision(asset):
        raise AssetEditConflict(
            "This media changed after you opened it. Reload the page before saving again."
        )
    name = original_name.strip()
    if not name or len(name) > 255 or any(c in name for c in "/\\\x00\r\n"):
        raise ValueError("Enter a filename of 1–255 characters without path separators")
    if PurePath(name).suffix.lower() != PurePath(asset.original_name).suffix.lower():
        raise ValueError("Keep the original file extension when changing its name")
    values = deepcopy(values)
    values["original_name"] = name
    values["rights"] = usage_rights(values["rights"])
    values["owner"] = owner_value(values["owner"])
    values["generator"] = (values.get("generator") or "").strip() or None
    values["prompt"] = (values.get("prompt") or "").strip() or None
    values["permission_reference"] = values.get("permission_reference", "").strip()
    if values["generator"] and len(values["generator"]) > 200:
        raise ValueError("AI model / generator must be 200 characters or fewer")
    if not isinstance(values["tags"], dict):
        raise ValueError("Tags must be a JSON object")
    for key in PROTECTED_TAGS:
        if key in values["tags"] and values["tags"][key] != asset.tags.get(key):
            raise ValueError("Imported source links and generation records cannot be changed here")
    values["tags"] = {k: v for k, v in values["tags"].items() if k not in PROTECTED_TAGS} | {
        k: v for k, v in asset.tags.items() if k in PROTECTED_TAGS
    }
    if error := source_error(values["source"], values["generator"], values["prompt"], values["tags"]):
        raise ValueError(error)
    if values["source"] == "company" and asset.media.get("embedded_provenance", {}).get("ai_declared"):
        raise ValueError(
            "Embedded metadata declares AI media. Choose AI-generated or AI-edited and supply its creation details."
        )
    if values["source"] != "company" and not values["tags"].get("asset_type"):
        values["tags"]["asset_type"] = "ai_concept"
    if not values["tags"].get("product_family") and values["tags"].get("product"):
        values["tags"]["product_family"] = values["tags"]["product"]
    if values["customer_permission"] and not values["permission_reference"]:
        raise ValueError("Customer permission requires an evidence reference")
    expiry = values.get("rights_expire_at")
    if expiry and expiry.tzinfo:
        values["rights_expire_at"] = expiry.astimezone(timezone.utc).replace(tzinfo=None)
    before = snapshot(asset)
    after = deepcopy(values)
    if after["rights_expire_at"]:
        after["rights_expire_at"] = after["rights_expire_at"].isoformat()
    if before == after:
        return 0
    approval_changed = any(before[k] != after[k] for k in FIELDS if k != "original_name")
    affected = []
    if approval_changed:
        ids = descendants(db, asset.id)
        for job in db.scalars(
            select(MediaJob).where(MediaJob.state.in_(["submitting", "running", "rendering"]))
        ):
            refs = (
                set(job.source_asset_ids or [])
                | set(job.brief.get("reference_ids", []))
                | set(job.brief.get("audio_reference_ids", []))
                | {job.brief.get("end_card_asset_id")}
            )
            if refs & ids:
                raise AssetEditConflict(
                    "This media is being used to generate content. Wait for generation to finish before editing its details."
                )
        affected = [
            p
            for p in db.scalars(
                select(Post)
                .where(Post.status.not_in(["published", "failed", "deleted", "awaiting_manual"]))
                .order_by(Post.id)
            )
            if ids.intersection(p.asset_ids)
        ]
        for post in affected:
            try:
                lock_post(db, post)
            except PostConflict:
                raise AssetEditConflict(
                    "A post using this media is being processed. Wait for it to finish, then reload these details."
                ) from None
            if post.status == "publishing":
                raise AssetEditConflict(
                    "A post using this media is being published. Try again after delivery finishes."
                )
    for key in FIELDS:
        setattr(asset, key, values[key])
    for post in affected:
        post.approved_digest = ""
        post.ai_disclosure = any(requires_ai_review(db.get(Asset, key)) for key in post.asset_ids)
        if post.status in {"generated", "checked", "approved", "scheduled"}:
            transition(db, post, "held", f"Media details changed for {asset.id}; recheck required", actor)
            post.scheduled_at, post.next_attempt_at = None, None
    db.add(AssetEdit(asset_id=asset.id, actor=actor[:200], before=before, after=after))
    db.flush()
    return len(affected)
