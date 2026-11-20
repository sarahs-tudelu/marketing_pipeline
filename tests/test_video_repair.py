"""Repairs of a passed scene use the failed take, the approved still, or a local edit as diagnosed."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from test_reference_grounding import photo, reference_job, sheet  # noqa: F401
from test_scene_clip_replace import held, hold, run  # noqa: F401
from test_scene_generation import video_job  # noqa: F401

from social.app import create_app
from social.clip_replace import auto_regenerate
from social.db import now
from social.lifecycle import transition
from social.models import Asset, Post, UsageRecord
from social.usage import repair_costs


def defect(kind, **changes):
    return {
        "kind": kind,
        "second": 11.2,
        "component": "logo" if kind == "detail" else "partition",
        "correction": "Keep a plain unmarked panel continuously engaged with its housing.",
        "clean_start": 0,
        "clean_end": 0,
        "needs_operation": False,
    } | changes


def diagnose(db, post, item, library=()):
    hold(
        db,
        post,
        [item["correction"]],
        video_defects=[item],
        problem_seconds=[item["second"]],
        repair_library_ids=list(library),
    )


@pytest.mark.parametrize(
    "kind,route,steps",
    [
        ("motion", "motion", ["image_to_video"]),
        ("detail", "aleph", ["text_to_image", "video_to_video"]),
        ("opening", "scene", ["text_to_image", "image_to_video"]),
        ("trim", "trim", []),
    ],
)
def test_repair_worker_and_repeat_hold(db, settings, held, kind, route, steps):  # noqa: F811
    original, post, provider = held
    settings.media_auto_retries = 3
    item = defect("motion", clean_start=6, clean_end=11) if kind == "trim" else defect(kind)
    diagnose(db, post, item)
    before = len(provider.submitted)
    redo = auto_regenerate(db, settings, post)
    assert redo.brief["repair_routes"] == {"1": route}
    assert auto_regenerate(db, settings, post) is None
    run(db, settings, redo, provider)
    assert redo.state == "complete", redo.error
    calls = provider.submitted[before:]
    assert [e for e, _ in calls] == steps
    old = original.brief["video_segments"]
    new = redo.brief["video_segments"]
    assert new[0] == old[0] and redo.brief["soundtrack_asset_id"] in original.source_asset_ids
    if kind == "detail":
        assert calls[0][1]["referenceImages"][0]["uri"].startswith("data:image/jpeg;")
        payload = calls[1][1]
        assert payload["model"] == "aleph2" and payload["videoUri"] == "runway://" + old[1]["output_asset_id"]
        assert payload["keyframes"][0]["seconds"] == pytest.approx(5.2)
        assert payload["keyframes"][0]["uri"] == "runway://" + new[1]["repair"]["keyframe"]["asset_id"]
        assert db.get(Asset, new[1]["output_asset_id"]).generator == "runway/aleph2"
        for task in redo.task_history:
            db.add(
                UsageRecord(
                    provider="runway",
                    purpose="repair",
                    request_id=task["task_id"],
                    estimated=False,
                    media_job_id=redo.id,
                    units={"credits": 4 if task["stage"] == "repair_keyframe" else 168},
                )
            )
        redo.brief = redo.brief | {"repair_verdict": True}
        db.commit()
        assert repair_costs(db, now() - timedelta(days=1))[0]["average"] == 172
    elif kind == "motion":
        assert (
            calls[0][1]["promptImage"][0]["uri"]
            == "runway://" + old[1]["reference_plan"]["scene_reference"]["asset_id"]
        )
        assert item["correction"] in calls[0][1]["promptText"]
    elif kind == "trim":
        assert new[1]["output_asset_id"] == old[1]["output_asset_id"]
        assert new[1]["playback_rate"] == pytest.approx(5 / 6)
        assert db.get(Asset, redo.output_asset_id).media["duration"] == pytest.approx(15, abs=0.1)
    latest = db.get(Post, redo.post_id)
    transition(db, latest, "held", "Review")
    diagnose(db, latest, item)
    assert auto_regenerate(db, settings, latest) is None
    assert "human review" in redo.brief["repair_hold"]


def test_uncertain_shows_frames_and_reference_without_new_job(db, settings, held):  # noqa: F811
    job, post, provider = held
    settings.media_auto_retries = 3
    diagnose(db, post, defect("uncertain"))
    assert auto_regenerate(db, settings, post) is None
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/posts/{post.id}").text
        assert "Compare clip 2" in page and "Product construction reference" in page
        response = client.get(f"/posts/{post.id}/repair-evidence/2")
        assert response.status_code == 200 and response.content.startswith(b"\xff\xd8")
        assert client.get(f"/posts/{post.id}/repair-evidence/1").status_code == 404


def test_repeated_defect_uses_cleared_library_photo_but_demonstration_needs_footage(
    db, settings, held, photo  # noqa: F811
):
    job, post, provider = held
    settings.media_auto_retries = 3
    job.brief = job.brief | {
        "video_segments": [
            s | {"repair_history": [{"route": "scene", "components": ["roof"]}]}
            for s in job.brief["video_segments"]
        ]
    }
    d = defect("motion", component="roof", library_asset_id=photo.id, needs_operation=True)
    diagnose(db, post, d, [photo.id])
    assert auto_regenerate(db, settings, post) is None
    diagnose(db, post, d | {"needs_operation": False}, [photo.id])
    redo = auto_regenerate(db, settings, post)
    assert redo.brief["video_segments"][1]["original_asset_id"] == photo.id
    before = len(provider.submitted)
    run(db, settings, redo, provider)
    assert redo.state == "complete", redo.error
    assert len(provider.submitted) == before
    assert db.get(Asset, redo.output_asset_id).source == "ai_edited"


def test_uploaded_video_edit_uses_failed_take_and_recorded_source(db, settings, held):  # noqa: F811
    from social.seed import seed_media
    from social.storage import ingest

    job, post, provider = held
    settings.media_auto_retries = 2
    raw = ingest(db, settings, seed_media(duration=6)[1], "library.mp4", rights="public")
    job.brief = job.brief | {
        "operation": "video_edit",
        "reference_ids": [raw.id],
        "video_segments": [
            {k: v for k, v in s.items() if k != "reference_plan"} for s in job.brief["video_segments"]
        ],
    }
    diagnose(db, post, defect("detail"))
    redo = auto_regenerate(db, settings, post)
    before = len(provider.submitted)
    run(db, settings, redo, provider)
    assert redo.state == "complete", redo.error
    shot = redo.brief["video_segments"][1]
    edit = provider.submitted[before + 1][1]
    assert edit["videoUri"] == "runway://" + job.brief["video_segments"][1]["output_asset_id"]
    assert shot["reference_plan"]["references"][0]["video_asset_id"] == raw.id


def test_same_component_stops_correcting_before_animation(db, settings, video_job, monkeypatch):  # noqa: F811
    from test_auto_retry import scene_reviews
    from test_reference_grounding import choice
    from test_scene_generation import Provider

    from social import reference_grounding as refs

    settings.media_auto_retries = 3
    scene_reviews(monkeypatch, [False], ("support",))
    monkeypatch.setattr(
        refs,
        "select_reference",
        lambda s, r, c: choice(r[0]).model_copy(
            update={"scene_requirements": ["Keep both attachments visible"]}
        ),
    )
    runway = Provider(db, video_job, settings)
    run(db, settings, video_job, runway)
    assert video_job.state == "failed" and "saved scene" in video_job.error
    assert [e for e, _ in runway.submitted] == ["text_to_image", "text_to_image"]
