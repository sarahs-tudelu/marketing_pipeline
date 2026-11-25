"""Generated media that fails a quality review is made again automatically, up to MEDIA_AUTO_RETRIES."""

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select
from test_clip_replace import REASON, held_video, run
from test_idea_plans import facts as facts  # noqa: F401
from test_idea_plans import logo as logo  # noqa: F401
from test_reference_grounding import canopy_design, choice
from test_reference_grounding import photo as photo  # noqa: F401
from test_reference_grounding import reference_job as reference_job  # noqa: F401
from test_reference_grounding import sheet as sheet  # noqa: F401
from test_scene_generation import Provider
from test_scene_generation import video_job as video_job  # noqa: F401

from social import gate, pipeline
from social import reference_grounding as refs
from social import scene_generation as scenes
from social.app import create_app
from social.clip_replace import auto_regenerate
from social.db import uid
from social.lifecycle import transition
from social.media_studio import process_jobs
from social.models import Asset, CheckResult, MediaJob, Post, RequestRecord, Transition
from social.recovery import retry_option
from social.scheduler import schedule


def scene_reviews(monkeypatch, verdicts, components=("roof", "support", "trim")):
    """The scene review rejects or passes in turn, recording what it was told."""
    seen = []

    def review(settings, plan, guidance, scene):
        seen.append(scenes.earlier_defects(plan))
        passed = verdicts[min(len(seen), len(verdicts)) - 1]
        return scenes.SceneCheck(
            passed=passed, reasons=[] if passed else [f"Roof member floats ({len(seen)})."],
            defect_components=[] if passed else [components[(len(seen)-1) % len(components)]],
        )

    monkeypatch.setattr(scenes, "review_scene", review)
    return seen


def test_rejected_scene_is_corrected_automatically_until_it_passes(db, settings, video_job, monkeypatch):  # noqa: F811
    settings.media_auto_retries = 3
    seen = scene_reviews(monkeypatch, [False, False, True])
    planned = []

    def choose(s, records, context):
        planned.append(context)
        return choice(records[0]).model_copy(update={"scene_requirements": ["Every roof member connected"]})

    monkeypatch.setattr(refs, "select_reference", choose)
    provider = Provider(db, video_job, settings)
    for _ in range(15):
        process_jobs(db, settings, provider)
        if video_job.state in {"rendering", "failed"}:
            break
    assert video_job.state == "rendering", video_job.error
    assert [e for e, _ in provider.submitted] == ["text_to_image"] * 3 + ["image_to_video"]
    automatic = [f for f in video_job.brief["failed_tasks"] if f.get("automatic")]
    assert len(automatic) == 2 and all(f["stage"] == "scene" for f in automatic)
    # Each correction is planned from the previous rejection, and the next review checks for it.
    assert planned[1]["scene_retry"]["review"]["reasons"] == ["Roof member floats (1)."]
    assert planned[2]["scene_retry"]["review"]["reasons"] == ["Roof member floats (2)."]
    assert seen == [[], ["Roof member floats (1)."], ["Roof member floats (2)."]]


def test_video_feedback_allows_first_still_correction_and_then_stops_repeated_defect(
    db, settings, video_job, monkeypatch,  # noqa: F811
):
    settings.media_auto_retries = 3
    motion_issue = "The housing moves during retraction."
    # Stored by correction() when a finished clip is replaced. These findings describe motion.
    video_job.brief = video_job.brief | {"scene_retry": {
        "source": "held_post", "review": {"passed": False, "reasons": [motion_issue]},
        "video_check": {"reasons": [motion_issue]},
    }}
    db.commit()
    width_issue = "The partition is too narrow to reach both guides."
    seen = []

    def inspect(settings, plan, guidance, scene):
        seen.append(scenes.earlier_defects(plan))
        return scenes.SceneCheck(passed=False, reasons=[width_issue], defect_components=["partition"])

    monkeypatch.setattr(scenes, "review_scene", inspect)
    monkeypatch.setattr(refs, "select_reference", lambda s, r, c: choice(r[0]).model_copy(
        update={"scene_requirements": ["The partition meets both guides."]}
    ))
    provider = Provider(db, video_job, settings)
    for _ in range(12):
        process_jobs(db, settings, provider)
        if video_job.state == "failed":
            break
    assert seen == [[motion_issue], [motion_issue, width_issue]]
    assert [endpoint for endpoint, _ in provider.submitted] == ["text_to_image", "text_to_image"]
    assert video_job.state == "failed" and "already targeted for correction (partition)" in video_job.error


