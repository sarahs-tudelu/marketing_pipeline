"""Real company photos and videos become posts of completed projects through standard edits only."""

import io
import json
import wave
from fractions import Fraction
from types import SimpleNamespace

import av
import numpy as np
import pytest
from PIL import Image
from test_runway_media import brief_for

from social.config import profiles
from social.gate import media_errors, run_gate
from social.media_studio import check_real_media, process_jobs, queue_jobs, validate_sources
from social.models import Asset, CheckResult, MediaCampaign, Post
from social.pipeline import generate
from social.provenance import requires_ai_review
from social.providers import OpenAI, Providers
from social.real_edit import (
    BACKGROUND,
    FRAMES_PER_VIDEO,
    AudioCue,
    EditItem,
    RealEditPlan,
    cells,
    fit_plan,
    photo_post,
    plan_edit,
    soundtrack,
)
from social.renditions import audio_samples
from social.runway import Runway
from social.storage import ingest
from social.video_rules import video_rule_errors


def jpeg(color, size=(1200, 900)):
    stream = io.BytesIO()
    Image.new("RGB", size, color).save(stream, "JPEG", quality=95)
    return stream.getvalue()


def tone(frequency, seconds, level=0.2):
    times = np.arange(round(seconds * 48000)) / 48000
    wave_ = (level * np.sin(2 * np.pi * frequency * times)).astype(np.float32)
    return np.vstack([wave_, wave_])


def wav_bytes(frequency=660, seconds=3):
    pcm = (tone(frequency, seconds, 0.3).T * 32767).astype("<i2")
    stream = io.BytesIO()
    with wave.open(stream, "wb") as file:
        file.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
        file.writeframes(pcm.tobytes())
    return stream.getvalue()


def footage(seconds=4, size=(640, 360), sound=True):
    """A landscape phone clip: a bar moving across a flat wall, with a 440 Hz hum when `sound`."""
    stream = io.BytesIO()
    with av.open(stream, "w", format="mp4") as container:
        video = container.add_stream("libx264", rate=24)
        video.width, video.height, video.pix_fmt = *size, "yuv420p"
        audio = container.add_stream("aac", rate=48000) if sound else None
        if audio:
            audio.layout = "stereo"
        for index in range(seconds * 24):
            picture = np.full((size[1], size[0], 3), (40, 90, 140), dtype=np.uint8)
            x = index * 8 % (size[0] - 20)
            picture[:, x : x + 20] = 240
            for packet in video.encode(av.VideoFrame.from_ndarray(picture, format="rgb24")):
                container.mux(packet)
        for packet in video.encode():
            container.mux(packet)
        if audio:
            samples = tone(440, seconds)
            for start in range(0, samples.shape[1], 1024):
                frame = av.AudioFrame.from_ndarray(
                    np.ascontiguousarray(samples[:, start : start + 1024]), format="fltp", layout="stereo"
                )
                frame.sample_rate, frame.pts, frame.time_base = 48000, start, Fraction(1, 48000)
                for packet in audio.encode(frame):
                    container.mux(packet)
            for packet in audio.encode():
                container.mux(packet)
    return stream.getvalue()


def close(pixel, color, tolerance=6):
    return all(abs(a - b) <= tolerance for a, b in zip(pixel, color, strict=True))


def test_a_photo_is_straightened_and_corrected_without_showing_corners():
    plan = RealEditPlan(layout="single", items=[EditItem(asset_id="p", straighten=8, brightness=1.2)])
    out = Image.open(io.BytesIO(photo_post({"p": Image.new("RGB", (1600, 1000), (100, 120, 140))}, plan, 1080, 1350)))
    assert out.size == (1080, 1350)
    # Brightened by 1.2 to the very corners: the rotation is cropped, so no padding or black shows.
    for corner in [(2, 2), (1077, 2), (2, 1347), (1077, 1347)]:
        assert close(out.getpixel(corner), (120, 144, 168))


