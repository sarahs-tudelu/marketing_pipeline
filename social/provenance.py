"""Keep AI creation records, review and disclosure consistent."""


def requires_ai_review(asset):
    # The generator/prompt fields describe AI creation, not camera/encoder metadata.
    # Legacy uploads could retain these fields while incorrectly selecting company.
    return bool(
        asset.source != "company"
        or (asset.generator or "").strip()
        or (asset.prompt or "").strip()
        or (asset.tags or {}).get("asset_type") == "ai_concept"
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
    return source_error(
        asset.source, (asset.generator or "").strip(), (asset.prompt or "").strip(), asset.tags or {}
    )


def asset_type(asset):
    # Source alone establishes an AI concept, but never establishes a verified
    # installation or product family. Preserve explicit classifications.
    return (asset.tags or {}).get("asset_type") or (
        "ai_concept" if asset.source in {"ai_generated", "ai_edited"} else None
    )