def test_automatic_scene_corrections_stop_at_the_limit(db, settings, video_job, monkeypatch):  # noqa: F811
    settings.media_auto_retries = 2
    scene_reviews(monkeypatch, [False])
    monkeypatch.setattr(
        refs,
        "select_reference",
        lambda s, r, c: choice(r[0]).model_copy(
            update={"scene_requirements": ["Every roof member connected"]}
        ),
    )
    provider = Provider(db, video_job, settings)
    for _ in range(10):
        process_jobs(db, settings, provider)
    assert [e for e, _ in provider.submitted] == ["text_to_image"] * 3
    assert video_job.state == "failed"
    assert "already made 2 corrected scenes automatically" in video_job.error
    # A person can still correct it.
    assert retry_option(video_job)["kind"] == "scene"


def failing_check(details):
    def evaluate(db, settings, post, **kwargs):
        check = CheckResult(
            post_id=post.id, run_id=uid(), name="vision_review", passed=False, details=details
        )
        db.add(check)
        db.flush()
        return [check], [db.get(Asset, i) for i in post.asset_ids]

    return evaluate


@pytest.mark.parametrize("pending_delivery", [False, True])
def test_pre_publish_visual_hold_queues_image_correction_unless_delivery_is_uncertain(
    db, settings, post, reference_job, monkeypatch, pending_delivery,  # noqa: F811
):
    reference_job.output_asset_id = post.asset_ids[0]
    reference_job.post_id, reference_job.state = post.id, "complete"
    reference_job.brief = reference_job.brief | {"depicted_system": "Canopies"}
    db.commit()
    gate.run_gate(db, settings, post)
    at = schedule(db, settings, post)
    if pending_delivery:
        db.add(RequestRecord(
            post_id=post.id, step="publish", state="pending", request={}, irreversible=True, dry_run=True,
        ))
        db.commit()
    reason = "Remove the visible joint lines across the canopy underside."
    monkeypatch.setattr(pipeline, "evaluate", failing_check({"reasons": [reason]}))
    pipeline.publish_due(db, settings, at=at)
    pipeline.publish_due(db, settings, at=at)
    assert post.status == "held" and not post.remote_id
    replacements = list(db.scalars(select(MediaJob).where(MediaJob.id != reference_job.id)))
    if pending_delivery:
        assert replacements == []
        return
    assert len(replacements) == 1
    redo = replacements[0]
    assert redo.state == "queued" and redo.brief["operation"] == "image"
    assert redo.brief["replaces"]["auto_attempt"] == 1
    corrected = "The entire canopy underside is a continuous seamless surface."

    def replan(settings, records, context):
        assert context["scene_retry"]["review"]["reasons"] == [reason]
        return canopy_design().model_copy(update={"scene_requirements": [corrected]})

    class ImageProvider:
        def submit(self, endpoint, payload):
            assert endpoint == "text_to_image" and corrected in payload["promptText"]
            return "corrected-image-task"

    settings.ai_mode, settings.runway_api_key = "live", SecretStr("test")
    monkeypatch.setattr(refs, "candidates", lambda *args: [])
    monkeypatch.setattr(refs, "select_reference", replan)
    process_jobs(db, settings, ImageProvider())
    assert redo.state == "running" and redo.task_id == "corrected-image-task", redo.error


