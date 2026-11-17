import io
import json

import pytest
from PIL import Image
from pydantic import SecretStr
from test_reference_grounding import choice
from test_reference_grounding import photo as photo
from test_reference_grounding import reference_job as reference_job
from test_reference_grounding import sheet as sheet

from social import reference_grounding as refs
from social import scene_generation as scenes
from social.media_studio import process_jobs, resume_job
from social.models import Asset
from social.provider_errors import ProviderError
from social.providers import OpenAI
from social.recovery import retry_option, retry_step
from social.seed import seed_media
from social.storage import storage

REAL_REVIEW = scenes.review_scene
REAL_SELECTOR = refs.select_reference


@pytest.fixture
def video_job(db, settings, reference_job, photo, monkeypatch):
    job = reference_job
    job.brief = job.brief | {
        "operation": "video",
        "duration": 6,
        "audio_prompt": "",
        "prompt": "White pergola in a stone courtyard, then a Wood Look pergola at a brick cafe.",
        "visual_direction": "Warm soft daylight, natural grade, 35mm lens, slow left-to-right tracking.",
    }
    settings.ai_mode, settings.runway_api_key = "live", SecretStr("test")
    # These tests cover the manual correction a person makes once automatic retries are used up.
    settings.media_auto_retries = 0
    monkeypatch.setattr(refs, "select_reference", lambda s, r, c: choice(r[0]))
    monkeypatch.setattr(scenes, "review_scene", lambda *args: scenes.SceneCheck(passed=True, reasons=[]))
    db.commit()
    return job


class Provider:
    def __init__(self, db, job, settings):
        self.db, self.job, self.settings = db, job, settings
        self.submitted, self.uploaded = [], []
        stream = io.BytesIO()
        Image.new("RGB", (720, 1280), "blue").save(stream, format="PNG")
        self.still = stream.getvalue()

    def reference(self, asset):
        self.uploaded.append(asset)
        return "runway://" + asset.id

    def submit(self, endpoint, payload):
        from sqlalchemy.orm import Session

        with Session(self.db.get_bind()) as other:
            saved = other.get(type(self.job), self.job.id)
            assert saved.state == "submitting"
            assert saved.brief["active_generation"] == ("scene" if endpoint == "text_to_image" else "visual")
        self.submitted.append((endpoint, payload))
        return "task-" + str(len(self.submitted))

    def task(self, task_id):
        return {"status": "SUCCEEDED", "output": [task_id]}

    def download(self, task_id):
        endpoint = self.submitted[int(task_id.split("-")[1]) - 1][0]
        return self.still if endpoint == "text_to_image" else seed_media(duration=6)[1]


@pytest.mark.parametrize("legacy", [False, True])
def test_saved_job_models_survive_environment_changes(db, settings, video_job, legacy):
    if legacy:
        video_job.brief = {
            k: v for k, v in video_job.brief.items()
            if k not in {"runway_image_model", "runway_video_model"}
        }
        db.commit()
    provider = Provider(db, video_job, settings)
    process_jobs(db, settings, provider)
    expected_image = "gen4_image" if legacy else "nano_banana_2.1"
    expected_video = "gen4.5" if legacy else "veo3.1"
    assert provider.submitted[0][1]["model"] == expected_image
    # Change configuration while the paid scene is running, then recover its original task.
    settings.runway_image_model, settings.runway_video_model = "gen4_image", "gen4.5"
    process_jobs(db, settings, provider)
    scene = db.get(Asset, refs.current_plan(video_job)["scene_reference"]["asset_id"])
    assert scene.generator == "runway/" + expected_image
    assert video_job.task_history[0]["model"] == expected_image
    process_jobs(db, settings, provider)
    assert provider.submitted[1][1]["model"] == expected_video


def test_missing_bundled_catalog_can_resume_without_duplicate_submission(
    db, settings, video_job, tmp_path, monkeypatch
):
    def catalog(*args):
        refs.reference_config("website-product-references.yaml")
        return []  # Use the fixture's library photo, with no website request.

    monkeypatch.setattr(refs, "website_references", catalog)
    provider = Provider(db, video_job, settings)
    with monkeypatch.context() as missing:
        missing.setattr(refs, "files", lambda _: tmp_path)
        process_jobs(db, settings, provider)
    assert video_job.state == "failed"
    assert "missing from this application build" in video_job.error
    assert not video_job.task_id and not video_job.task_history and not provider.submitted

    resume_job(video_job)
    db.commit()
    process_jobs(db, settings, provider)
    assert video_job.state == "running", video_job.error
    assert len(provider.submitted) == 1
    assert provider.submitted[0][0] == "text_to_image"
    assert video_job.brief["active_generation"] == "scene"


