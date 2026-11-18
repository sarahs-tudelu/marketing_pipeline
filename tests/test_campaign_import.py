"""The campaign file: briefs that fit the approved facts, saved paused like the Recurring campaign form."""

import io
from pathlib import Path

import pytest
import yaml
from PIL import Image
from sqlalchemy import select

from social.campaigns import FIELDS, import_campaigns, load_campaigns
from social.config import profiles
from social.facts import load_facts, save_facts
from social.media_studio import CreativeBrief, queue_jobs
from social.models import MediaCampaign
from social.storage import ingest

CAMPAIGNS = Path("config/campaigns.yaml")
FACTS = Path("config/facts.tudelu-2026-11-17-builder-v2.yaml")


@pytest.fixture
def facts(db):
    sheet = load_facts(db, FACTS)
    db.commit()
    return sheet


def drive_photo(db, settings, name="terrace.jpg"):
    stream = io.BytesIO()
    Image.new("RGB", (1000, 1500), "#7d9d89").save(stream, format="JPEG")
    tags = {"google_drive_file_id": f"drive-{name}", "google_drive_folder_id": "folder"}
    asset = ingest(db, settings, stream.getvalue(), name, rights="public", tags=tags)
    db.commit()
    return asset


def write(tmp_path, document):
    path = tmp_path / "campaigns.yaml"
    path.write_text(yaml.safe_dump(document, allow_unicode=True), encoding="utf-8")
    return path


def test_shipped_campaigns_fit_the_approved_facts(settings, facts):
    _, entries = load_campaigns(CAMPAIGNS)
    document = facts.document
    products = {p["id"]: p for p in document["products"]}
    banned = [b.casefold() for b in document["banned_phrases"]]
    names = [e["campaign"] for e in entries]
    assert len(names) == len(set(names)) >= 5
    for entry in entries:
        assert set(entry) <= FIELDS, entry["campaign"]
        brief = CreativeBrief(**entry, facts_id=facts.id)
        # Captions draw on the product's facts and company facts, so the product must have its own.
        assert brief.product in products and products[brief.product].get("status") != "coming_soon"
        assert any(f["product"] == brief.product for f in document["facts"]), brief.product
        assert not [b for b in banned if b in brief.prompt.casefold()], brief.campaign
    # Every operation, content style and platform the pipeline supports for campaigns is represented.
    assert {e["operation"] for e in entries} == {"real", "mixed", "image", "video"}
    assert {e["kind"] for e in entries} == {"promotional", "non_promotional", "mixed"}
    assert {p for e in entries for p in e["platforms"]} == set(profiles(settings))


def test_shipped_campaigns_stay_within_each_platforms_daily_target(settings):
    _, entries = load_campaigns(CAMPAIGNS)
    for platform, profile in profiles(settings).items():
        per_day = sum(24 / e["interval_hours"] for e in entries if platform in e["platforms"])
        assert 0 < per_day <= profile.target_per_day, platform


def test_import_saves_campaigns_paused_once(db, settings, facts):
    drive_photo(db, settings)
    result = import_campaigns(db, settings, CAMPAIGNS)

    assert result["facts_version"] == facts.version
    assert {c["status"] for c in result["campaigns"]} == {"created"}
    saved = db.scalars(select(MediaCampaign)).all()
    assert len(saved) == len(result["campaigns"])
    for campaign in saved:
        assert not campaign.enabled and campaign.name == campaign.brief["campaign"]
        assert campaign.brief["facts_id"] == facts.id
        assert campaign.brief["workflow"] == "automatic" and campaign.brief["create_posts"]
        CreativeBrief.model_validate(campaign.brief, context={"stored": True})

    # Running it again leaves the saved campaigns, and any enabled since, as they are.
    saved[0].enabled = True
    db.commit()
    again = import_campaigns(db, settings, CAMPAIGNS)
    assert {c["status"] for c in again["campaigns"]} == {"exists"}
    assert db.scalar(select(MediaCampaign).where(MediaCampaign.id == saved[0].id)).enabled
    assert len(db.scalars(select(MediaCampaign)).all()) == len(saved)


