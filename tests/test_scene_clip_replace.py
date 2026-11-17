"""Regenerating a held post's clip that was animated from a generated scene image."""

import io
import json
import math
import struct
import wave

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from test_reference_grounding import choice
from test_reference_grounding import photo as photo  # noqa: F401
from test_reference_grounding import reference_job as reference_job  # noqa: F401
from test_reference_grounding import sheet as sheet  # noqa: F401
from test_scene_generation import Provider

import social.visual_context as visual_context
from social import reference_grounding as refs
from social import scene_generation as scenes
from social.app import create_app
from social.clip_replace import FIXES, clip_options, findings, flagged, replace_part
from social.lifecycle import transition
from social.media_studio import process_jobs
from social.models import Asset, CheckResult, MediaJob, Post
from social.providers import OpenAI
from social.seed import seed_media

REAL_SELECTOR = refs.select_reference
REAL_REVIEW = scenes.review_scene
CLIP_2 = "Garbled lettering on the cafe sign at about 8 seconds."
CLIP_1 = "A person crosses the courtyard at 2 seconds."
UNPLACED = "The fascia edge looks soft."
REQUIREMENTS = ["White aluminum frame", "Four slim posts"]


class Runway(Provider):
    """Scenes, clips and music, with the real job's checkpoints."""

    def submit(self, endpoint, payload):
        self.submitted.append((endpoint, payload))
        return "task-" + str(len(self.submitted))

    def download(self, task_id):
        endpoint = self.submitted[int(task_id.split("-")[1]) - 1][0]
        if endpoint == "text_to_image":
            return self.still
        if endpoint == "text_to_music":
            stream = io.BytesIO()
            with wave.open(stream, "wb") as track:
                track.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
                track.writeframes(
                    b"".join(struct.pack("<h", int(6000 * math.sin(i / 20))) for i in range(48000 * 12))
                )
            return stream.getvalue()
        return seed_media(duration=6)[1]


def run(db, settings, job, runway):
    for _ in range(12):
        process_jobs(db, settings, runway)
        db.refresh(job)
        if job.state in {"complete", "failed"}:
            break


@pytest.fixture
def held(db, settings, reference_job, photo, house_logo, monkeypatch):  # noqa: F811
    """A finished two-clip video made from generated scenes, held by the visual check."""
    job = reference_job
    monkeypatch.setattr(
        refs,
        "select_reference",
        lambda s, r, c: choice(r[0]).model_copy(update={"scene_requirements": REQUIREMENTS}),
    )
    monkeypatch.setattr(scenes, "review_scene", lambda *args: scenes.SceneCheck(passed=True, reasons=[]))
    monkeypatch.setattr(visual_context, "ensure_visual_summary", lambda *args, **kwargs: None)
    settings.ai_mode, settings.runway_api_key = "live", SecretStr("test")
    job.brief = job.brief | {
        "operation": "video",
        "duration": 12,
        "audio_prompt": "Calm ambient pads",
        "soundtrack_model": "music",
        "create_posts": True,
        "end_card": {"asset_id": house_logo.id, "seconds": 3},
        "prompt": "White pergola in a stone courtyard, then one at a brick cafe.",
        "visual_direction": "Warm soft daylight, natural grade, 35mm lens, slow left-to-right tracking.",
        "video_segments": [
            {
                "index": i,
                "start": i * 6,
                "duration": 6,
                "seed": i,
                "system": "Pergolas",
                "prompt": setting,
                "shot": {"system": "Pergolas", "setting": setting},
            }
            for i, setting in enumerate(
                ["White pergola in a stone courtyard", "White pergola at a brick cafe"]
            )
        ],
    }
    db.commit()
    runway = Runway(db, job, settings)
    run(db, settings, job, runway)
    assert job.state == "complete", job.error
    # A correction saved during the first run belongs to that run only.
    job.brief = job.brief | {
        "failed_tasks": [{"stage": "scene", "task_id": "old"}],
        "video_segments": [
            s | {"scene_retry": {"operator_prompt": "An old edit from the first run", "prompt_edited": True}}
            if s["index"] == 1
            else s
            for s in job.brief["video_segments"]
        ],
    }
    post = db.get(Post, job.post_id)
    transition(db, post, "held", "Visual check failed")
    db.commit()
    return job, post, runway