def test_prompt_led_scene_is_saved_checked_then_animated(db, settings, video_job, photo):
    from fastapi.testclient import TestClient

    from social.app import create_app

    provider = Provider(db, video_job, settings)
    for _ in range(2):
        process_jobs(db, settings, provider)
    plan = refs.current_plan(video_job)
    scene_id = plan["scene_reference"]["asset_id"]
    assert scene_id != photo.id
    assert not video_job.source_asset_ids  # still is not a completed clip
    assert provider.submitted[0][0] == "text_to_image"
    assert provider.submitted[0][1]["ratio"] == "768:1376"
    assert "white" in provider.submitted[0][1]["promptText"]
    assert provider.submitted[0][1]["referenceImages"][0]["uri"] == "runway://" + photo.id
    with TestClient(create_app(settings)) as client:
        page = client.get("/admin/media-queue")
        assert page.status_code == 200 and "View generated scene" in page.text
        assert "Preparing the scene from your prompt" in page.text
        progress = client.get("/api/media-jobs").json()[0]
        assert progress["generation_phase"] == "scene" and progress["completed_clips"] == 0
        assert progress["saved_scenes"] == 1 and progress["retry_count"] == 0
        assert "View latest generated scene" in page.text
        assert f'/admin/assets/{scene_id}/edit' in page.text
    process_jobs(db, settings, provider)
    assert refs.current_plan(video_job)["scene_check"]["passed"]
    assert provider.submitted[1][0] == "image_to_video"
    assert provider.submitted[1][1]["promptImage"] == [
        {"uri": "runway://" + scene_id, "position": p} for p in ("first", "last")]
    process_jobs(db, settings, provider)
    assert video_job.state == "rendering", video_job.error
    clip = db.get(Asset, video_job.source_asset_ids[0])
    assert set(clip.tags["source_asset_ids"]) == {photo.id, scene_id}
    assert [h["stage"] for h in video_job.task_history] == ["scene", "visual"]
    assert storage(settings).read(db.get(Asset, scene_id).storage_key) == provider.still


@pytest.mark.parametrize("segmented", [False, True])
def test_rejected_scene_requires_correction_and_carries_feedback_into_new_scene(
    db, settings, video_job, monkeypatch, segmented
):
    if segmented:
        video_job.brief = video_job.brief | {"video_segments": [{
            "index": 0, "start": 0, "duration": 6, "seed": 123,
            "system": "Pergolas", "prompt": "A white pergola in a courtyard.",
        }]}
        db.commit()
    reviewed, planned = [], []
    reason = "A roof member floats above the frame."

    def review(*args):
        reviewed.append(args)
        return scenes.SceneCheck(passed=len(reviewed) > 1, reasons=[reason] if len(reviewed) == 1 else [])

    monkeypatch.setattr(scenes, "review_scene", review)
    provider = Provider(db, video_job, settings)
    for _ in range(3):
        process_jobs(db, settings, provider)
    saved = refs.current_plan(video_job)["scene_reference"]["asset_id"]
    assert video_job.state == "failed" and "floats" in video_job.error
    assert len(provider.submitted) == 1 and not video_job.task_id
    assert retry_option(video_job)["kind"] == "scene"
    assert retry_option(video_job)["scene_reasons"] == [reason]
    from fastapi.testclient import TestClient

    from social.app import create_app

    with TestClient(create_app(settings)) as client:
        page = client.get("/admin/media-queue")
        assert "Generate corrected scene" in page.text and reason in page.text
        assert "Rejected before animation" in page.text
    with pytest.raises(ValueError, match="Generate corrected scene"):
        resume_job(video_job)
    # Even an already-queued resume cannot get an unchanged rejected scene approved on a second review.
    video_job.state = "queued"
    db.commit()
    process_jobs(db, settings, provider)
    assert (
        len(provider.submitted) == 1 and refs.current_plan(video_job)["scene_reference"]["asset_id"] == saved
    )
    assert len(reviewed) == 1 and video_job.state == "failed"

    def choose(settings, records, context):
        planned.append(context)
        return choice(records[0]).model_copy(update={
            "scene_requirements": ["White aluminum frame", "All roof members physically connected to the frame"]
            if len(planned) > 1 else [],
        })

    monkeypatch.setattr(refs, "select_reference", choose)
    retry_step(db, video_job, "White pergola in a courtyard, all roof members connected.")
    process_jobs(db, settings, provider)
    assert video_job.state == "failed" and "explicit product requirements" in video_job.error
    assert len(provider.submitted) == 1
    assert retry_option(video_job)["scene_reasons"] == [reason]
    retry_step(db, video_job, retry_option(video_job)["prompt"])
    process_jobs(db, settings, provider)
    assert video_job.state == "running", video_job.error
    assert len(provider.submitted) == 2 and not refs.current_plan(video_job).get("scene_reference")
    assert video_job.brief["failed_tasks"][-1]["stage"] == "scene"
    assert video_job.brief["failed_tasks"][-2]["reference_plan"]["scene_reference"]["asset_id"] == saved
    assert planned[0]["scene_retry"]["review"]["reasons"] == [reason]
    assert planned[0]["scene_retry"]["prompt_edited"] is True
    assert planned[1]["scene_retry"]["review"]["reasons"] == [reason]
    assert planned[1]["scene_retry"]["operator_prompt"] == "White pergola in a courtyard, all roof members connected."
    assert "All roof members physically connected" in provider.submitted[1][1]["promptText"]
    assert all(r.id != saved for r in provider.uploaded)  # Rejected image isn't a product/style reference.
    for _ in range(2):
        process_jobs(db, settings, provider)
    assert len(reviewed) == 2
    assert provider.submitted[-1][0] == "image_to_video"
    assert len(planned) == 2  # Checking/animating the replacement must not discard it and replan.
    with TestClient(create_app(settings)) as client:
        page = client.get("/admin/media-queue")
        progress = client.get("/api/media-jobs").json()[0]
        assert progress["saved_scenes"] == 2 and progress["retry_count"] == 2
        assert 'data-job-scenes="2"' in page.text and 'data-job-retries="2"' in page.text
        assert "View saved scene 1" in page.text and "View saved scene 2" in page.text