def test_held_post_regenerates_flagged_clip_automatically_up_to_the_limit(
    db,
    settings,
    facts,
    logo,
    monkeypatch,  # noqa: F811
):
    settings.media_auto_retries = 2
    details = {
        "passed": False,
        "reasons": [REASON],
        "garbled_text": True,
        "misused_logos": False,
        "unexpected_people": False,
        "visible_claims": [],
    }
    monkeypatch.setattr(gate, "evaluate", failing_check(details))
    with TestClient(create_app(settings)) as client:
        _, job, post = held_video(db, settings, client, facts, logo)
    posts = [post]
    for attempt in (1, 2):
        details["video_defects"] = [{"kind": "opening", "component": "text" if attempt == 1 else "finish",
                                    "second": 15, "correction": "Use a plain fascia.", "needs_operation": False,
                                    "clean_start": 0, "clean_end": 0}]
        current = posts[-1]
        transition(db, current, "generated", "Recheck")
        gate.run_gate(db, settings, current)
        assert current.status == "held"
        redo = db.scalars(select(MediaJob).order_by(MediaJob.created_at.desc())).first()
        correction = redo.brief["video_segments"][2]["scene_retry"]
        assert correction["prompt_edited"] is False
        assert "operator_prompt" not in correction and "operator_edits" not in correction
        assert correction["review"]["reasons"] == [REASON]
        assert redo.brief["replaces"] | {"post_id": None, "media_job_id": None} == {
            "post_id": None,
            "media_job_id": None,
            "part": "2",
            "label": "clip 3",
            "clip_index": 2,
            "count": 1,
            "requested_by": "pipeline",
            "auto_attempt": attempt,
            "auto_limit": 2,
        }
        with TestClient(create_app(settings)) as client:
            page = client.get(f"/posts/{current.id}").text
            assert (
                f"Started automatically because the visual check found problems (attempt {attempt} of 2)"
                in page
            )
        run(db, settings, redo)
        db.refresh(current)
        assert current.status == "failed"
        reason = db.scalars(select(Transition.reason).where(Transition.post_id == current.id)).all()[-1]
        assert reason.endswith("after clip 3 was regenerated automatically")
        posts.append(db.get(Post, redo.post_id))
    # The third failure in a row waits for a person.
    last = posts[-1]
    transition(db, last, "generated", "Recheck")
    gate.run_gate(db, settings, last)
    assert last.status == "held" and len(db.scalars(select(MediaJob)).all()) == 3
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/posts/{last.id}").text
    assert "The app already regenerated this 2 times automatically" in page
    # A person's own regeneration starts a new run of automatic attempts.
    assert auto_regenerate(db, settings, last) is None


def test_unplaced_problem_holds_without_spending(db, settings, facts, logo):  # noqa: F811
    with TestClient(create_app(settings)) as client:
        _, job, post = held_video(db, settings, client, facts, logo)
    db.add(
        CheckResult(
            post_id=post.id,
            run_id="unplaced",
            name="vision_review",
            passed=False,
            details={"passed": False, "reasons": ["A support post bends near the roof."]},
        )
    )
    db.commit()
    redo = auto_regenerate(db, settings, post)
    assert redo is None
    assert "timestamped" in job.brief["repair_hold"]
    assert len(db.scalars(select(MediaJob)).all()) == 1


def test_auto_retries_off(db, settings, facts, logo):  # noqa: F811
    settings.media_auto_retries = 0
    with TestClient(create_app(settings)) as client:
        _, _, post = held_video(db, settings, client, facts, logo)
    assert auto_regenerate(db, settings, post) is None


def test_scene_that_the_shot_itself_makes_impossible_waits_for_a_person(
    db, settings, video_job, monkeypatch  # noqa: F811
):
    settings.media_auto_retries = 3
    monkeypatch.setattr(scenes, "review_scene", lambda *args: scenes.SceneCheck(
        passed=False,
        reasons=["Hanger rods run below the canopy."],
        request_conflict="The shot asks for hanger rods below the canopy.",
    ))
    monkeypatch.setattr(
        refs,
        "select_reference",
        lambda s, r, c: choice(r[0]).model_copy(update={"scene_requirements": ["Rods below the roof"]}),
    )
    provider = Provider(db, video_job, settings)
    for _ in range(8):
        process_jobs(db, settings, provider)
    # One scene, then no paid automatic corrections that would fail the same way.
    assert [e for e, _ in provider.submitted] == ["text_to_image"]
    assert video_job.state == "failed"
    assert "asks for something Tudelu doesn't make: The shot asks for hanger rods" in video_job.error
    assert retry_option(video_job)["kind"] == "scene"


def test_held_post_whose_shot_conflicts_is_not_regenerated(db, settings, facts, logo):  # noqa: F811
    with TestClient(create_app(settings)) as client:
        _, _, post = held_video(db, settings, client, facts, logo)
    db.add(
        CheckResult(
            post_id=post.id,
            run_id="conflict",
            name="vision_review",
            passed=False,
            details={
                "passed": False,
                "reasons": ["Pergola louvers rotate open at 4 seconds."],
                "request_conflict": "The shot asks for opening pergola louvers.",
            },
        )
    )
    db.commit()
    assert auto_regenerate(db, settings, post) is None
