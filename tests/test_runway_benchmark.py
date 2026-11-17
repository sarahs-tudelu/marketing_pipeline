import json

import pytest

from social.provider_errors import ProviderError
from social.runway_benchmark import (
    VARIANTS,
    Budget,
    Journal,
    Variant,
    adapted_payload,
    lettering_mask,
    setup_trial,
    summarize,
)


def image_payload():
    return {"model": "gen4_image", "promptText": "Use @ref0 for construction.", "ratio": "720:1280",
            "seed": 10, "referenceImages": [{"uri": "runway://image0", "tag": "ref0"}]}


@pytest.mark.parametrize("name", ["gemini_pro", "nano_banana", "gpt_sunburst"])
def test_alternative_image_contracts(name):
    source = image_payload()
    result = adapted_payload("text_to_image", source, VARIANTS[name])
    assert "seed" not in result
    assert source["seed"] == 10
    assert result["ratio"] != source["ratio"]
    if name == "gpt_sunburst":
        assert "@ref" not in result["promptText"]
        assert "image 1" in result["promptText"]
        assert result["referenceImages"] == [{"uri": "runway://image0"}]


def test_baseline_request_is_unchanged():
    source = image_payload()
    assert adapted_payload("text_to_image", source, VARIANTS["baseline"]) == source


@pytest.mark.parametrize("name", ["veo", "veo_fast"])
def test_veo_no_audio_and_identical_endpoints(name):
    source = {"model": "gen4.5", "promptImage": "runway://reviewedscene", "duration": 6,
              "seed": 4, "ratio": "720:1280", "outputFormat": "mp4"}
    result = adapted_payload("image_to_video", source, VARIANTS[name])
    assert result["audio"] is False
    assert "outputFormat" not in result
    assert result["promptImage"] == [
        {"uri": source["promptImage"], "position": p} for p in ("first", "last")]
    with pytest.raises(ValueError, match="4, 6 or 8"):
        adapted_payload("image_to_video", source | {"duration": 5}, VARIANTS[name])


def test_three_reference_limit_counts_style():
    payload = image_payload() | {"referenceImages": [{"uri": f"runway://ref{i}"} for i in range(4)]}
    with pytest.raises(ValueError, match="Too many"):
        adapted_payload("text_to_image", payload, Variant(references=3))


def test_mask_polarity_and_protected_pixels():
    mask = lettering_mask((100, 100), [{"left": .1, "top": .1, "right": .2, "bottom": .2}])
    assert mask.getpixel((15, 15)) == 0
    assert mask.getpixel((50, 50)) == 255
    for boxes in [[], [{"left": 0, "top": 0, "right": 1, "bottom": 1}],
                  [{"left": .8, "top": .1, "right": .2, "bottom": .2}]]:
        with pytest.raises(ValueError):
            lettering_mask((100, 100), boxes)


def test_ideogram_payload_is_not_changed_to_gen4():
    payload = {"model": "ideogram_4_5", "quality": "high", "maskUri": "data:image/png;base64,a",
               "preciseEdit": True, "referenceImages": [{"uri": "runway://scene"}]}
    assert adapted_payload("text_to_image", payload, VARIANTS["text_repair"]) == payload


def test_shared_budget_blocks_before_paid_submission(tmp_path):
    path = tmp_path / "budget.json"
    Budget(path, 16).reserve("one", image_payload())
    Budget(path, 16).reserve("two", image_payload())
    with pytest.raises(ProviderError, match="limit"):
        Budget(path, 16).reserve("three", image_payload())
    assert len(json.loads(path.read_text())) == 2


def test_failed_run_has_no_time_to_acceptable(db, tmp_path):
    journal = Journal(tmp_path)
    journal.event("trial_started")
    result = summarize(journal, db, "failed", "scene rejected", 25)
    assert result["time_to_acceptable_seconds"] is None
    assert result["elapsed_seconds"] == 25
    assert result["published"] is False


