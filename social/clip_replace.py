"""Redo one part of a held post's generated media (a clip, a single take or the music), reusing the rest."""

import re
from copy import deepcopy

from sqlalchemy import select

from .db import uid
from .lifecycle import lock_post
from .models import Asset, CheckResult, MediaJob
from .prompting import PROMPT_LIMIT, utf16_trim
from .recovery import MUSIC_PROMPT_LIMIT

# Positive wording only: Runway models do not support negative prompts.
FIXES = {
    "text": "Every sign, window, awning and wall in frame is plain and unbranded, and any neighboring buildings "
    "appear only as blank rendered walls and greenery.",
    "people": "The whole scene is empty and unoccupied.",
}
_RANGE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:–|-|to)\s*(\d+(?:\.\d+)?)\s*(?:s\b|sec|second)", re.I)
_POINT = re.compile(r"\b(?:at|around|about|near)\s+(\d+(?:\.\d+)?)\s*(?:s\b|sec|second)", re.I)
GENERATED = {"ai_generated", "ai_edited"}


def source_job(db, post):
    """The media job whose finished video or image this post uses, or None."""
    if not post.asset_ids:
        return None
    return db.scalar(
        select(MediaJob)
        .where(MediaJob.output_asset_id.in_(post.asset_ids))
        .order_by(MediaJob.created_at.desc())
    )


def replacement(db, post):
    """A redo already requested for this post, unless it was removed from the queue."""
    return db.scalar(
        select(MediaJob)
        .where(MediaJob.brief[("replaces", "post_id")].as_string() == post.id, MediaJob.state != "cancelled")
        .order_by(MediaJob.created_at.desc())
    )


def failed_check(db, post, name):
    check = db.scalar(
        select(CheckResult)
        .where(CheckResult.post_id == post.id, CheckResult.name == name)
        .order_by(CheckResult.id.desc())
    )
    return check.details if check and not check.passed else {}


def failed_vision(db, post):
    return failed_check(db, post, "vision_review")


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


def _soundtrack(db, job):
    return next(
        (i for i in job.source_asset_ids if (a := db.get(Asset, i)) and a.media["mime"].startswith("audio/")),
        "",
    )


def parts(db, job, vision, audio):
    """Every part of a job's output that can be made again on its own."""
    brief = job.brief
    segments = brief.get("video_segments") or []
    prompts = {
        h["segment_index"]: h["prompt"] for h in job.task_history if "segment_index" in h and h.get("prompt")
    }
    result = []
    if segments and brief["operation"] == "video":
        if all(s.get("output_asset_id") for s in segments):
            marked = flagged(vision.get("reasons", []), segments)
            for s in segments:
                prompt = s.get("prompt") or prompts.get(s["index"], "")
                if not prompt:
                    return []
                result.append(
                    {
                        "key": str(s["index"]),
                        "kind": "clip",
                        "index": s["index"],
                        "label": f"clip {s['index'] + 1}",
                        "title": f"Clip {s['index'] + 1}",
                        "start": s["start"],
                        "end": s["start"] + s["duration"],
                        "duration": s["duration"],
                        "setting": (s.get("shot") or {}).get("setting", ""),
                        "system": s.get("system") or (s.get("shot") or {}).get("system", ""),
                        "prompt": suggested_prompt({"prompt": prompt}, vision),
                        "flagged": s["index"] in marked,
                        "limit": PROMPT_LIMIT,
                    }
                )
    elif not segments and brief["operation"] in {"video", "image"} and brief.get("generation_prompt"):
        noun = "image" if brief["operation"] == "image" else "video"
        result.append(
            {
                "key": "visual",
                "kind": "visual",
                "label": f"the {noun}",
                "title": f"The {noun}",
                "duration": brief["duration"] if noun == "video" else 0,
                "prompt": suggested_prompt({"prompt": brief["generation_prompt"]}, vision),
                "flagged": bool(vision),
                "limit": PROMPT_LIMIT,
            }
        )
    if brief["operation"] == "video" and brief.get("audio_prompt") and _soundtrack(db, job):
        result.append(
            {
                "key": "music",
                "kind": "music",
                "label": "the music",
                "title": "Music",
                "duration": brief["duration"],
                "prompt": brief["audio_prompt"],
                "flagged": bool(audio),
                "limit": MUSIC_PROMPT_LIMIT,
            }
        )
    return result