@pytest.mark.parametrize("focus,color", [(0.0, (200, 40, 40)), (1.0, (40, 40, 200))])
def test_the_crop_keeps_the_chosen_part_of_the_photo(focus, color):
    picture = Image.new("RGB", (1600, 800), (200, 40, 40))
    picture.paste((40, 40, 200), (800, 0, 1600, 800))
    plan = RealEditPlan(layout="single", items=[EditItem(asset_id="p", focus_x=focus)])
    out = Image.open(io.BytesIO(photo_post({"p": picture}, plan, 1080, 1080)))
    assert close(out.getpixel((540, 540)), color)


def test_a_collage_puts_each_photo_in_its_own_cell():
    colors = [(200, 30, 30), (30, 200, 30), (30, 30, 200), (200, 200, 30)]
    pictures = {str(i): Image.new("RGB", (800, 600), c) for i, c in enumerate(colors)}
    plan = RealEditPlan(layout="grid_four", items=[EditItem(asset_id=str(i)) for i in range(4)])
    out = Image.open(io.BytesIO(photo_post(pictures, plan, 1080, 1350)))
    for (x, y, w, h), color in zip(cells("grid_four", 1080, 1350), colors, strict=True):
        assert close(out.getpixel((x + w // 2, y + h // 2)), color)
    assert close(out.getpixel((3, 3)), BACKGROUND) and close(out.getpixel((540, 675)), BACKGROUND)


def test_the_edit_stays_inside_the_media_and_the_requested_length():
    video = SimpleNamespace(id="v", media={"mime": "video/mp4", "duration": 5.0})
    photo = SimpleNamespace(id="p", media={"mime": "image/jpeg"})
    bite = SimpleNamespace(id="b", media={"mime": "audio/wav", "duration": 2.0})
    plan = RealEditPlan(
        layout="sequence",
        items=[
            EditItem(asset_id="v", start=4.5, end=12, brightness=3, zoom=9),
            EditItem(asset_id="ghost", end=3),
            EditItem(asset_id="p", end=20),
            EditItem(asset_id="v", end=1),
        ],
        audio=[AudioCue(asset_id="b", start=0.5, at=6, duration=9), AudioCue(asset_id="other", duration=1)],
    )
    fitted = fit_plan(plan, [video, photo], [bite], True, 7)
    # A clip lasts at least 1.5 seconds within its video; unknown media is left out; the photo fills the rest.
    assert [(i.asset_id, i.start, i.end) for i in fitted.items] == [("v", 3.5, 5.0), ("p", 0.0, 5.5)]
    assert (fitted.items[0].brightness, fitted.items[0].zoom) == (1.2, 2.0)
    assert [(c.asset_id, c.start, c.at, c.duration) for c in fitted.audio] == [("b", 0.5, 6.0, 1.0)]
    # A collage needs as many photos as cells; with two it becomes a pair.
    two = RealEditPlan(layout="grid_four", items=[EditItem(asset_id="p"), EditItem(asset_id="q")])
    second = SimpleNamespace(id="q", media={"mime": "image/png"})
    assert fit_plan(two, [photo, second], [], False, 0).layout == "pair_stacked"
    with pytest.raises(ValueError, match="No real media fits this post: only drawings"):
        fit_plan(RealEditPlan(layout="single", items=[], problem="only drawings"), [photo], [], False, 0)


def test_uploaded_audio_is_laid_at_its_place_over_the_lowered_clip_sound():
    clip = footage(seconds=4)
    plan = RealEditPlan(
        layout="sequence", items=[EditItem(asset_id="v", end=4)], audio=[AudioCue(asset_id="b", at=2, duration=1.5)]
    )
    mixed = audio_samples(soundtrack(plan, [(clip, 0, 4)], [True], {"b": wav_bytes()}, 4, 0))

    def level(frequency, start, end):
        window = mixed[0, round(start * 48000) : round(end * 48000)]
        spectrum = np.abs(np.fft.rfft(window)) / len(window)
        return spectrum[round(frequency * len(window) / 48000)]

    # Before the bite, only the clip's 440 Hz hum; under it, the bite at 660 Hz and the hum lowered.
    assert level(440, 0.5, 1.5) > 0.05 and level(660, 0.5, 1.5) < 0.005
    assert level(660, 2.3, 3.3) > 0.1 and level(440, 2.3, 3.3) < 0.4 * level(440, 0.5, 1.5)
    # Without the clip's sound and without bites, the footage is silent and gets ambient music instead.
    quiet = plan.model_copy(update={"audio": [], "keep_clip_sound": False})
    assert soundtrack(quiet, [(clip, 0, 4)], [True], {}, 4, 0) is None


def test_real_video_is_trimmed_joined_scored_and_stays_company_media(db, settings, house_logo, monkeypatch):
    from social import real_edit
    from social.storage import LocalStore

    def forbidden(*args, **kwargs):
        raise AssertionError("Real media must never be sent to Runway")

    monkeypatch.setattr(Runway, "submit", forbidden)
    clip = ingest(db, settings, footage(), "installation.mp4", rights="public")
    still = ingest(db, settings, jpeg((180, 120, 60)), "finished.jpg", rights="public")
    bite = ingest(db, settings, wav_bytes(), "site-voice.wav", rights="public")
    rendering = False
    read, render = LocalStore.read, real_edit.real_copy

    def bounded_read(store, key):
        if rendering and key in {clip.storage_key, still.storage_key, bite.storage_key}:
            pytest.fail("Real edits must render verified source files without buffering all originals")
        return read(store, key)

    def bounded_render(*args):
        nonlocal rendering
        rendering = True
        try:
            return render(*args)
        finally:
            rendering = False

    monkeypatch.setattr(LocalStore, "read", bounded_read)
    monkeypatch.setattr(real_edit, "real_copy", bounded_render)
    db.commit()
    brief = brief_for(
        db, settings, operation="real", duration=10,
        reference_ids=[clip.id, still.id], audio_reference_ids=[bite.id],
    )
    [job] = queue_jobs(db, settings, brief)
    db.commit()
    assert job.brief["real_format"] == "video" and job.brief["reference_ids"] == [clip.id, still.id]
    for _ in range(3):
        process_jobs(db, settings)
    assert job.state == "complete", job.error
    edit = job.brief["real_edit"]
    assert [(i["asset_id"], i["end"]) for i in edit["items"]] == [(clip.id, 3.5), (still.id, 3.5)]
    result = db.get(Asset, job.output_asset_id)
    assert result.source == "company" and not requires_ai_review(result)
    assert result.tags["real_project"] and "No generative editing" in result.tags["standard_edit"]["tools"]
    assert {clip.id, still.id, bite.id, house_logo.id} <= set(result.tags["source_asset_ids"])
    assert result.tags["video_sound"] == "upload" and result.media["duration"] == pytest.approx(10, abs=0.2)
    assert media_errors([result], profiles(settings)["instagram"], "instagram") == []
    assert video_rule_errors([result]) == []

    seen = {}

    class Capture(Providers):
        def caption(self, context):
            seen.update(context)
            return super().caption(context)

    post = db.get(Post, job.post_id)
    generate(db, settings, post, providers=Capture(settings))
    # The caption writer may present it as a completed project; it carries no AI label.
    assert seen["media_kind"] == "real_media" and post.ai_disclosure is False


def test_real_project_posts_use_only_real_media(db, settings):
    concept = ingest(
        db, settings, jpeg((90, 90, 90)), "concept.jpg", source="ai_generated",
        generator="runway/gen4_image", prompt="A pergola", rights="public",
    )
    real = ingest(db, settings, jpeg((150, 110, 80)), "terrace.jpg", rights="public")
    db.commit()
    with pytest.raises(ValueError, match="only real photos, videos and audio"):
        queue_jobs(db, settings, brief_for(db, settings, operation="real", reference_ids=[concept.id]))
    # With nothing chosen, the library's real media is used and AI media is left out.
    [job] = queue_jobs(db, settings, brief_for(db, settings, operation="real"))
    assert job.brief["real_format"] == "photo" and job.brief["reference_ids"] == [real.id]


def test_mixed_campaign_alternates_real_projects_with_ai_inspiration(db, settings, house_logo):
    first = ingest(db, settings, jpeg((150, 110, 80)), "terrace.jpg", rights="public")
    second = ingest(db, settings, jpeg((80, 110, 150)), "courtyard.jpg", rights="public")
    clip = ingest(db, settings, footage(seconds=7, sound=False), "installation.mp4", rights="public")
    db.commit()
    brief = brief_for(
        db, settings, operation="mixed", kind="mixed", duration=10, reference_ids=[first.id, second.id, clip.id]
    )
    campaign = MediaCampaign(name="Projects and ideas", brief=brief.model_dump())
    db.add(campaign)
    db.flush()
    jobs = []
    for sequence in range(12):
        jobs += queue_jobs(db, settings, brief, campaign=campaign, sequence=sequence)
        db.commit()
    assert ["real" if j.brief["operation"] == "real" else "ai" for j in jobs] == ["real", "ai"] * 6
    real = [j for j in jobs if j.brief["operation"] == "real"]
    assert [j.brief["real_format"] for j in real] == ["video", "photo"] * 3
    assert all(not j.brief["concept"] and not j.brief["use_case"] for j in real)
    ai = [j for j in jobs if j.brief["operation"] != "real"]
    # The re-render of the uploaded video is one of the AI formats.
    assert [j.brief["operation"] for j in ai] == ["image", "video", "video_edit"] * 2
    assert len({j.brief["concept"] for j in ai}) == 6 and len({j.brief["use_case"] for j in ai}) == 6
    assert all("Suggested use case" in j.brief["direction"] for j in ai)
    # Each AI video starts from the next photo, instead of always the same one.
    assert [j.brief["reference_ids"] for j in ai if j.brief["operation"] == "video"] == [[first.id], [second.id]]


def test_the_editor_sees_every_candidate_and_its_answer_is_bounded(db, settings, house_logo, monkeypatch):
    clip = ingest(db, settings, footage(sound=False), "installation.mp4", rights="public")
    still = ingest(db, settings, jpeg((180, 120, 60)), "finished.jpg", rights="public")
    db.commit()
    settings.ai_mode = "live"
    [job] = queue_jobs(db, settings, brief_for(db, settings, operation="real", duration=10,
                                               reference_ids=[clip.id, still.id]))
    db.commit()
    edit = {"start": 0, "focus_x": 0.3, "focus_y": 0.5, "quarter_turns": 0, "contrast": 1, "saturation": 1,
            "warmth": 0, "framing": "fill"}
    answer = {
        "layout": "sequence",
        "items": [
            edit | {"asset_id": still.id, "end": 3, "zoom": 5, "straighten": 30, "brightness": 2},
            edit | {"asset_id": clip.id, "start": 1, "end": 3.5, "zoom": 1, "straighten": 0, "brightness": 1},
        ],
        "audio": [], "keep_clip_sound": True, "story": "The finished terrace, then the crew at work.", "problem": "",
    }
    sent = {}

    def call(self, endpoint, payload):
        sent.update(payload)
        text = json.dumps(answer)
        return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}

    monkeypatch.setattr(OpenAI, "call", call)
    plan_edit(db, settings, job)
    content = sent["input"][0]["content"]
    assert "Nothing may be added, removed, retouched or generated" in content[0]["text"]
    assert sum(part["type"] == "input_image" for part in content) == 1 + FRAMES_PER_VIDEO

    def closed(node):
        if isinstance(node, dict):
            if "properties" in node:
                assert node["additionalProperties"] is False and node["required"] == list(node["properties"])
            for value in node.values():
                closed(value)
        elif isinstance(node, list):
            for value in node:
                closed(value)

    closed(sent["text"]["format"]["schema"])
    saved = job.brief["real_edit"]["items"][0]
    assert (saved["zoom"], saved["straighten"], saved["brightness"]) == (2.0, 8.0, 1.2)
    assert job.source_asset_ids == [still.id, clip.id]


def from_drive(db, settings, data, name):
    tags = {"google_drive_file_id": f"drive-{name}", "google_drive_folder_id": "projects"}
    return ingest(db, settings, data, name, rights="public", tags=tags)


def test_pinterest_gets_a_real_drive_project_in_every_batch_of_a_varied_campaign(db, settings, house_logo):
    photo = from_drive(db, settings, jpeg((150, 110, 80), (1000, 1500)), "terrace.jpg")
    clip = from_drive(db, settings, footage(sound=False), "installation.mp4")
    upload = ingest(db, settings, jpeg((80, 110, 150)), "phone-upload.jpg", rights="public")
    db.commit()
    brief = brief_for(db, settings, operation="mixed", kind="mixed", duration=10,
                      platforms=["instagram", "pinterest"], reference_ids=[upload.id, photo.id, clip.id])
    campaign = MediaCampaign(name="Projects", brief=brief.model_dump())
    db.add(campaign)
    db.flush()
    jobs = []
    for sequence in range(4):
        jobs += queue_jobs(db, settings, brief, campaign=campaign, sequence=sequence)
        db.commit()
    pins = [j for j in jobs if j.platform == "pinterest"]
    # Never AI media, never media that didn't come from Google Drive; video and photo posts alternate.
    assert [(j.brief["operation"], j.brief["real_format"]) for j in pins] == [("real", "video"), ("real", "photo")] * 2
    assert all(set(j.brief["reference_ids"]) <= {photo.id, clip.id} for j in pins)
    instagram = [j.brief["operation"] for j in jobs if j.platform == "instagram"]
    assert instagram[0] == "real" and instagram[2] == "real" and "real" not in (instagram[1], instagram[3])
    # Each job lists only its own platform, so an AI job for Instagram isn't held to Pinterest's rule.
    assert all(j.brief["platforms"] == [j.platform] for j in jobs)


def test_a_real_pinterest_post_uses_drive_media_and_passes_the_drive_check(db, settings, house_logo):
    clip = from_drive(db, settings, footage(), "installation.mp4")
    photo = from_drive(db, settings, jpeg((180, 120, 60), (1000, 1500)), "finished.jpg")
    upload = ingest(db, settings, jpeg((80, 110, 150)), "phone-upload.jpg", rights="public")
    site = from_drive(db, settings, wav_bytes(), "site-voice.wav")
    voice = ingest(db, settings, wav_bytes(), "voice-memo.wav", rights="public")
    db.commit()
    brief = brief_for(db, settings, operation="real", duration=10, platforms=["pinterest"],
                      reference_ids=[clip.id, upload.id, photo.id], audio_reference_ids=[voice.id, site.id])
    [job] = queue_jobs(db, settings, brief)
    db.commit()
    assert job.brief["reference_ids"] == [clip.id, photo.id] and job.brief["audio_reference_ids"] == [site.id]
    for _ in range(3):
        process_jobs(db, settings)
    assert job.state == "complete", job.error
    post = db.get(Post, job.post_id)
    assert post.platform == "pinterest"
    generate(db, settings, post)
    run_gate(db, settings, post)
    checks = {c.name: c for c in db.query(CheckResult).filter(CheckResult.post_id == post.id)}
    assert checks["drive_media"].passed and checks["media"].passed and checks["video_finish"].passed


def test_a_campaign_with_pinterest_needs_drive_media_before_it_is_saved(db, settings):
    upload = ingest(db, settings, jpeg((80, 110, 150)), "phone-upload.jpg", rights="public")
    db.commit()
    brief = brief_for(db, settings, operation="mixed", kind="mixed", platforms=["instagram", "pinterest"],
                      reference_ids=[upload.id])
    with pytest.raises(ValueError, match="only posts real photos and videos imported from Google Drive"):
        check_real_media(db, settings, brief, validate_sources(db, brief))
    # AI formats still can't select Pinterest.
    with pytest.raises(ValueError, match="Choose Real project or a varied campaign"):
        brief_for(db, settings, operation="video", platforms=["pinterest"])


def test_an_ai_image_job_in_a_campaign_with_tiktok_still_runs(db, settings, house_logo):
    photo = ingest(db, settings, jpeg((150, 110, 80)), "terrace.jpg", rights="public")
    db.commit()
    brief = brief_for(db, settings, operation="mixed", platforms=["instagram", "tiktok"], reference_ids=[photo.id])
    campaign = MediaCampaign(name="Ideas", brief=brief.model_dump())
    db.add(campaign)
    db.flush()
    jobs = queue_jobs(db, settings, brief, campaign=campaign, sequence=1)
    db.commit()
    image = next(j for j in jobs if j.platform == "instagram")
    assert image.brief["operation"] == "image"
    for _ in range(3):
        process_jobs(db, settings)
    # Its brief used to list TikTok as well, and failed TikTok's video-only rule when it was rechecked.
    assert image.state == "complete", image.error