def hold(db, post, reasons, **flags):
    db.add(
        CheckResult(
            post_id=post.id,
            run_id="vision-" + str(len(reasons)) + str(sorted(flags)),
            name="vision_review",
            passed=False,
            details={"passed": False, "reasons": reasons, "visible_claims": []} | flags,
        )
    )
    db.commit()


def test_held_scene_clip_is_corrected_through_its_scene(db, settings, held, photo, monkeypatch):  # noqa: F811
    job, post, runway = held
    hold(db, post, [CLIP_2, CLIP_1, UNPLACED], garbled_text=True)
    old = job.brief["video_segments"][1]
    plan = old["reference_plan"]
    part = next(p for p in clip_options(db, post)["parts"] if p["key"] == "1")
    # The box holds the scene the clip was animated from, with the wording for what the check found.
    assert part["scene"] and part["flagged"]
    assert part["prompt"] == plan["scene_prompt"] + " " + FIXES["text"]
    assert part["camera"] == old["prompt"] and "camera push" in part["camera"]
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/posts/{post.id}").text
    assert "Scene for this clip" in page and "Last time the camera movement was" in page
    assert "This makes a new scene image, then one new 6-second Runway clip." in page
    assert "the new scene is checked for the same problems" in page

    redo = replace_part(db, post, "1", "", "admin")
    segment = redo.brief["video_segments"][1]
    retry = segment["scene_retry"]
    assert "reference_plan" not in segment and "output_asset_id" not in segment
    assert segment["prompt"] == part["prompt"] and segment["seed"] == old["seed"] + 1
    assert retry["review"] == {"passed": False, "reasons": [CLIP_2, UNPLACED]}
    assert retry["video_check"] == {"reasons": [CLIP_2, UNPLACED], "covers": "6–12 seconds of the video"}
    assert retry["scene"] == plan["scene_reference"] and retry["previous_prompt"] == plan["scene_prompt"]
    assert retry["scene_requirements"] == REQUIREMENTS and retry["source"] == "held_post"
    assert retry["prompt_edited"] is True and retry["operator_prompt"] == part["prompt"]
    assert "old edit" not in json.dumps(redo.brief)
    assert "failed_tasks" not in redo.brief and "active_generation" not in redo.brief
    assert redo.brief["video_segments"][0] == job.brief["video_segments"][0]

    seen = []

    def call(self, endpoint, payload):
        seen.append(payload)
        if payload["text"]["format"]["name"] == "product_reference":
            answer = (
                choice(refs.asset_record(photo))
                .model_copy(update={"scene_requirements": [*REQUIREMENTS, "Plain unmarked cafe fascia"]})
                .model_dump_json()
            )
        else:
            answer = json.dumps({"passed": True, "reasons": []})
        return {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": answer}]}],
        }

    monkeypatch.setattr(refs, "select_reference", REAL_SELECTOR)
    monkeypatch.setattr(scenes, "review_scene", REAL_REVIEW)
    monkeypatch.setattr(OpenAI, "call", call)
    before = len(runway.submitted)
    run(db, settings, redo, runway)
    assert redo.state == "complete", redo.error

    planner, review = [p["input"][0]["content"] for p in seen]
    context = json.loads(planner[0]["text"].split("Context: ")[1])
    assert "scene_retry.video_check" in planner[0]["text"]
    assert context["scene_retry"]["video_check"]["reasons"] == [CLIP_2, UNPLACED]
    assert context["scene_retry"]["scene"]["asset_id"] == plan["scene_reference"]["asset_id"]
    assert any(p.get("text", "").startswith("REJECTED OUTPUT:") for p in planner)
    # The scene check knows what went wrong last time.
    checked = json.loads(review[0]["text"].split("Context: ")[1])
    assert checked["earlier_defects"] == [CLIP_2, UNPLACED]
    assert "earlier_defects lists" in review[0]["text"]

    (scene_step, scene_payload), (clip_step, clip_payload) = runway.submitted[before:]
    new_plan = redo.brief["video_segments"][1]["reference_plan"]
    assert scene_step == "text_to_image" and "Plain unmarked cafe fascia" in scene_payload["promptText"]
    style = job.brief["video_segments"][0]["reference_plan"]["scene_reference"]["asset_id"]
    assert [r["uri"] for r in scene_payload["referenceImages"]] == [
        "runway://" + photo.id,
        "runway://" + style,
    ]
    assert clip_step == "image_to_video"
    assert clip_payload["promptImage"] == "runway://" + new_plan["scene_reference"]["asset_id"]
    assert new_plan["scene_reference"]["asset_id"] != plan["scene_reference"]["asset_id"]
    # The clip is animated with a newly planned camera prompt, not the scene description.
    assert redo.brief["video_segments"][1]["prompt"] == new_plan["prompt"] == clip_payload["promptText"]
    assert db.get(Post, post.id).status == "failed" and db.get(Post, redo.post_id).status == "draft"


