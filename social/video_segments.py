"""Saved per-clip plans and immutable edit inputs for longer Runway videos."""

import hashlib
import math

from PIL import Image, ImageOps

from .models import Asset
from .prompting import utf16_trim
from .renditions import video_rendition
from .storage import ingest, storage

# One slow move per clip while the structure stays rigid. Pull-back reveals and orbits are left out:
# they make the model invent parts of the structure the first frame doesn't show. No direction holds the
# camera still: with nothing else asked to move, that comes back as a frozen picture.
SHOT_DIRECTIONS = (
    "Establish the whole system in its setting; the camera slowly pushes in.",
    "Move closer to the same system's materials and joints with a slow push-in.",
    "Show a complementary side angle with a gentle lateral camera move.",
    "Follow the same system's repeating lines and existing light patterns with a slow lateral camera move.",
    "Return to a medium view that connects the details to the setting, with a gentle lateral move.",
    "Explore a different close-up of the existing finish and supports with a slow push-in.",
    "Use a low viewpoint with a restrained upward camera tilt.",
    "Show the whole system in its surroundings from a fixed viewpoint with a slow pedestal move.",
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


def with_original_cuts(brief, cuts, candidates):
    """Mark the original shots as available and send only the remaining footage to Runway."""
    from .runway import video_clip_limit

    available = {item["asset_id"]: item for item in candidates}
    placements = {"opening": [], "middle": [], "closing": []}
    remaining = sum(s["duration"] for s in brief["video_segments"])
    for cut in cuts:
        source = available.pop(cut.asset_id, None)
        if not source or (source["kind"] == "photo" and cut.start) or (
            source["kind"] == "video" and cut.start + cut.seconds > source["seconds"]
        ):
            raise ValueError("The planned original shot does not fit its source. Resume to replan.")
        placements[cut.placement].append({
            "duration": cut.seconds, "original_asset_id": cut.asset_id, "original_kind": source["kind"],
            "source_start": cut.start, "output_asset_id": cut.asset_id,
        })
        remaining -= cut.seconds
    middle = placements["middle"]
    if remaining < (4 if middle else 2):
        raise ValueError("The original shots leave too little time for the generated scenes. Resume to replan.")
    segments = plan_segments(remaining, video_clip_limit(brief["runway_video_model"]), 2 if middle else None)
    split = (len(segments) + 1) // 2
    segments = placements["opening"] + segments[:split] + middle + segments[split:] + placements["closing"]
    start = 0
    for i, segment in enumerate(segments):
        segment.update(index=i, start=start)
        start += segment["duration"]
    return segments


def original_shots(db, brief):
    from .asset_edit import ancestry
    from .real_sources import _cleared, real_asset

    originals = []
    permissions = {"live": brief["execution_mode"] == "live", "public": brief["rights"] != "not_public"}
    for segment in brief.get("video_segments", []):
        if source_id := segment.get("original_asset_id"):
            asset = db.get(Asset, source_id)
            if not asset or asset.removed_at or not real_asset(asset, **permissions):
                raise ValueError("An original shot is no longer cleared for this campaign.")
            originals.append(asset)
    sources, missing = ancestry(db, originals)
    if missing or any(a.removed_at or not _cleared(a, **permissions) for a in sources):
        raise ValueError("Source material for an original shot is no longer cleared for this campaign.")
    return originals


def sequence_clip(db, segment, files, profile):
    from .real_edit import EditItem, still_clip

    asset = db.get(Asset, segment["output_asset_id"]) if segment.get("output_asset_id") else None
    if not asset or asset.id not in files:
        raise ValueError("A planned video clip is missing; resume generation before assembly")
    clip = files[asset.id]
    if segment.get("original_asset_id") and asset.media["mime"].startswith("image/"):
        with Image.open(clip) as picture:
            clip = still_clip(ImageOps.exif_transpose(picture), EditItem(asset_id=asset.id, framing="fit"),
                              profile.video_width, profile.video_height, segment["duration"])
    return clip, segment.get("source_start", 0), segment["duration"]


def current_segment(job):
    return next((s for s in job.brief.get("video_segments", []) if not s.get("output_asset_id")), None)


def segment_brief(job):
    segment = current_segment(job) if job.stage == "visual" else None
    if segment is None:
        return job.brief
    if segment.get("prompt"):
        return job.brief | {
            "duration": segment["duration"],
            "generation_prompt": segment["prompt"],
            "seed": segment["seed"],
        }
    base = job.brief["generation_prompt"]
    if job.brief["operation"] == "video":
        count = len(job.brief["video_segments"])
        direction = SHOT_DIRECTIONS[min(segment["index"], len(SHOT_DIRECTIONS) - 1)]
        base = utf16_trim(base, 680) + (
            f" Shot {segment['index'] + 1} of {count}. {direction} "
            "Keep the same system, setting, geometry, materials and lighting throughout this sequence."
        )
    return job.brief | {
        "duration": segment["duration"],
        "generation_prompt": utf16_trim(base),
        "seed": (job.brief["seed"] + segment["index"]) % (2**32),
    }


def save_segment_prompt(db, job):
    segment = current_segment(job) if job.stage == "visual" else None
    if segment and not segment.get("prompt"):
        prepared = segment_brief(job)
        job.brief = job.brief | {
            "video_segments": [
                s | {"prompt": prepared["generation_prompt"], "seed": prepared["seed"]}
                if s["index"] == segment["index"]
                else s
                for s in job.brief["video_segments"]
            ]
        }
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
    data = video_rendition(
        data,
        int(width * scale) // 2 * 2,
        int(height * scale) // 2 * 2,
        segment["duration"],
        start=segment["start"],
    )
    asset = ingest(
        db,
        settings,
        data,
        f"edit-input-{job.id}-{segment['index'] + 1}.mp4",
        source=original.source,
        generator=original.generator,
        prompt=original.prompt,
        rights=original.rights,
        owner=original.owner,
        rights_expire_at=original.rights_expire_at,
        customer_permission=original.customer_permission,
        permission_reference=original.permission_reference,
        demo_only=original.demo_only,
        tags=original.tags
        | {
            "source_asset_ids": [original.id],
            "source_checksum": original.checksum,
            "edit_interval": {"start": segment["start"], "duration": segment["duration"]},
        },
    )
    if asset.media["duration"] + 0.1 < segment["duration"] or asset.media["duration"] > 30.1:
        raise ValueError("The edit interval does not fit the source or Runway's 30-second limit")
    job.brief = job.brief | {
        "video_segments": [
            s | {"input_asset_id": asset.id} if s["index"] == segment["index"] else s
            for s in job.brief["video_segments"]
        ]
    }
    db.commit()
    return asset