def test_setup_isolated_and_refuses_existing_trial(settings, tmp_path):
    import hashlib
    import io

    from PIL import Image
    from sqlalchemy import select

    from social.models import Account, MediaJob

    directory = tmp_path / "run"
    directory.mkdir()
    image = io.BytesIO()
    Image.new("RGB", (720, 720), "white").save(image, "PNG")
    file = tmp_path / "logo.png"
    file.write_bytes(image.getvalue())
    manifest = {"facts_path": "config/facts.tudelu-2026-11-16-builder-v1.yaml", "logo_id": "logo",
                "assets": [{"id": "logo", "file": str(file), "checksum": hashlib.sha256(image.getvalue()).hexdigest(),
                            "original_name": "logo.png", "source": "company", "rights": "public"}],
                "brief": {"campaign": "test", "prompt": "A canopy", "operation": "video",
                          "platforms": ["facebook"], "product": "canopies", "rights": "public", "duration": 8}}
    local, engine, db = setup_trial(settings, manifest, directory, VARIANTS["baseline"], 30)
    try:
        assert local.storage_backend == "local"
        assert not local.live_platforms and not local.auto_advance
        assert db.scalar(select(MediaJob)).brief["seed"] == 30
        assert all(a.credentials == "" for a in db.scalars(select(Account)))
        assert not db.scalars(select(MediaJob)).first().post_id
    finally:
        db.close()
        engine.dispose()
    with pytest.raises(ValueError, match="Trial exists"):
        setup_trial(settings, manifest, directory, VARIANTS["baseline"], 30)


def test_veo_preflight_rejects_five_second_footage_before_setup(settings, tmp_path):
    with pytest.raises(ValueError, match="plus the logo card"):
        setup_trial(settings, {"brief": {"duration": 8}}, tmp_path, VARIANTS["veo"], 1)
    assert not (tmp_path / "trial.db").exists()


def test_pending_repair_prevents_automatic_new_purchase(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from social import recovery
    from social.runway_benchmark import experiment_hooks

    calls = []
    def original(*a):
        calls.append(True)

    monkeypatch.setattr(recovery, "auto_retry", original)
    with experiment_hooks(VARIANTS["text_repair"], None, Journal(tmp_path), []):
        assert recovery.auto_retry(None, None, SimpleNamespace(brief={"benchmark_repair_pending": "task"})) is False
        assert calls == []
    assert recovery.auto_retry is original


def test_matched_branch_clears_old_tasks_without_changing_source(settings, tmp_path):
    import hashlib
    import io

    from PIL import Image
    from sqlalchemy import select

    from social.models import MediaJob
    from social.runway_benchmark import setup_branch

    source = tmp_path / "source"
    source.mkdir()
    stream = io.BytesIO()
    Image.new("RGB", (720, 720), "white").save(stream, "PNG")
    logo = tmp_path / "logo.png"
    logo.write_bytes(stream.getvalue())
    manifest = {"facts_path": "config/facts.tudelu-2026-11-16-builder-v1.yaml", "logo_id": "logo",
                "assets": [{"id": "logo", "file": str(logo), "checksum": hashlib.sha256(stream.getvalue()).hexdigest(),
                            "original_name": "logo.png", "source": "company", "rights": "public"}],
                "brief": {"campaign": "test", "prompt": "A canopy", "operation": "video",
                          "platforms": ["facebook"], "product": "canopies", "rights": "public", "duration": 8}}
    _, engine, db = setup_trial(settings, manifest, source, VARIANTS["baseline"], 30)
    job = db.scalar(select(MediaJob))
    plan = {"scene_check": {"passed": True}, "scene_reference": {"asset_id": "logo"},
            "prompt": "Locked camera", "references": []}
    job.brief = job.brief | {"video_segments": [
        {"index": 0, "duration": 5, "reference_plan": plan, "output_asset_id": "old-video"}]}
    job.task_id, job.state = "already-paid-task", "running"
    db.commit()
    Journal(source).event("trial_started")
    target = tmp_path / "branch"
    target.mkdir()
    branch_manifest = manifest | {"branch_from": str(source), "branch_approved": True, "source_seed": 30,
                                  "brief": manifest["brief"] | {"duration": 9}}
    _, branch_engine, branch_db = setup_branch(settings, branch_manifest, target, VARIANTS["nano_veo"])
    try:
        new_job = branch_db.scalar(select(MediaJob))
        assert new_job.task_id == "" and new_job.state == "queued"
        assert not new_job.source_asset_ids and new_job.post_id is None
        assert new_job.brief["video_segments"][0]["duration"] == 6
        assert "output_asset_id" not in new_job.brief["video_segments"][0]
        db.refresh(job)
        assert job.task_id == "already-paid-task" and job.state == "running"
    finally:
        branch_db.close()
        branch_engine.dispose()
        db.close()
        engine.dispose()


def test_matched_veo_preflight_runs_before_copying_source(settings, tmp_path):
    manifest = {"brief": {"duration": 8}, "branch_from": str(tmp_path / "missing-source")}
    with pytest.raises(ValueError, match="plus the logo card"):
        setup_trial(settings, manifest, tmp_path, VARIANTS["nano_veo"], 1)
    assert not (tmp_path / "trial.db").exists()