def test_correction_planner_sees_rejected_pixels_but_runway_only_gets_product_reference(
    db, settings, video_job, photo, monkeypatch
):
    from fastapi.testclient import TestClient

    from social.app import create_app

    monkeypatch.setattr(scenes, "review_scene", lambda *args: scenes.SceneCheck(
        passed=False, reasons=["The frame has a floating roof member."]))
    provider = Provider(db, video_job, settings)
    for _ in range(3):
        process_jobs(db, settings, provider)
    rejected = refs.current_plan(video_job)["scene_reference"]
    previous = refs.current_plan(video_job)["scene_prompt"]
    monkeypatch.setattr(refs, "select_reference", REAL_SELECTOR)
    seen = []

    def response(self, endpoint, payload):
        seen.append(payload)
        assert refs.current_plan(video_job) is None  # retry removes the rejected plan.
        selected = choice(refs.asset_record(photo)).model_copy(update={
            "scene_requirements": ["White aluminum frame with fixed roof members connected at both ends"],
        })
        return {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": selected.model_dump_json()}]}]}

    monkeypatch.setattr(OpenAI, "call", response)
    with TestClient(create_app(settings)) as client:
        token = client.get("/api/session").json()["csrf"]
        result = client.post(f"/forms/media-jobs/{video_job.id}/resubmit", data={"csrf": token, "prompt": previous})
        assert result.status_code == 200
    db.refresh(video_job)
    process_jobs(db, settings, provider)
    assert video_job.state == "running", video_job.error
    content = seen[0]["input"][0]["content"]
    context = json.loads(content[0]["text"].split("Context: ")[1])
    assert context["scene_retry"]["prompt_edited"] is False
    assert context["scene_retry"]["scene"]["asset_id"] == rejected["asset_id"]
    assert len([p for p in content if p["type"] == "input_image"]) == 2
    assert any(p.get("text", "").startswith("REJECTED OUTPUT:") for p in content)
    assert all(r.id != rejected["asset_id"] for r in provider.uploaded)
    assert len(provider.submitted) == 2


def test_provider_failure_during_correction_keeps_visual_feedback(db, settings, video_job, monkeypatch):
    monkeypatch.setattr(scenes, "review_scene", lambda *args: scenes.SceneCheck(
        passed=False, reasons=["The roof is floating."]))
    provider = Provider(db, video_job, settings)
    for _ in range(3):
        process_jobs(db, settings, provider)
    scene = refs.current_plan(video_job)["scene_reference"]
    monkeypatch.setattr(refs, "select_reference", lambda s, r, c: choice(r[0]).model_copy(update={
        "scene_requirements": ["White aluminum pergola with every roof member connected"],
    }))
    retry_step(db, video_job, "Keep the white finish and connect the roof members.")
    process_jobs(db, settings, provider)
    assert video_job.state == "running", video_job.error
    monkeypatch.setattr(provider, "task", lambda _: {"status": "FAILED", "failureCode": "INTERNAL.BAD_OUTPUT.CODE01"})
    process_jobs(db, settings, provider)
    assert video_job.state == "failed"
    retry_step(db, video_job, retry_option(video_job)["prompt"])
    retry = video_job.brief["scene_retry"]
    assert retry["scene"] == scene and retry["review"]["reasons"] == ["The roof is floating."]
    assert retry["operator_prompt"] == "Keep the white finish and connect the roof members."


def test_later_scene_uses_shared_style_without_reusing_its_background(
    db, settings, video_job, photo, monkeypatch
):
    observed = []

    def choose(s, records, context):
        observed.append(context)
        return choice(records[0]).model_copy(
            update={
                "scene_prompt": "Use @ref0 construction. "
                + context["prompt"]
                + ". Match @ref1 lighting only.",
                "creative_changes": context["prompt"],
            }
        )

    monkeypatch.setattr(refs, "select_reference", choose)
    video_job.brief = video_job.brief | {
        "duration": 12,
        "video_segments": [
            {
                "index": i,
                "start": i * 6,
                "duration": 6,
                "seed": i,
                "system": "Pergolas",
                "prompt": prompt,
                "shot": {"system": "Pergolas", "setting": prompt},
            }
            for i, prompt in enumerate(
                ["White pergola in a stone courtyard", "Wood Look pergola at a brick cafe"]
            )
        ],
    }
    db.commit()
    provider = Provider(db, video_job, settings)
    for _ in range(5):
        process_jobs(db, settings, provider)
    assert [e for e, p in provider.submitted] == ["text_to_image", "image_to_video", "text_to_image"], (
        video_job.error
    )
    first = video_job.brief["video_segments"][0]["reference_plan"]
    second = video_job.brief["video_segments"][1]["reference_plan"]
    assert first["visual_direction"] == second["visual_direction"]
    assert first["scene_prompt"] != second["scene_prompt"]
    assert "brick cafe" in second["scene_prompt"]
    assert second["style_reference"]["asset_id"] == first["scene_reference"]["asset_id"]
    # The style scene is untagged: Runway styles the output with it without copying its content.
    assert provider.submitted[2][1]["referenceImages"] == [
        {"tag": "ref0", "uri": "runway://" + photo.id},
        {"uri": "runway://" + first["scene_reference"]["asset_id"]},
    ]
    assert "@ref1" not in provider.submitted[2][1]["promptText"]
    assert observed[1]["style_reference"]["role"] == "style only"
    # Replacing the first clip still has the second clip as its style anchor.
    for _ in range(3):
        process_jobs(db, settings, provider)
    video_job.brief = video_job.brief | {
        "video_segments": [
            {k: v for k, v in clip.items() if k != "output_asset_id"} if clip["index"] == 0 else clip
            for clip in video_job.brief["video_segments"]
        ]
    }
    assert (
        refs.style_reference(video_job)["asset_id"]
        == video_job.brief["video_segments"][1]["reference_plan"]["scene_reference"]["asset_id"]
    )


def test_scene_download_failure_resumes_saved_task_without_new_generation(
    db, settings, video_job, monkeypatch
):
    provider = Provider(db, video_job, settings)
    original = provider.download
    monkeypatch.setattr(
        provider,
        "download",
        lambda *args: (_ for _ in ()).throw(
            ProviderError("Temporary download failure", provider="Runway", category="download")
        ),
    )
    process_jobs(db, settings, provider)
    process_jobs(db, settings, provider)
    assert video_job.state == "failed" and video_job.task_id == "task-1"
    monkeypatch.setattr(provider, "download", original)
    resume_job(video_job)
    db.commit()
    process_jobs(db, settings, provider)
    assert len(provider.submitted) == 1 and refs.current_plan(video_job)["scene_reference"]


def test_runway_internal_scene_failure_needs_explicit_new_task(db, settings, video_job, monkeypatch):
    provider = Provider(db, video_job, settings)
    process_jobs(db, settings, provider)
    assert "seed" not in provider.submitted[0][1]  # Nano Banana does not accept a seed.
    monkeypatch.setattr(provider, "task", lambda _: {"status": "FAILED", "failureCode": "INTERNAL.BAD_OUTPUT.CODE01"})
    process_jobs(db, settings, provider)
    assert video_job.state == "failed" and video_job.task_id == "task-1"
    assert "INTERNAL.BAD_OUTPUT.CODE01" in video_job.error
    process_jobs(db, settings, provider)
    assert len(provider.submitted) == 1  # No automatic paid retry.
    retry_step(db, video_job, retry_option(video_job)["prompt"])
    process_jobs(db, settings, provider)
    assert video_job.task_id == "task-2" and len(provider.submitted) == 2
    assert "seed" not in provider.submitted[1][1]
    assert video_job.brief["failed_tasks"][-1]["task_id"] == "task-1"


def test_incomplete_scene_review_can_resume_without_rebuying_scene(db, settings, video_job, monkeypatch):
    provider = Provider(db, video_job, settings)
    for _ in range(2):
        process_jobs(db, settings, provider)
    scene_id = refs.current_plan(video_job)["scene_reference"]["asset_id"]
    with monkeypatch.context() as failed_review:
        failed_review.setattr(scenes, "review_scene", lambda *args: (_ for _ in ()).throw(
            ProviderError("Temporary review error", provider="OpenAI", category="service")))
        process_jobs(db, settings, provider)
    assert video_job.state == "failed" and not refs.current_plan(video_job).get("scene_check")
    resume_job(video_job)
    db.commit()
    process_jobs(db, settings, provider)
    assert refs.current_plan(video_job)["scene_reference"]["asset_id"] == scene_id
    assert [endpoint for endpoint, _ in provider.submitted] == ["text_to_image", "image_to_video"]


def test_ambiguous_scene_submission_does_not_auto_retry(db, settings, video_job, monkeypatch):
    provider = Provider(db, video_job, settings)
    calls = []

    def submit(*args):
        calls.append(args)
        raise ProviderError("Timeout", provider="Runway", category="timeout")

    monkeypatch.setattr(provider, "submit", submit)
    process_jobs(db, settings, provider)
    assert video_job.state == "uncertain" and video_job.brief["active_generation"] == "scene"
    process_jobs(db, settings, provider)
    assert len(calls) == 1
    with pytest.raises(ValueError, match="Reconcile"):
        resume_job(video_job)


def test_permissions_are_checked_between_scene_and_animation(db, settings, video_job, photo):
    provider = Provider(db, video_job, settings)
    process_jobs(db, settings, provider)
    process_jobs(db, settings, provider)
    photo.rights = "not_public"
    db.commit()
    process_jobs(db, settings, provider)
    assert video_job.state == "failed" and "permissions" in video_job.error
    assert len(provider.submitted) == 1


def test_private_output_scenes_can_be_animated_without_becoming_public(db, settings, video_job):
    video_job.brief = video_job.brief | {"rights": "not_public"}
    db.commit()
    provider = Provider(db, video_job, settings)
    for _ in range(3):
        process_jobs(db, settings, provider)
    assert video_job.state == "running", video_job.error
    record = refs.current_plan(video_job)["scene_reference"]
    assert db.get(Asset, record["asset_id"]).rights == "not_public"
    assert provider.submitted[1][0] == "image_to_video"


def test_scene_metadata_change_blocks_animation(db, settings, video_job):
    provider = Provider(db, video_job, settings)
    for _ in range(2):
        process_jobs(db, settings, provider)
    scene = db.get(Asset, refs.current_plan(video_job)["scene_reference"]["asset_id"])
    scene.rights = "not_public"
    db.commit()
    process_jobs(db, settings, provider)
    assert video_job.state == "failed" and "scene or its permissions changed" in video_job.error
    assert len(provider.submitted) == 1


def test_scene_review_receives_actual_pixels_and_creative_intent(db, settings, video_job, monkeypatch):
    # Capture the real reviewer before replacing only its network call.
    monkeypatch.setattr(scenes, "review_scene", REAL_REVIEW)
    seen = []
    provider = Provider(db, video_job, settings)
    monkeypatch.setattr(refs, "website_references", lambda *args: [])
    monkeypatch.setattr(refs, "select_reference", lambda s, r, c: choice(r[0]))
    process_jobs(db, settings, provider)
    process_jobs(db, settings, provider)

    def response(self, endpoint, payload):
        seen.append(payload)
        return {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps({"passed": True, "reasons": []})}],
                }
            ],
        }

    monkeypatch.setattr(OpenAI, "call", response)
    process_jobs(db, settings, provider)
    assert video_job.state == "running", video_job.error
    content = seen[0]["input"][0]["content"]
    assert len([p for p in content if p["type"] == "input_image"]) == 2
    context = json.loads(content[0]["text"].split("Context: ")[1])
    assert "courtyard" in context["creative_changes"]
    assert context["visual_direction"] == video_job.brief["visual_direction"]
    assert context["builder"]["products"]
