"""Whether each generated clip actually moves.

A clip whose middle never changes plays like a still photo, at most with something moving near the edges.
Veo returned clips like that when it was sent its scene as both the first and the last frame. The visual check
sees about one frame a second and can't judge that reliably, so the app measures it from the pixels."""

import io

import av
import numpy as np

from .models import MediaJob
from .storage import storage

SAMPLES_PER_SECOND = 4
# Small grey frames: compression noise averages out, and a 90-second video stays quick to read.
WIDTH = 128
# A pixel has visibly changed when it differs from its clip's first frame by more than this, out of 255.
CHANGE_LEVEL = 12
# A clip is frozen when, at every sampled moment, less than 3% of the middle of the picture has visibly changed
# and the middle's average change stays under 4 of 255. Measured on the Runway clips of 17 Nov 2026: frozen Veo
# clips reached at most 1.4% and 1.7; clips that moved reached at least 46% and 27.
FROZEN_SHARE = 0.03
FROZEN_LEVEL = 4.0
# Too few samples to tell a frozen clip from a short one.
MIN_SAMPLES = 3
GENERATED = {"ai_generated", "ai_edited"}


def clip_motion(data, spans):
    """Measure each (start, end) span of a video, in seconds; one dict per span, in order."""
    found = [
        {"start": start, "end": end, "first": None, "next": start, "samples": 0,
         "centre_changed": 0.0, "centre_level": 0.0, "edges_changed": 0.0}
        for start, end in spans
    ]
    last_end = max((end for _, end in spans), default=0)
    with av.open(io.BytesIO(data)) as video:
        for frame in video.decode(video=0):
            at = float(frame.time or 0)
            if at >= last_end:
                break
            span = next((s for s in found if s["start"] <= at < s["end"]), None)
            if span is None or at + 1e-6 < span["next"]:
                continue
            span["next"] = max(span["next"], at) + 1 / SAMPLES_PER_SECOND
            height = max(2, round(frame.height * WIDTH / frame.width))
            grey = frame.reformat(width=WIDTH, height=height, format="gray").to_ndarray().astype(np.float32)
            span["samples"] += 1
            if span["first"] is None:
                span["first"] = grey
                continue
            change = np.abs(grey - span["first"])
            middle = np.zeros(change.shape, dtype=bool)
            middle[height // 4 : height - height // 4, WIDTH // 4 : WIDTH - WIDTH // 4] = True
            changed = change > CHANGE_LEVEL
            span["centre_changed"] = max(span["centre_changed"], float(changed[middle].mean()))
            span["centre_level"] = max(span["centre_level"], float(change[middle].mean()))
            span["edges_changed"] = max(span["edges_changed"], float(changed[~middle].mean()))
    return [
        {
            "start": s["start"],
            "end": s["end"],
            "centre_changed": round(s["centre_changed"], 4),
            "centre_level": round(s["centre_level"], 2),
            "edges_changed": round(s["edges_changed"], 4),
            "frozen": s["samples"] >= MIN_SAMPLES
            and s["centre_changed"] < FROZEN_SHARE
            and s["centre_level"] < FROZEN_LEVEL,
        }
        for s in found
    ]


def still_clips(db, settings, assets):
    """The generated clips in these assets whose picture doesn't move. Uploaded footage, video edits and
    offline placeholders aren't measured."""
    result = []
    for asset in assets:
        if not asset.media.get("mime", "").startswith("video/") or asset.source not in GENERATED or asset.demo_only:
            continue
        job = db.get(MediaJob, asset.tags.get("media_job_id") or "")
        if (
            not job
            or job.brief.get("operation") != "video"
            or job.brief.get("as_uploaded")
            or job.brief.get("execution_mode") == "fixture"
        ):
            continue
        # The logo ending is still by design.
        footage = float(asset.media.get("duration") or 0) - float((asset.tags.get("end_card") or {}).get("seconds", 0))
        sequence = asset.tags.get("video_sequence") or []
        segments = sequence or [{"index": 0, "start": 0, "duration": footage}]
        spans = [(s["start"], min(s["start"] + s["duration"], footage)) for s in segments]
        try:
            measured = clip_motion(storage(settings).read(asset.storage_key), spans)
        except (av.FFmpegError, ValueError):
            continue  # Unreadable media fails the media checks; there is nothing to measure here.
        for segment, clip in zip(segments, measured):
            if clip["frozen"]:
                result.append(clip | {"asset_id": asset.id, "clip": segment["index"] + 1 if sequence else 0})
    return result


def still_reason(clip):
    """What the held post shows for a frozen clip. The seconds are written so they aren't read as a time range
    (see clip_replace.flagged), which could also mark the next clip; problem_seconds places it instead."""
    where = (
        f"Clip {clip['clip']} (seconds {round(clip['start'], 1):g}–{round(clip['end'], 1):g})"
        if clip["clip"]
        else "The video"
    )
    edges = ", with movement only near the edges" if clip["edges_changed"] >= FROZEN_SHARE else ""
    return (
        f"{where} is frozen: the middle of the picture barely changes for the whole clip{edges}, so it plays "
        "like a still photo. Make it again with the people or the setting doing something and one camera move."
    )


def middle_second(clip):
    return round((clip["start"] + clip["end"]) / 2, 2)
