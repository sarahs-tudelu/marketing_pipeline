"""Regenerate one clip of a held multi-clip video, reusing its other clips, music and logo end card."""

import re
from copy import deepcopy

from sqlalchemy import select

from .db import uid
from .lifecycle import lock_post
from .models import Asset, CheckResult, MediaJob
from .prompting import PROMPT_LIMIT, utf16_trim

# Positive wording only: Runway models do not support negative prompts.
FIXES = {
    "text": "Every sign, window, awning and wall in frame is plain and unbranded, and any neighboring buildings "
    "appear only as blank rendered walls and greenery.",
    "people": "The whole scene is empty and unoccupied.",
}
_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:–|-|to)\s*(\d+(?:\.\d+)?)\s*(?:s\b|sec|second)", re.I)
_POINT = re.compile(r"\b(?:at|around|about|near)\s+(\d+(?:\.\d+)?)\s*(?:s\b|sec|second)", re.I)


def source_job(db, post):
    """The finished multi-clip media job behind a post, or None."""
    job = db.scalar(select(MediaJob).where(MediaJob.post_id == post.id).order_by(MediaJob.created_at.desc()))
    segments = (job.brief.get("video_segments") or []) if job else []
    if not job or job.state != "complete" or not segments:
        return None
    if not all(s.get("output_asset_id") and s.get("prompt") for s in segments):
        return None
    return job


def replacement(db, post):
    """A clip regeneration already requested for this post, unless it was removed from the queue."""
    return db.scalar(
        select(MediaJob)
        .where(MediaJob.brief[("replaces", "post_id")].as_string() == post.id, MediaJob.state != "cancelled")
        .order_by(MediaJob.created_at.desc())
    )


def failed_vision(db, post):
    check = db.scalar(
        select(CheckResult)
        .where(CheckResult.post_id == post.id, CheckResult.name == "vision_review")
        .order_by(CheckResult.id.desc())
    )
    return check.details if check and not check.passed else {}


def flagged(reasons, segments):
    """Indexes of clips overlapping the times a failed visual check mentions."""
    spans = []
    for text in reasons:
        spans += [(float(a), float(b)) for a, b in _RANGE.findall(text)]
        spans += [(float(a), float(a)) for a in _POINT.findall(text)]
    return {
        s["index"]
        for s in segments
        for start, end in spans
        if s["start"] <= end and start < s["start"] + s["duration"]
    }


def suggested_prompt(segment, details):
    """The clip's prompt plus positive wording for what the visual check found."""
    extra = []
    if details.get("garbled_text") or details.get("misused_logos"):
        extra.append(FIXES["text"])
    if details.get("unexpected_people"):
        extra.append(FIXES["people"])
    addition = " ".join(e for e in extra if e not in segment["prompt"])
    if not addition:
        return segment["prompt"]
    return utf16_trim(segment["prompt"], PROMPT_LIMIT - len(addition) - 1) + " " + addition


def clip_options(db, post):
    """What the post page shows for regenerating one clip, or None when it does not apply."""
    if post.status != "held":
        return None
    pending = replacement(db, post)
    job = source_job(db, post)
    if not job and not pending:
        return None
    details = failed_vision(db, post)
    segments = job.brief["video_segments"] if job else []
    marked = flagged(details.get("reasons", []), segments)
    return {
        "pending": pending,
        "live": bool(job and job.brief["execution_mode"] == "live"),
        "reasons": details.get("reasons", []),
        "clips": [
            {
                "index": s["index"],
                "start": s["start"],
                "end": s["start"] + s["duration"],
                "duration": s["duration"],
                "setting": (s.get("shot") or {}).get("setting", ""),
                "system": s.get("system") or (s.get("shot") or {}).get("system", ""),
                "prompt": suggested_prompt(s, details),
                "flagged": s["index"] in marked,
            }
            for s in segments
        ],
    }


def replace_clip(db, post, index, prompt, actor):
    """Queue a media job that regenerates one clip; the held post is replaced when the new video is ready."""
    lock_post(db, post)
    if post.status != "held":
        raise ValueError("Only a held post can have a clip regenerated")
    pending = replacement(db, post)
    if pending:
        db.rollback()
        return pending
    job = source_job(db, post)
    if not job:
        raise ValueError(
            "This post's video is not made of separate clips. Create it again from the idea instead."
        )
    segments = job.brief["video_segments"]
    if not 0 <= index < len(segments):
        raise ValueError("Choose one of the video's clips")
    old = segments[index]
    prompt = " ".join(str(prompt or "").split()) or suggested_prompt(old, failed_vision(db, post))
    if len(prompt.encode("utf-16-le")) > PROMPT_LIMIT * 2:
        raise ValueError(f"Keep the clip prompt under {PROMPT_LIMIT} characters")
    clip = {k: v for k, v in old.items() if k != "output_asset_id"} | {
        "prompt": prompt,
        # A different seed gives a different take even when the prompt is unchanged.
        "seed": (old["seed"] + 1) % 2**32,
        "replaced_asset_id": old["output_asset_id"],
    }
    soundtrack = next(
        (i for i in job.source_asset_ids if (a := db.get(Asset, i)) and a.media["mime"].startswith("audio/")),
        "",
    )
    brief = deepcopy(job.brief) | {
        "video_segments": [clip if s["index"] == index else s for s in segments],
        "workflow": "immediate",
        "create_posts": True,
        "soundtrack_asset_id": soundtrack,
        "replaces": {"post_id": post.id, "media_job_id": job.id, "clip_index": index, "requested_by": actor},
    }
    if index == 0:
        brief["generation_prompt"] = prompt
    created = MediaJob(
        batch_key=f"replace-clip:{uid()}:{job.platform}",
        platform=job.platform,
        brief=brief,
        state="queued",
        stage="visual",
        # Reused clips and music stay in the sources; the new clip is added when it is saved.
        source_asset_ids=[i for i in job.source_asset_ids if i != old["output_asset_id"]],
    )
    db.add(created)
    db.commit()
    return created