def clip_options(db, post):
    """What the post page offers for redoing part of a held post's media, or None when it does not apply."""
    if post.status != "held":
        return None
    pending = replacement(db, post)
    job = source_job(db, post)
    media = [a for i in post.asset_ids if (a := db.get(Asset, i))]
    if not job and not pending and not any(a.source in GENERATED for a in media):
        return None
    vision, audio = failed_vision(db, post), failed_check(db, post, "audio_review")
    options = parts(db, job, vision, audio) if job else []
    unavailable = ""
    if not options:
        unavailable = (
            "The generation record for this media wasn't found, so its parts can't be made again here. "
            if not job
            else "This media can't be split into parts that can be made again on their own. "
        ) + "Create the post again from the idea for new media."
    return {
        "pending": pending,
        "live": bool(job and job.brief["execution_mode"] == "live"),
        "noun": "image" if job and job.brief["operation"] == "image" else "video",
        "reasons": vision.get("reasons", []),
        "parts": options,
        "unavailable": unavailable,
    }


def replace_part(db, post, key, prompt, actor):
    """Queue a media job that makes one part again; the held post is replaced when the new media is ready."""
    lock_post(db, post)
    if post.status != "held":
        raise ValueError("Only a held post can have part of its media made again")
    pending = replacement(db, post)
    if pending:
        db.rollback()
        return pending
    job = source_job(db, post)
    if not job:
        raise ValueError(
            "The generation record for this media wasn't found. Create the post again from the idea instead."
        )
    options = {
        p["key"]: p for p in parts(db, job, failed_vision(db, post), failed_check(db, post, "audio_review"))
    }
    if not options:
        raise ValueError("This media can't be split into parts. Create the post again from the idea instead.")
    part = options.get(str(key))
    if not part:
        raise ValueError("Choose one of the parts listed on the post page")
    prompt = " ".join(str(prompt or "").split()) or part["prompt"]
    too_long = (
        len(prompt) > MUSIC_PROMPT_LIMIT
        if part["kind"] == "music"
        else len(prompt.encode("utf-16-le")) > PROMPT_LIMIT * 2
    )
    if too_long:
        raise ValueError(f"Keep the prompt under {part['limit']} characters")
    brief = deepcopy(job.brief) | {
        "workflow": "immediate",
        "create_posts": True,
        "replaces": {
            "post_id": post.id,
            "media_job_id": job.id,
            "part": part["key"],
            "label": part["label"],
            "clip_index": part.get("index"),
            "requested_by": actor,
        },
    }
    sources = list(job.source_asset_ids)
    soundtrack = _soundtrack(db, job)
    stage = "visual"
    if part["kind"] == "clip":
        old = next(s for s in job.brief["video_segments"] if s["index"] == part["index"])
        clip = {k: v for k, v in old.items() if k != "output_asset_id"} | {
            "prompt": prompt,
            # A different seed gives a different take even when the prompt is unchanged.
            "seed": (old["seed"] + 1) % 2**32,
            "replaced_asset_id": old["output_asset_id"],
        }
        brief["video_segments"] = [
            clip if s["index"] == part["index"] else s for s in job.brief["video_segments"]
        ]
        brief["soundtrack_asset_id"] = soundtrack
        if part["index"] == 0:
            brief["generation_prompt"] = prompt
        # Reused clips and music stay in the sources; the new clip is added when it is saved.
        sources = [i for i in sources if i != old["output_asset_id"]]
    elif part["kind"] == "visual":
        brief |= {
            "generation_prompt": prompt,
            "seed": (job.brief["seed"] + 1) % 2**32,
            "soundtrack_asset_id": soundtrack,
        }
        sources = [i for i in sources if i == soundtrack]
    else:
        brief |= {"audio_prompt": prompt, "soundtrack_asset_id": ""}
        sources = [i for i in sources if i != soundtrack]
        stage = "audio"
    created = MediaJob(
        batch_key=f"replace-{part['kind']}:{uid()}:{job.platform}",
        platform=job.platform,
        brief=brief,
        state="queued",
        stage=stage,
        source_asset_ids=sources,
    )
    db.add(created)
    db.commit()
    return created
