"""Generated media that fails a quality review is made again automatically, up to MEDIA_AUTO_RETRIES."""

from fastapi.testclient import TestClient
from sqlalchemy import select
from test_clip_replace import REASON, held_video, run
from test_idea_plans import facts as facts  # noqa: F401
from test_idea_plans import logo as logo  # noqa: F401
from test_reference_grounding import choice
from test_reference_grounding import photo as photo  # noqa: F401
from test_reference_grounding import reference_job as reference_job  # noqa: F401
from test_reference_grounding import sheet as sheet  # noqa: F401
from test_scene_generation import Provider
from test_scene_generation import video_job as video_job  # noqa: F401

from social import gate
from social import reference_grounding as refs
from social import scene_generation as scenes
from social.app import create_app
from social.clip_replace import auto_regenerate
from social.db import uid
from social.lifecycle import transition
from social.media_studio import process_jobs
from social.models import Asset, CheckResult, MediaJob, Post, Transition
from social.recovery import retry_option


def scene_reviews(monkeypatch, verdicts):
    """The scene review rejects or passes in turn, recording what it was told."""
    seen = []

    def review(settings, plan, guidance, scene):
        seen.append(scenes.earlier_defects(plan))
        passed = verdicts[min(len(seen), len(verdicts)) - 1]
        return scenes.SceneCheck(
            passed=passed, reasons=[] if passed else [f"Roof member floats ({len(seen)})."]
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


def test_unplaced_problem_regenerates_every_clip(db, settings, facts, logo):  # noqa: F811
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
    assert redo.brief["replaces"]["label"] == "all 4 clips" and redo.brief["replaces"]["count"] == 4
    assert not any(s.get("output_asset_id") for s in redo.brief["video_segments"])
    assert all(
        s["scene_retry"]["review"]["reasons"] == ["A support post bends near the roof."]
        for s in redo.brief["video_segments"]
    )
    with TestClient(create_app(settings)) as client:
        queue = client.get("/admin/media-queue").text
    assert "Regenerating all 4 clips of a" in queue and "The other clips" not in queue
    run(db, settings, redo)
    old = [s["output_asset_id"] for s in job.brief["video_segments"]]
    new = [s["output_asset_id"] for s in redo.brief["video_segments"]]
    assert len(set(new)) == 4 and not set(new) & set(old)


def test_auto_retries_off(db, settings, facts, logo):  # noqa: F811
    settings.media_auto_retries = 0
    with TestClient(create_app(settings)) as client:
        _, _, post = held_video(db, settings, client, facts, logo)
    assert auto_regenerate(db, settings, post) is None
