"""Keep AI creation records, review and disclosure consistent."""


def requires_ai_review(asset):
    # The generator/prompt fields describe AI creation, not camera/encoder metadata.
    # Legacy uploads could retain these fields while incorrectly selecting company.
    return bool(
        asset.source != "company"
        or (asset.generator or "").strip()
        or (asset.prompt or "").strip()
        or (asset.tags or {}).get("asset_type") == "ai_concept"
        or (asset.media or {}).get("embedded_provenance", {}).get("ai_declared", False)
    )


def source_error(source, generator, prompt, tags):
    if source not in {"company", "ai_generated", "ai_edited"}:
        return "Invalid asset source"
    if source == "company" and (generator or prompt or tags.get("asset_type") == "ai_concept"):
        return "AI creation details require Source: AI-generated or AI-edited. Choose the matching source."
    if source != "company" and (not generator or not prompt):
        return "AI assets require generator/model and prompt provenance"
    return ""


def asset_source_error(asset):
    if asset.source == "company" and (asset.media or {}).get("embedded_provenance", {}).get("ai_declared"):
        return "Embedded IPTC metadata declares AI media; correct its Source and creation record"
    return source_error(
        asset.source, (asset.generator or "").strip(), (asset.prompt or "").strip(), asset.tags or {}
    )


def asset_type(asset):
    # Source alone establishes an AI concept, but never establishes a verified
    # installation or product family. Preserve explicit classifications.
    return (asset.tags or {}).get("asset_type") or (
        "ai_concept" if asset.source in {"ai_generated", "ai_edited"} else None
    )


def real_drive_media_error(db, assets):
    """Why media can't go to a real-media-only platform such as Pinterest, or "" when it can.

    Every asset and everything it was made from must be real (no AI generation or editing), and the
    originals must have been imported from Google Drive. Resized platform copies count as the original,
    and the logo a video ends on is the company's own artwork, not Drive media.
    """
    from .asset_edit import ancestry

    sources, missing = ancestry(db, assets)
    if missing:
        return "Media this post was made from is missing from the asset library."
    if any(requires_ai_review(a) for a in sources):
        return "Pinterest only posts real photos and videos, and this media was made or edited with AI."
    logos = {((a.tags or {}).get("end_card") or {}).get("asset_id") for a in sources}
    originals = [a for a in sources if "rendition" not in (a.tags or {}) and a.id not in logos]
    if not originals or not all((a.tags or {}).get("google_drive_file_id") for a in originals):
        return (
            "Pinterest only posts photos and videos imported from Google Drive. Import them through "
            "Asset library → Automatic Google Drive imports, then create the post from them."
        )
    return ""
