"""Saved per-clip plans and immutable edit inputs for longer Runway videos."""

import hashlib
import math

from .models import Asset
from .prompting import utf16_trim
from .renditions import video_rendition
from .storage import ingest, storage

SHOT_DIRECTIONS = (
    "Establish the setting with a slow wide reveal.",
    "Move closer to the same system's materials and joints.",
    "Show a complementary side angle with a gentle lateral camera move.",
    "Frame the same system's repeating lines and existing light patterns.",
    "Return to a medium view that connects the details to the setting.",
    "Explore a different close-up of the existing finish and supports.",
    "Use a low viewpoint with a restrained upward camera tilt.",
    "Widen gradually to show the same system in its surroundings.",
    "Finish with a calm, wide composition and a gentle camera settle.",
)


def plan_segments(duration, maximum, count=None):
    """Split duration into near-equal whole-second clips of at most maximum seconds."""
    count = max(count or 0, math.ceil(duration / maximum))
    base, extra = divmod(duration, count)
    result, start = [], 0
    for index in range(count):
        length = base + (index < extra)
        result.append({"index": index, "start": start, "duration": length})
        start += length
    return result


def current_segment(job):
    return next((s for s in job.brief.get("video_segments", []) if not s.get("output_asset_id")), None)


def segment_brief(job):
    segment = current_segment(job) if job.stage == "visual" else None
    if segment is None:
        return job.brief
    if segment.get("prompt"):
        return job.brief | {"duration": segment["duration"], "generation_prompt": segment["prompt"],
                            "seed": segment["seed"]}
    base = job.brief["generation_prompt"]
    if job.brief["operation"] == "video":
        count = len(job.brief["video_segments"])
        direction = SHOT_DIRECTIONS[min(segment["index"], len(SHOT_DIRECTIONS) - 1)]
        base = utf16_trim(base, 680) + (
            f" Shot {segment['index'] + 1} of {count}. {direction} "
            "Keep the same system, setting, geometry, materials and lighting throughout this sequence."
        )
    return job.brief | {"duration": segment["duration"], "generation_prompt": utf16_trim(base),
                        "seed": (job.brief["seed"] + segment["index"]) % (2 ** 32)}


def save_segment_prompt(db, job):
    segment = current_segment(job) if job.stage == "visual" else None
    if segment and not segment.get("prompt"):
        prepared = segment_brief(job)
        job.brief = job.brief | {"video_segments": [s | {"prompt": prepared["generation_prompt"], "seed": prepared["seed"]}
                                                 if s["index"] == segment["index"] else s
                                                 for s in job.brief["video_segments"]]}
        db.commit()


def edit_reference(db, settings, job):
    segment = current_segment(job)
    if segment is None or job.brief["operation"] != "video_edit":
        return None
    if segment.get("input_asset_id"):
        return db.get(Asset, segment["input_asset_id"])
    original = db.get(Asset, job.brief["reference_ids"][0])
    data = storage(settings).read(original.storage_key)
    if hashlib.sha256(data).hexdigest() != original.checksum:
        raise ValueError("Source integrity failed before splitting the video")
    width, height = original.media["width"], original.media["height"]
    scale = min(1, 1920 / max(width, height), 1080 / min(width, height))
    data = video_rendition(data, int(width * scale) // 2 * 2, int(height * scale) // 2 * 2,
                           segment["duration"], start=segment["start"])
    asset = ingest(db, settings, data, f"edit-input-{job.id}-{segment['index'] + 1}.mp4",
                   source=original.source, generator=original.generator, prompt=original.prompt,
                   rights=original.rights, owner=original.owner, rights_expire_at=original.rights_expire_at,
                   customer_permission=original.customer_permission, permission_reference=original.permission_reference,
                   demo_only=original.demo_only, tags=original.tags | {
                       "source_asset_ids": [original.id], "source_checksum": original.checksum,
                       "edit_interval": {"start": segment["start"], "duration": segment["duration"]},
                   })
    if asset.media["duration"] + 0.1 < segment["duration"] or asset.media["duration"] > 30.1:
        raise ValueError("The edit interval does not fit the source or Runway's 30-second limit")
    job.brief = job.brief | {"video_segments": [s | {"input_asset_id": asset.id} if s["index"] == segment["index"] else s
                                             for s in job.brief["video_segments"]]}
    db.commit()
    return asset
