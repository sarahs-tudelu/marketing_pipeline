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
    assert provider.submitted[0][1]["ratio"] == "720:1280"
    assert "white" in provider.submitted[0][1]["promptText"]
    assert provider.submitted[0][1]["referenceImages"][0]["uri"] == "runway://" + photo.id
    with TestClient(create_app(settings)) as client:
        page = client.get("/admin/media-queue")
        assert page.status_code == 200 and "View generated scene" in page.text
        assert "Preparing the scene from your prompt" in page.text
        progress = client.get("/api/media-jobs").json()[0]
        assert progress["generation_phase"] == "scene" and progress["completed_clips"] == 0
    process_jobs(db, settings, provider)
    assert refs.current_plan(video_job)["scene_check"]["passed"]
    assert provider.submitted[1][0] == "image_to_video"
    assert provider.submitted[1][1]["promptImage"] == "runway://" + scene_id
    process_jobs(db, settings, provider)
    assert video_job.state == "rendering", video_job.error
    clip = db.get(Asset, video_job.source_asset_ids[0])
    assert set(clip.tags["source_asset_ids"]) == {photo.id, scene_id}
    assert [h["stage"] for h in video_job.task_history] == ["scene", "visual"]
    assert storage(settings).read(db.get(Asset, scene_id).storage_key) == provider.still


def test_rejected_scene_never_buys_video_and_can_be_reviewed_or_replaced(
    db, settings, video_job, monkeypatch
):
    monkeypatch.setattr(
        scenes,
        "review_scene",
        lambda *args: scenes.SceneCheck(passed=False, reasons=["A roof member floats above the frame."]),
    )
    provider = Provider(db, video_job, settings)
    for _ in range(3):
        process_jobs(db, settings, provider)
    saved = refs.current_plan(video_job)["scene_reference"]["asset_id"]
    assert video_job.state == "failed" and "floats" in video_job.error
    assert len(provider.submitted) == 1 and not video_job.task_id
    assert retry_option(video_job)["kind"] == "scene"
    resume_job(video_job)
    db.commit()
    process_jobs(db, settings, provider)
    assert (
        len(provider.submitted) == 1 and refs.current_plan(video_job)["scene_reference"]["asset_id"] == saved
    )
    retry_step(db, video_job, "White pergola in a courtyard, all roof members connected.")
    process_jobs(db, settings, provider)
    assert video_job.state == "running", video_job.error
    assert len(provider.submitted) == 2 and not refs.current_plan(video_job).get("scene_reference")
    assert video_job.brief["failed_tasks"][-1]["stage"] == "scene"


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
    assert provider.submitted[2][1]["referenceImages"] == [
        {"tag": "ref0", "uri": "runway://" + photo.id},
        {"tag": "ref1", "uri": "runway://" + first["scene_reference"]["asset_id"]},
    ]
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
