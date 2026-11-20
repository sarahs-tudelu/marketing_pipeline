import io
from datetime import timedelta

import av
import pytest
from PIL import Image
from pydantic import SecretStr
from test_product_rules import sheet as sheet
from test_real_edit import close, footage, jpeg
from test_reference_grounding import choice
from test_runway_media import brief_for
from test_scene_generation import Provider

from social import reference_grounding, scene_generation
from social.clip_replace import parts
from social.db import now
from social.media_studio import process_jobs, queue_jobs
from social.models import Asset, MediaCampaign
from social.prompting import OriginalCut
from social.providers import Claude
from social.storage import ingest, storage
from social.video_segments import with_original_cuts


def campaign_video(db, settings, **kwargs):
    brief = brief_for(db, settings, operation="mixed", prompt="Open the reel with our original pool pergola, then animate it.",
                      duration=10, **kwargs)
    campaign = MediaCampaign(name="Poolside", brief=brief.model_dump())
    db.add(campaign)
    db.flush()
    [job] = queue_jobs(db, settings, brief, campaign=campaign, sequence=1)
    db.commit()
    return job


@pytest.mark.parametrize("video", [False, True])
def test_mixed_campaign_finds_and_includes_original_in_ai_video(db, settings, house_logo, video):
    data = footage(seconds=3, sound=False) if video else jpeg((180, 40, 90))
    pool = ingest(db, settings, data, "pool-pergola." + ("mp4" if video else "jpg"), rights="public",
                  rights_expire_at=now() + timedelta(days=12))
    for i in range(8):
        ingest(db, settings, jpeg((i, 100, 50)), f"entry-canopy-{i}.jpg", rights="public")
    restricted = ingest(db, settings, jpeg((5, 5, 5)), "pool-pergola.jpg", rights="not_public")
    db.commit()
    job = campaign_video(db, settings)
    assert job.brief["operation"] == "video" and job.brief["reference_ids"] == [pool.id]
    assert job.brief["repair_library_scope"] == []  # A later repair can match footage outside the initial shortlist.
    assert restricted.id not in job.brief["campaign_reference_ids"] + job.brief["original_candidates"]
    for _ in range(5):
        process_jobs(db, settings)
        if job.state == "complete":
            break
    assert job.state == "complete", job.error
    result = db.get(Asset, job.output_asset_id)
    timeline = result.tags["video_sequence"]
    assert [s.get("original_asset_id") for s in timeline] == [pool.id, None]
    assert [s["duration"] for s in timeline] == [2, 5]
    assert result.source == "ai_edited" and pool.id in result.tags["source_asset_ids"]
    assert result.rights_expire_at == pool.rights_expire_at and result.media["duration"] == pytest.approx(10, abs=.1)
    assert result.media["visual_summary"]["summary"]
    assert [p["index"] for p in parts(db, job, {}, {}) if p["kind"] == "clip"] == [1]
    with av.open(io.BytesIO(storage(settings).read(result.storage_key))) as film:
        picture = next(film.decode(video=0)).to_image()
        assert close(picture.getpixel((picture.width // 2, picture.height // 2)),
                     (40, 90, 140) if video else (180, 40, 90))


def test_real_turn_prefers_older_prompt_match(db, settings, house_logo):
    match = ingest(db, settings, jpeg((30, 80, 55)), "IMG_1234.jpg", rights="public")
    match.media = match.media | {"visual_summary": {"schema_version": 1, "asset_checksum": match.checksum,
                                "summary": "A pergola on a rooftop", "simulated": False}}
    for i in range(12):
        ingest(db, settings, jpeg((60, i, 5)), f"walkway-{i}.jpg", rights="public")
    brief = brief_for(db, settings, operation="mixed", prompt="Make a rooftop pergola reel", duration=10)
    campaign = MediaCampaign(name="Rooftops", brief=brief.model_dump())
    db.add(campaign)
    db.flush()
    [job] = queue_jobs(db, settings, brief, campaign=campaign)
    assert job.brief["real_format"] == "video" and job.brief["reference_ids"][0] == match.id


def test_photo_only_campaign_submits_library_photo_then_checked_scene(db, settings, house_logo, sheet, monkeypatch):
    photo = ingest(db, settings, jpeg((150, 110, 80)), "pool-pergola.jpg", rights="public")
    settings.ai_mode, settings.runway_api_key = "live", SecretStr("mock")
    job = campaign_video(db, settings, facts_id=sheet.id)

    def writer(self, instructions, context):
        assert context["campaign_references"] == [photo.id]
        return {"system": "Pergolas", "prompt": "A fixed pergola at dusk.",
                "original_cuts": [{"asset_id": photo.id, "seconds": 2, "start": 0, "placement": "opening"}]}

    monkeypatch.setattr(Claude, "call", writer)
    monkeypatch.setattr("social.prompting._describe", lambda *args: "A pool pergola")
    monkeypatch.setattr(reference_grounding, "select_reference", lambda s, records, ctx: choice(records[0]))
    monkeypatch.setattr(scene_generation, "review_scene", lambda *args: scene_generation.SceneCheck(passed=True, reasons=[]))
    provider = Provider(db, job, settings)
    for _ in range(3):
        process_jobs(db, settings, provider)
        assert job.state != "failed", job.error
    assert [e for e, _ in provider.submitted] == ["text_to_image", "image_to_video"]
    assert provider.submitted[0][1]["referenceImages"][0]["uri"] == "runway://" + photo.id
    plan = reference_grounding.current_plan(job)
    assert plan["scene_check"]["passed"]
    assert provider.submitted[1][1]["promptImage"][0]["uri"] == "runway://" + plan["scene_reference"]["asset_id"]
    assert job.brief["video_segments"][0]["original_asset_id"] == photo.id


def test_private_video_supplies_a_reusable_still_with_ancestry(db, settings, house_logo):
    from social.product_rules import profile_for
    from social.reference_grounding import builder_guidance, candidates, read_reference, validate_record

    clip = ingest(db, settings, footage(seconds=2, sound=False), "pool-pergola.mp4", rights="not_public")
    db.commit()
    job = campaign_video(db, settings, rights="not_public")
    assert job.brief["reference_ids"] == [clip.id]
    profile = profile_for(builder_guidance(db, job), "Pergolas")
    [record] = candidates(db, settings, job, profile)
    assert Image.open(io.BytesIO(read_reference(settings, record))).format == "JPEG"
    still = db.get(Asset, record["asset_id"])
    assert still.tags["source_asset_ids"] == [clip.id] and still.rights == "not_public"
    assert 0 < still.tags["video_reference_frame"]["seconds"] < 2
    assert candidates(db, settings, job, profile) == [record]
    validate_record(db, settings, record, private=True)
    with pytest.raises(ValueError):
        validate_record(db, settings, record)
    clip.removed_at = now()
    with pytest.raises(ValueError):
        validate_record(db, settings, record, private=True)


def test_generic_mixed_campaign_keeps_variety_and_skips_a_too_short_rerender(db, settings, house_logo):
    photo = ingest(db, settings, jpeg((100, 70, 60)), "courtyard.jpg", rights="public")
    ingest(db, settings, footage(seconds=3, sound=False), "courtyard.mp4", rights="public")
    brief = brief_for(db, settings, operation="mixed", prompt="Use our photos and videos of the courtyard.", duration=15)
    campaign = MediaCampaign(name="Courtyard", brief=brief.model_dump())
    db.add(campaign)
    db.flush()
    jobs = [queue_jobs(db, settings, brief, campaign=campaign, sequence=i)[0] for i in (1, 3, 5)]
    assert [job.brief["operation"] for job in jobs] == ["image", "video", "image"]
    assert jobs[1].brief["reference_ids"] == [photo.id]


def test_writer_cannot_insert_an_unknown_or_short_original():
    brief = {"video_segments": [{"duration": 8}], "runway_video_model": "veo3.1"}
    cut = OriginalCut(asset_id="clip", placement="middle", seconds=3, start=2)
    original = {"asset_id": "clip", "kind": "video", "seconds": 4}
    for invalid in ([], [original]):
        with pytest.raises(ValueError, match="does not fit"):
            with_original_cuts(brief, [cut], invalid)
    timeline = with_original_cuts(brief, [cut], [original | {"seconds": 5}])
    assert [(s["start"], s.get("original_asset_id")) for s in timeline] == [(0, None), (3, "clip"), (6, None)]
    photo = OriginalCut(asset_id="photo", placement="opening", seconds=2)
    sources = [original | {"seconds": 5}, {"asset_id": "photo", "kind": "photo"}]
    with pytest.raises(ValueError, match="too little time"):
        with_original_cuts(brief, [photo, cut], sources)
    timeline = with_original_cuts(brief, [photo, cut.model_copy(update={"placement": "closing"})], sources)
    assert [s.get("original_asset_id") for s in timeline] == ["photo", None, "clip"]