def test_unchanged_and_edited_scene_corrections(db, held):
    job, post, _ = held
    hold(db, post, ["The roof floats above the posts at 8 seconds."])
    scene_prompt = job.brief["video_segments"][1]["reference_plan"]["scene_prompt"]
    part = next(p for p in clip_options(db, post)["parts"] if p["key"] == "1")
    assert part["prompt"] == scene_prompt  # Nothing to add for a roof.
    retry = replace_part(db, post, "1", "", "admin").brief["video_segments"][1]["scene_retry"]
    assert retry["prompt_edited"] is False and "operator_prompt" not in retry
    assert retry["review"]["reasons"] == ["The roof floats above the posts at 8 seconds."]

    for redo in db.query(MediaJob).filter(MediaJob.id != job.id):
        redo.state = "cancelled"
    db.commit()
    edit = "A white pergola at a brick cafe with a plain sign, every roof member resting on the posts."
    retry = replace_part(db, post, "1", edit, "admin").brief["video_segments"][1]["scene_retry"]
    assert retry["prompt_edited"] is True and retry["operator_prompt"] == edit == retry["requested_prompt"]

    # Clip 1 wasn't where the check found the problem, so it gets no findings to correct.
    for redo in db.query(MediaJob).filter(MediaJob.id != job.id):
        redo.state = "cancelled"
    db.commit()
    retry = replace_part(db, post, "0", "", "admin").brief["video_segments"][0]["scene_retry"]
    assert "review" not in retry and "scene" not in retry and "video_check" not in retry


def test_old_scene_with_changed_permissions_still_passes_on_the_findings(db, settings, held, monkeypatch):
    job, post, runway = held
    hold(db, post, [CLIP_2])
    redo = replace_part(db, post, "1", "", "admin")
    old_scene = db.get(Asset, redo.brief["video_segments"][1]["scene_retry"]["scene"]["asset_id"])
    old_scene.tags = old_scene.tags | {"note": "Edited after the post was held"}
    db.commit()
    contexts = []

    def choose(s, records, context):
        contexts.append(context)
        return choice(records[0]).model_copy(update={"scene_requirements": REQUIREMENTS})

    monkeypatch.setattr(refs, "select_reference", choose)
    run(db, settings, redo, runway)
    assert redo.state == "complete", redo.error
    assert "scene" not in contexts[0]["scene_retry"]
    assert contexts[0]["scene_retry"]["review"]["reasons"] == [CLIP_2]


def test_findings_go_to_the_clip_they_concern():
    segments = [{"index": i, "start": i * 6, "duration": 6} for i in range(2)]

    def clip(index, vision):
        part = {"kind": "clip", "index": index, "flagged": index in flagged(vision, segments)}
        return findings(vision, segments, part)

    placed = {"reasons": ["Garbled lettering on a sign."], "problem_seconds": [8.0], "garbled_text": True}
    assert clip(1, placed) == ["Garbled lettering on a sign."] and clip(0, placed) == []
    unplaced = {"reasons": ["Garbled lettering on a sign."]}
    assert clip(0, unplaced) == clip(1, unplaced) == ["Garbled lettering on a sign."]
    timed = {"reasons": ["A sign at 2 seconds.", "A logo at 9 seconds.", "Soft edges."]}
    assert clip(0, timed) == ["A sign at 2 seconds.", "Soft edges."]
    assert clip(1, timed) == ["A logo at 9 seconds.", "Soft edges."]
    # Only the check's flags: written for the flagged clip.
    assert clip(1, {"reasons": [], "problem_seconds": [7.0], "unexpected_people": True}) == [
        "People appear who weren't requested."
    ]
