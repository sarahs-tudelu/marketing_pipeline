import pytest
from test_runway_media import brief_for

from social.idea_plan import IdeaPlan, clip_seconds_preview, work_summary
from social.media_studio import queue_jobs
from social.runway import VEO_NEGATIVE_PROMPT, job_settings, payload_for


@pytest.mark.parametrize("platform,scene,ratio", [
    ("instagram", True, "768:1376"),
    ("linkedin", True, "1376:768"),
    ("instagram", False, "928:1152"),
    ("linkedin", False, "1024:1024"),
])
def test_nano_contract_preserves_product_and_style_roles(settings, platform, scene, ratio):
    _, payload = payload_for(settings, {
        "operation": "image", "generation_prompt": "Use @ref0 construction.",
        "seed": 10, "video_scene": scene,
    }, platform, ["runway://product"], style="runway://style")
    assert payload == {
        "model": "nano_banana_2.1", "promptText": "Use @ref0 construction.",
        "ratio": ratio, "thinkingLevel": "high",
        "referenceImages": [{"uri": "runway://product", "tag": "ref0"}, {"uri": "runway://style"}],
    }


@pytest.mark.parametrize("seconds,generated", [(2, 4), (3, 4), (4, 4), (5, 6), (6, 6), (7, 8), (8, 8)])
def test_veo_covers_the_planned_length_without_mutating_it(settings, seconds, generated):
    brief = {"operation": "video", "generation_prompt": "Still canopy.", "seed": 10, "duration": seconds}
    _, payload = payload_for(settings, brief, "linkedin", ["runway://scene"])
    assert payload == {
        "model": "veo3.1", "promptText": "Still canopy.", "seed": 10, "duration": generated,
        "ratio": "1280:720", "audio": False, "negativePrompt": VEO_NEGATIVE_PROMPT,
        # The scene is the first frame only: as the last frame too, Veo returned to it and the clip froze.
        "promptImage": [{"uri": "runway://scene", "position": "first"}],
    }
    assert brief["duration"] == seconds


@pytest.mark.parametrize("model,clips", [("veo3.1", 11), ("gen4.5", 9)])
def test_long_video_preview_matches_saved_jobs_and_preserves_total(db, settings, house_logo, model, clips):
    settings.runway_video_model = model
    brief = brief_for(db, settings, operation="video", duration=90)
    job = queue_jobs(db, settings, brief)[0]
    plan = IdeaPlan(title="Long video", operation="video", duration=90, end_card=True)
    lengths = [s["duration"] for s in job.brief["video_segments"]]
    assert lengths == clip_seconds_preview(plan, model)
    assert len(lengths) == clips and sum(lengths) == 87
    assert job.brief["runway_image_model"] == "nano_banana_2.1"
    assert job.brief["runway_video_model"] == model
    # A later environment change must not change recovery, replacement or provenance.
    settings.runway_video_model, settings.runway_image_model = "gen4.5", "gen4_image"
    saved = job_settings(settings, job.brief)
    assert saved.runway_video_model == model and saved.runway_image_model == "nano_banana_2.1"


def test_pre_switch_jobs_keep_legacy_contracts(settings):
    saved = job_settings(settings, {})
    brief = {"operation": "video", "generation_prompt": "Still canopy.", "seed": 10, "duration": 10}
    _, payload = payload_for(saved, brief, "linkedin", ["runway://scene"])
    assert payload["model"] == "gen4.5" and payload["duration"] == 10
    # Gen-4.5 takes one image and has no negativePrompt field.
    assert payload["promptImage"] == "runway://scene" and "negativePrompt" not in payload
    assert payload["promptImage"] == "runway://scene" and payload["outputFormat"] == "mp4"
    _, image = payload_for(saved, brief | {"operation": "image", "video_scene": True}, "linkedin", [])
    assert image["model"] == "gen4_image" and image["seed"] == 10 and image["ratio"] == "1280:720"


def test_work_preview_discloses_extra_generated_footage():
    plan = IdeaPlan(title="A canopy", operation="video", duration=10, end_card=True)
    assert "Veo generates 8 seconds in total; extra footage is trimmed" in work_summary(plan, ["facebook"])


def test_veo_surplus_is_trimmed_before_the_logo():
    import io

    import av
    from test_video_segments import clip

    from social.renditions import video_sequence

    # Five seconds of footage is purchased as six seconds, followed by the three-second ending.
    result = video_sequence([(clip(6, 0), 0, 5), (clip(3, 2), 0, 3)], 64, 64, require_full_duration=True)
    with av.open(io.BytesIO(result)) as media:
        assert media.duration / av.time_base == 8
        frames = list(media.decode(video=0))
        assert len(frames) == 240
        assert frames[149].to_ndarray(format="rgb24").mean(axis=(0, 1)).argmax() == 0
        assert frames[150].to_ndarray(format="rgb24").mean(axis=(0, 1)).argmax() == 2
