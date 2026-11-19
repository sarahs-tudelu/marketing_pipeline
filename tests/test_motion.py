"""Generated clips whose picture doesn't move are measured from the pixels and held for automatic replacement."""

import io

import av
import numpy as np
import pytest
from test_gate import checks

from social.clip_replace import flagged
from social.db import uid
from social.models import MediaJob
from social.motion import clip_motion, still_clips, still_reason
from social.product_rules import PRODUCT_CONSTRAINTS_VERSION
from social.providers import Providers, VisionReview
from social.storage import ingest

FPS = 24
WIDTH, HEIGHT = 192, 336  # Portrait, like a reel. The middle half is x 48-144, y 84-252.


def scene(seed=7):
    """A still 'photo': flat 16-pixel blocks of colour, which compress without flicker."""
    blocks = np.random.default_rng(seed).integers(40, 215, size=(HEIGHT // 16, WIDTH // 16, 3), dtype=np.uint8)
    return np.kron(blocks, np.ones((16, 16, 1), dtype=np.uint8))


def person(image, x, y):
    out = image.copy()
    out[y : y + 60, x : x + 20] = (20, 20, 20)
    return out


def frozen(seconds=4):
    """The middle never changes; two people walk down the left and right edges."""
    still, count = scene(), seconds * FPS
    return [person(person(still, 4, 10 + 220 * i // count), 168, 240 - 220 * i // count) for i in range(count)]


def walking(seconds=4):
    """Someone crosses the middle of a fixed shot."""
    still, count = scene(), seconds * FPS
    return [person(still, 170 * i // count, 140) for i in range(count)]


def panning(seconds=4):
    still, count = scene(), seconds * FPS
    return [np.roll(still, i, axis=1) for i in range(count)]


def encode(frames):
    stream = io.BytesIO()
    with av.open(stream, mode="w", format="mp4") as container:
        video = container.add_stream("libx264", rate=FPS)
        video.width, video.height, video.pix_fmt = WIDTH, HEIGHT, "yuv420p"
        for image in frames:
            for packet in video.encode(av.VideoFrame.from_ndarray(image, format="rgb24")):
                container.mux(packet)
        for packet in video.encode():
            container.mux(packet)
    return stream.getvalue()


def test_frozen_middle_is_found_even_when_people_walk_at_the_edges():
    clip = clip_motion(encode(frozen()), [(0, 4)])[0]
    assert clip["frozen"] and clip["centre_changed"] < 0.03
    assert clip["edges_changed"] >= 0.03
    assert "with movement only near the edges" in still_reason(clip | {"clip": 2})


@pytest.mark.parametrize("make", [walking, panning])
def test_action_in_the_middle_or_a_camera_move_is_not_frozen(make):
    assert not clip_motion(encode(make()), [(0, 4)])[0]["frozen"]


def generated_video(db, settings, frames, *, sequence=True, operation="video", mode="live", demo=False):
    job = MediaJob(batch_key=f"motion-test:{uid()}", platform="linkedin", state="complete",
                   brief={"operation": operation, "execution_mode": mode})
    db.add(job)
    db.flush()
    tags = {"media_job_id": job.id, "end_card": {"seconds": 2}}
    if sequence:
        tags["video_sequence"] = [{"index": i, "start": 4 * i, "duration": 4} for i in range(3)]
    asset = ingest(db, settings, encode(frames), "generated.mp4", source="ai_generated",
                   generator="runway/veo3.1", prompt="Diners arrive under a pergola.", rights="public",
                   tags=tags, demo_only=demo)
    db.commit()
    return asset


def three_clips_and_an_ending():
    # Clip 2 is frozen; the logo ending is still by design and isn't measured.
    return [*panning(), *frozen(), *walking(), *[scene(3)] * (2 * FPS)]


def test_gate_holds_a_frozen_clip_and_marks_only_that_clip_for_replacement(db, settings, post):
    video = generated_video(db, settings, three_clips_and_an_ending())
    post.asset_ids = [video.id]
    db.commit()

    class Reviewer(Providers):
        def vision(self, supplied, context):
            return VisionReview(
                passed=True, reasons=["The pergola's geometry stays stable."], garbled_text=False,
                misused_logos=False, unexpected_people=False,
                product_constraints_version=PRODUCT_CONSTRAINTS_VERSION,
            )

    result = checks(db, settings, post, providers=Reviewer(settings))["vision_review"]
    assert result.passed is False
    assert [c["clip"] for c in result.details["still_clips"]] == [2]
    reason = result.details["reasons"][0]
    assert reason.startswith("Clip 2 (seconds 4–8) is frozen") and "near the edges" in reason
    assert result.details["problem_seconds"] == [6.0]
    # Only the frozen clip is made again: its seconds aren't read as a range that touches clip 3.
    assert flagged(result.details, video.tags["video_sequence"]) == {1}


def test_single_takes_are_measured_but_placeholders_edits_and_uploads_are_not(db, settings):
    take = generated_video(db, settings, [*frozen(), *[scene(3)] * (2 * FPS)], sequence=False)
    [clip] = still_clips(db, settings, [take])
    assert clip["clip"] == 0 and clip["end"] == pytest.approx(4, abs=0.1)
    assert still_reason(clip).startswith("The video is frozen")
    for kwargs in ({"demo": True}, {"mode": "fixture"}, {"operation": "video_edit"}):
        other = generated_video(db, settings, frozen(), **kwargs)
        assert still_clips(db, settings, [other]) == []


def test_hybrid_video_measures_generated_motion_around_original_shots(db, settings):
    video = generated_video(db, settings, [*frozen(), *frozen(), *walking(), *[scene(3)] * (2 * FPS)])
    job = db.get(MediaJob, video.tags["media_job_id"])
    sequence = list(video.tags["video_sequence"])
    sequence[0] = sequence[0] | {"original_asset_id": "original-photo"}
    job.brief = job.brief | {"video_segments": sequence}
    job.output_asset_id = video.id
    db.commit()
    assert [c["clip"] for c in still_clips(db, settings, [video])] == [2]