def test_imported_campaigns_queue_their_batches(db, settings, facts, house_logo):
    drive_photo(db, settings)
    import_campaigns(db, settings, CAMPAIGNS)
    for campaign in db.scalars(select(MediaCampaign)).all():
        brief = CreativeBrief.model_validate(campaign.brief, context={"stored": True})
        operations = set()
        # Four batches cover the real/AI alternation and the AI format rotation.
        for sequence in range(4):
            jobs = queue_jobs(db, settings, brief, campaign=campaign, sequence=sequence, snapshot={})
            assert sorted(j.platform for j in jobs) == sorted(brief.platforms), campaign.name
            for job in jobs:
                operations.add(job.brief["operation"])
                assert job.brief["kind"] in {"promotional", "non_promotional"}
                if job.platform == "tiktok":
                    assert job.brief["operation"] != "image" and job.brief.get("real_format") != "photo"
                if job.platform == "pinterest":
                    assert job.brief["operation"] == "real"
            db.commit()
        if brief.operation == "mixed":
            assert {"real", "image", "video"} <= operations, campaign.name
        else:
            assert operations == {brief.operation}, campaign.name


def test_import_reports_campaigns_it_cannot_save(db, settings, facts, tmp_path):
    path = write(tmp_path, {
        "defaults": {"rights": "public"},
        "campaigns": [
            {"campaign": "Real work", "prompt": "Our projects", "operation": "real", "platforms": ["instagram"]},
            {"campaign": "Pins", "prompt": "Ideas", "operation": "mixed", "kind": "mixed",
             "platforms": ["pinterest"]},
            {"campaign": "Typo", "prompt": "Shade", "operation": "image", "platforms": ["x"], "interval": 48},
            {"campaign": "Too often", "prompt": "Shade", "operation": "image", "platforms": ["x"],
             "interval_hours": 12},
            {"campaign": "TikTok stills", "prompt": "Shade", "operation": "image", "platforms": ["tiktok"]},
            {"campaign": "Shade", "prompt": "Pergolas at dusk", "operation": "image", "product": "pergolas",
             "platforms": ["linkedin"]},
        ],
    })
    result = {c["campaign"]: c for c in import_campaigns(db, settings, path)["campaigns"]}

    # With no real photos or videos in the library, real project and Pinterest campaigns can't start.
    assert "no real photos or videos fit" in result["Real work"]["reason"]
    assert "Google Drive" in result["Pins"]["reason"]
    assert result["Typo"]["reason"] == "Unknown fields: interval"
    assert "interval_hours" in result["Too often"]["reason"]
    assert "TikTok requires video" in result["TikTok stills"]["reason"]
    assert result["Shade"]["status"] == "created"
    assert [c.name for c in db.scalars(select(MediaCampaign))] == ["Shade"]


def test_import_needs_approved_facts(db, settings, tmp_path):
    path = write(tmp_path, {"campaigns": []})
    with pytest.raises(ValueError, match="No approved non-demo facts"):
        import_campaigns(db, settings, path)
    demo = save_facts(db, yaml.safe_load(FACTS.read_text(encoding="utf-8")) | {"version": "demo", "demo_only": True})
    with pytest.raises(ValueError, match="demo-only"):
        import_campaigns(db, settings, write(tmp_path, {"facts_version": demo.version, "campaigns": []}))
    with pytest.raises(ValueError, match="isn't in the database"):
        import_campaigns(db, settings, write(tmp_path, {"facts_version": "missing", "campaigns": []}))
    with pytest.raises(ValueError, match="needs a `campaigns:` list"):
        import_campaigns(db, settings, write(tmp_path, {"campaigns": {"name": "not a list"}}))
