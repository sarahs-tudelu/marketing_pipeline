"""Recurring campaigns kept in a file and saved the way the Recurring campaign form saves them: paused."""

from pathlib import Path

import yaml
from pydantic import ValidationError
from sqlalchemy import select

from .media_studio import CreativeBrief, check_real_media, validate_sources
from .models import FactSheet, MediaCampaign

# The Recurring campaign form's fields. The facts version is chosen for the whole file, and every campaign
# creates image or video posts on the automatic schedule.
FIELDS = {
    "campaign",
    "prompt",
    "operation",
    "platforms",
    "product",
    "kind",
    "rights",
    "owner",
    "reference_ids",
    "audio_reference_ids",
    "audio_prompt",
    "duration",
    "website",
    "interval_hours",
}


def approved_facts(db, version=""):
    """The named facts version, or the newest non-demo one as the form preselects."""
    if version:
        sheet = db.scalar(select(FactSheet).where(FactSheet.version == version))
        if not sheet:
            raise ValueError(f"Facts version {version} isn't in the database. Import it under Approved facts first.")
    else:
        sheets = db.scalars(select(FactSheet).order_by(FactSheet.created_at.desc()))
        sheet = next((s for s in sheets if not s.document.get("demo_only")), None)
        if not sheet:
            raise ValueError("No approved non-demo facts version exists. Import one under Approved facts first.")
    if sheet.document.get("demo_only"):
        raise ValueError(f"Facts version {sheet.version} is demo-only; campaigns need approved facts.")
    return sheet


def _reason(exc):
    if isinstance(exc, ValidationError):
        return " ".join(
            f"{'.'.join(map(str, e['loc'])) + ': ' if e['loc'] else ''}"
            + str(e["msg"]).removeprefix("Value error, ").rstrip(".")
            + "."
            for e in exc.errors()[:3]
        )
    return str(exc)


def load_campaigns(path):
    """The file's facts version and its campaigns, each with the file's defaults filled in."""
    try:
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{path} isn't valid YAML: {exc}") from exc
    if not isinstance(document, dict):
        document = {"campaigns": None}
    defaults = document.get("defaults") or {}
    entries = document.get("campaigns")
    if not isinstance(defaults, dict) or not isinstance(entries, list) or not all(
        isinstance(e, dict) for e in entries
    ):
        raise ValueError(f"{path} needs a `campaigns:` list of briefs and optional `defaults:` fields")
    return str(document.get("facts_version") or ""), [defaults | entry for entry in entries]


def import_campaigns(db, settings, path):
    """Save each campaign in the file that passes the form's checks; existing names are left unchanged."""
    version, entries = load_campaigns(path)
    sheet = approved_facts(db, version)
    existing = set(db.scalars(select(MediaCampaign.name)))
    results = []
    for values in entries:
        name = str(values.get("campaign", "")).strip()
        if name in existing:
            results.append({"campaign": name, "status": "exists"})
            continue
        try:
            if unknown := sorted(set(values) - FIELDS):
                raise ValueError(f"Unknown fields: {', '.join(unknown)}")
            brief = CreativeBrief(**values, facts_id=sheet.id, create_posts=True, workflow="automatic")
            if brief.operation == "audio":
                raise ValueError("Recurring campaigns must create image/video posts")
            check_real_media(db, settings, brief, validate_sources(db, brief))
        except ValueError as exc:
            db.rollback()
            results.append({"campaign": name, "status": "failed", "reason": _reason(exc)})
            continue
        campaign = MediaCampaign(name=brief.campaign, brief=brief.model_dump(), enabled=False)
        db.add(campaign)
        db.commit()
        existing.add(name)
        results.append({"campaign": name, "status": "created", "id": campaign.id})
    return {"facts_version": sheet.version, "campaigns": results}
