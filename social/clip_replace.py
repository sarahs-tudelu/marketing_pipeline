"""Redo parts of a held post's generated media (clips, a single take or the music), reusing the rest.

A failed visual check starts this automatically (see auto_regenerate); a person can also start it."""

import re
from copy import deepcopy

from sqlalchemy import select

from .db import uid
from .lifecycle import lock_post, transition
from .models import Asset, CheckResult, MediaJob
from .prompting import PROMPT_LIMIT, utf16_trim
from .recovery import MUSIC_PROMPT_LIMIT

# Positive wording only: Runway models do not support negative prompts.
FIXES = {
    "text": "Every sign, window, awning and wall in frame is plain and unbranded, and any neighboring buildings "
    "appear only as blank rendered walls and greenery.",
    "people": "The whole scene is empty and unoccupied.",
}
_UNIT = r"\s*(?:s|secs?|seconds?)\b"
_RANGE = re.compile(
    rf"(?:\bbetween\s+(\d+(?:\.\d+)?)(?:{_UNIT})?\s+and|(\d+(?:\.\d+)?)(?:{_UNIT})?\s*(?:–|—|-|to))"
    rf"\s*(\d+(?:\.\d+)?){_UNIT}",
    re.I,
)
_POINT = re.compile(rf"\b(?:at|around|about|near)\s+(\d+(?:\.\d+)?){_UNIT}", re.I)
_CLOCK = re.compile(r"\b(\d{1,2}):([0-5]\d)\b")
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
    details = (check.details or {}) if check and not check.passed else {}
    # A check that couldn't run found nothing to fix in the media.
    return {} if details.get("error_type") else details


def failed_vision(db, post):
    return failed_check(db, post, "vision_review")


def flagged(vision, segments):
    """Indexes of clips holding the frames a failed visual check flagged, or the times its reasons mention."""
    spans = [(float(t), float(t)) for t in vision.get("problem_seconds") or [] if isinstance(t, (int, float))]
    for text in vision.get("reasons", []):
        spans += [(float(a or b), float(c)) for a, b, c in _RANGE.findall(text)]
        spans += [(float(a), float(a)) for a in _POINT.findall(text)]
        spans += [(int(m) * 60 + int(s),) * 2 for m, s in _CLOCK.findall(text)]
    return {
        s["index"]
        for s in segments
        for start, end in spans
        if s["start"] <= end and start < s["start"] + s["duration"]
    }


def scene_plan(target):
    """The saved plan of a clip or single take animated from a generated scene image, or {}."""
    plan = target.get("reference_plan") or {}
    return plan if plan.get("scene_reference") and plan.get("scene_prompt") else {}


def findings(vision, segments, part):
    """The visual check's reasons that concern one part: those placing a problem in its time range. A reason
    that doesn't say where goes to the clips the check's frame times flagged, or to every clip if none were."""
    reasons = [r for r in vision.get("reasons", []) if isinstance(r, str) and r.strip()]
    if part["kind"] == "clip":
        anywhere = part["flagged"] or not flagged(vision, segments)
        reasons = [
            r
            for r in reasons
            if (part["index"] in where if (where := flagged({"reasons": [r]}, segments)) else anywhere)
        ]
    if not reasons and (part["flagged"] or part["kind"] != "clip"):
        reasons = [
            text
            for key, text in (
                ("garbled_text", "Garbled or invented lettering is visible."),
                ("misused_logos", "An invented or misused logo is visible."),
                ("unexpected_people", "People appear who weren't requested."),
            )
            if vision.get(key)
        ]
    return reasons


def correction(job, part, prompt, vision, plan):
    """What the planner needs to make this part again: what the visual check found in it, the scene it
    was animated from and the operator's edit. Corrections saved during the first run don't carry over."""
    reasons = findings(vision, job.brief.get("video_segments") or [], part)
    # The displayed suggestion already includes model-written corrections; submitting it unchanged
    # does not turn those suggestions (or the original generated text) into an operator instruction.
    edited = prompt != " ".join(part["prompt"].split())
    retry = {
        "source": "held_post",
        "previous_prompt": part["base"],
        "creative_changes": plan.get("creative_changes", ""),
        "scene_requirements": plan.get("scene_requirements", []),
        "requested_prompt": prompt,
        "prompt_edited": edited,
        **({
            "operator_prompt": prompt,
            "operator_edits": [{"before": part["prompt"], "after": prompt}],
        } if edited else {}),
    }
    if reasons:
        covers = (
            f"{part['start']}–{part['end']} seconds of the video" if part["kind"] == "clip" else part["label"]
        )
        retry["video_check"] = {"reasons": reasons, "covers": covers}
        retry["review"] = {"passed": False, "reasons": reasons}
        if plan:
            retry["scene"] = plan["scene_reference"]
    return retry


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
    if segments and brief["operation"] in {"video", "video_edit"}:
        if all(s.get("output_asset_id") for s in segments):
            marked = flagged(vision, segments)
            for s in segments:
                if s.get("original_asset_id"):
                    continue
                prompt = s.get("prompt") or prompts.get(s["index"], "")
                if not prompt:
                    return []
                # A clip animated from a generated scene image is corrected through that scene: its
                # description is edited, and the camera movement is planned again with it.
                plan = scene_plan(s)
                base = plan.get("scene_prompt") or prompt
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
                        "base": base,
                        "prompt": suggested_prompt({"prompt": base}, vision),
                        "scene": bool(plan),
                        "camera": prompt if plan else "",
                        "flagged": s["index"] in marked,
                        "limit": PROMPT_LIMIT,
                    }
                )
    elif not segments and brief["operation"] in {"video", "video_edit", "image"} and brief.get("generation_prompt"):
        noun = "image" if brief["operation"] == "image" else "video"
        plan = scene_plan(brief) if noun == "video" else {}
        base = plan.get("scene_prompt") or brief["generation_prompt"]
        result.append(
            {
                "key": "visual",
                "kind": "visual",
                "label": f"the {noun}",
                "title": f"The {noun}",
                "duration": brief["duration"] if noun == "video" else 0,
                "base": base,
                "prompt": suggested_prompt({"prompt": base}, vision),
                "scene": bool(plan),
                "camera": brief["generation_prompt"] if plan else "",
                "flagged": bool(vision),
                "limit": PROMPT_LIMIT,
            }
        )
    soundtrack = _soundtrack(db, job)
    if brief["operation"] in {"video", "video_edit"} and (soundtrack or not brief.get("audio_prompt")):
        from .video_rules import AMBIENT_STYLES, next_ambient_style

        # A video made before every video had sound gets calm ambient music from Runway.
        result.append(
            {
                "key": "music",
                "kind": "music",
                "label": "the music",
                "title": "Music" if soundtrack else "Music (this video has none yet)",
                "duration": brief["duration"],
                "prompt": brief.get("audio_prompt") or AMBIENT_STYLES[next_ambient_style(db)][1],
                "flagged": bool(audio) or not soundtrack,
                "limit": MUSIC_PROMPT_LIMIT,
            }
        )
    return result


def review_evidence(db, job, vision):
    from .video_repair import evidence_times, shots

    if job.brief.get("operation") not in {"video", "video_edit"}:
        return []
    output = db.get(Asset, job.output_asset_id)
    if not output:
        return []
    result = []
    times = [d["second"] for d in vision.get("video_defects", [])] or vision.get("problem_seconds", [])
    if not times:
        times = [float(a or b) for reason in vision.get("reasons", []) for a, b, _ in _RANGE.findall(reason)]
        times += [float(t) for reason in vision.get("reasons", []) for t in _POINT.findall(reason)]
        times += [int(m)*60 + int(s) for reason in vision.get("reasons", []) for m, s in _CLOCK.findall(reason)]
    for shot in shots(db, job):
        start, length = shot["start"], shot["duration"]
        at = next((t for t in times if start <= t < start + length), None)
        if at is None:
            continue
        plan = shot.get("reference_plan") or {}
        result.append({"clip": shot["index"] + 1, "asset_id": output.id, "second": at,
                       "times": [round(start + t, 2) for t in evidence_times(at - start, length)],
                       "references": plan.get("references", []), "scene": plan.get("scene_reference")})
    return result


def needs_runway_music(db, post):
    """True when a held generated video has no sound: Runway composes its music again (see replace_part)."""
    media = [a for i in post.asset_ids if (a := db.get(Asset, i))]
    silent = [
        a
        for a in media
        if a.media["mime"].startswith("video/") and a.source in GENERATED and not a.media.get("audio_codecs")
    ]
    job = source_job(db, post) if silent else None
    return bool(job and any(p["kind"] == "music" for p in parts(db, job, {}, {})))


def clip_options(db, post):
    """What the post page offers for redoing part of a held post's media, or None when it does not apply."""
    if post.status not in {"held", "awaiting_review"}:
        return None
    pending = replacement(db, post)
    job = source_job(db, post)
    if job and (job.brief.get("as_uploaded") or job.brief.get("operation") == "real") and not pending:
        # The admin's own media; there is nothing generated to make again. A real project edit is changed
        # by describing the change (social/media_changes.py).
        return None
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
        # Automatic replacements already made in a row, and the limit, when this media came from one.
        "auto_attempts": auto_attempts(job),
        "auto_limit": int((job.brief.get("replaces") or {}).get("auto_limit") or 0) if job else 0,
        "live": bool(job and job.brief["execution_mode"] == "live"),
        "noun": "image" if job and (job.brief["operation"] == "image" or job.brief.get("real_format") == "photo")
        else "video",
        "reasons": vision.get("reasons", []),
        "parts": options,
        "unavailable": unavailable,
        "repair_hold": job.brief.get("repair_hold", "") if job and vision else "",
        "evidence": review_evidence(db, job, vision) if job and vision else [],
    }


def replace_part(db, post, key, prompt, actor):
    """Queue a media job that makes one part again; the held post is replaced when the new media is ready."""
    return replace_parts(db, post, [(key, prompt)], actor)


def _label(chosen, clips):
    if len(chosen) == 1:
        return chosen[0]["label"]
    if len(chosen) == len(clips):
        return f"all {len(chosen)} clips"
    numbers = [str(p["index"] + 1) for p in chosen]
    return "clips " + ", ".join(numbers[:-1]) + " and " + numbers[-1]


def replace_parts(db, post, requests, actor, *, auto_attempt=0, auto_limit=0, request="", repairs=None):
    """Queue one media job that makes the requested parts again, reusing the rest. Several clips can be
    made again together. The held post is replaced when the new media is ready. `request` is the admin's
    description of the change, when the prompts were written from one (social/media_changes.py)."""
    lock_post(db, post)
    if post.status not in {"held", "awaiting_review"}:
        raise ValueError("Stop scheduled delivery before changing the media")
    pending = replacement(db, post)
    if pending:
        db.rollback()
        return pending
    job = source_job(db, post)
    if not job:
        raise ValueError(
            "The generation record for this media wasn't found. Create the post again from the idea instead."
        )
    vision = failed_vision(db, post)
    listed = parts(db, job, vision, failed_check(db, post, "audio_review"))
    options = {p["key"]: p for p in listed}
    if not options:
        raise ValueError("This media can't be split into parts. Create the post again from the idea instead.")
    chosen = []
    for key, prompt in requests:
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
        chosen.append((part, prompt))
    if len(chosen) > 1 and {p["kind"] for p, _ in chosen} != {"clip"}:
        raise ValueError("Only clips can be made again together")
    chosen.sort(key=lambda c: c[0].get("index") or 0)
    first = chosen[0][0]
    # The first run's retries and last step are its own history, not the replacement's.
    brief = {
        k: v for k, v in deepcopy(job.brief).items()
        if k not in {"failed_tasks", "active_generation", "runway_retry_at", "repair_hold", "repair_routes", "repair_verdict"}
    } | {
        "workflow": "immediate",
        "create_posts": True,
        "replaces": {
            "post_id": post.id,
            "media_job_id": job.id,
            "part": ",".join(p["key"] for p, _ in chosen),
            "label": _label([p for p, _ in chosen], [p for p in listed if p["kind"] == "clip"]),
            "clip_index": first.get("index"),
            "count": len(chosen),
            "requested_by": actor,
            **({"auto_attempt": auto_attempt, "auto_limit": auto_limit} if auto_attempt else {}),
            **({"request": request} if request else {}),
        },
    }
    sources = list(job.source_asset_ids)
    soundtrack = _soundtrack(db, job)
    stage = "visual"
    if first["kind"] == "clip":
        segments = list(job.brief["video_segments"])
        for part, prompt in chosen:
            old = next(s for s in segments if s["index"] == part["index"])
            plan = scene_plan(old)
            # A scene clip is planned again from the edited scene description, like Generate corrected
            # scene; the old plan's scene stays in the correction for the planner to diagnose.
            dropped = {"output_asset_id", "scene_retry", "repair", "source_start", "playback_rate"} | ({"reference_plan"} if plan else set())
            clip = {k: v for k, v in old.items() if k not in dropped} | {
                "prompt": prompt,
                # A different seed gives a different take even when the prompt is unchanged.
                "seed": (old["seed"] + 1) % 2**32,
                "replaced_asset_id": old["output_asset_id"],
                "scene_retry": correction(job, part, prompt, vision, plan),
            }
            segments = [clip if s["index"] == part["index"] else s for s in segments]
            if part["index"] == 0:
                brief["generation_prompt"] = prompt
            # Reused clips and music stay in the sources; the new clip is added when it is saved.
            sources = [i for i in sources if i != old["output_asset_id"]]
        brief["video_segments"] = segments
        brief["soundtrack_asset_id"] = soundtrack
    elif first["kind"] == "visual":
        prompt = chosen[0][1]
        plan = scene_plan(job.brief) if first["scene"] else {}
        if plan:
            brief.pop("reference_plan", None)
        brief |= {
            "generation_prompt": prompt,
            "seed": (job.brief["seed"] + 1) % 2**32,
            "soundtrack_asset_id": soundtrack,
            "scene_retry": correction(job, first, prompt, vision, plan),
        }
        sources = [i for i in sources if i == soundtrack]
    else:
        from .video_rules import AMBIENT_STYLES

        prompt = chosen[0][1]
        brief |= {"audio_prompt": prompt, "soundtrack_asset_id": ""}
        brief.pop("audio_fallback", None)
        if not job.brief.get("audio_reference_ids"):
            brief["soundtrack_model"] = "music"
        style = next((k for k, (_, direction) in AMBIENT_STYLES.items() if direction == prompt), None)
        if style:
            brief["ambient_style"] = style
        sources = [i for i in sources if i != soundtrack]
        stage = "audio"
    state = "queued"
    if repairs:
        from .video_repair import apply_repairs

        stage = apply_repairs(db, job, brief, sources, repairs)
        if stage == "render":
            stage, state = "visual", "rendering"
    created = MediaJob(
        batch_key=f"replace-{first['kind']}:{uid()}:{job.platform}",
        platform=job.platform,
        brief=brief,
        state=state,
        stage=stage,
        source_asset_ids=sources,
    )
    db.add(created)
    post.human_review_required, post.human_approved_digest = True, ""
    post.approved_digest = ""
    if post.status == "awaiting_review":
        reason = f"Media changes requested: {request[:300]}" if request else "Media changes requested; waiting for replacement"
        transition(db, post, "held", reason, actor)
    db.commit()
    return created


def auto_attempts(job):
    """How many automatic replacements in a row led to this job's media (0 when a person asked for it)."""
    return int((job.brief.get("replaces") or {}).get("auto_attempt") or 0) if job else 0


def auto_regenerate(db, settings, post):
    """Queue one evidence-based repair for each affected generated shot."""
    limit = settings.media_auto_retries
    if post.status != "held" or not limit:
        return None
    vision = failed_vision(db, post)
    job = source_job(db, post)
    if not vision or not job or job.brief.get("as_uploaded") or replacement(db, post):
        return None
    if str(vision.get("request_conflict") or "").strip():
        # The shot itself asks for something Tudelu doesn't make; another take would fail the same way.
        return None
    attempt = auto_attempts(job) + 1
    if attempt > limit:
        job.brief = job.brief | {"repair_hold": "The automatic repair limit was reached. Compare the footage with its references."}
        db.commit()
        return None
    visual = [p for p in parts(db, job, vision, {}) if p["kind"] != "music"]
    segments = job.brief.get("video_segments") or []
    original_indices = {s["index"] for s in segments if s.get("original_asset_id")}
    if original_indices and original_indices.intersection(flagged(vision, segments)):
        job.brief = job.brief | {"repair_hold": "The review flagged an original library shot. Inspect the footage before replacing it."}
        db.commit()
        return None
    repairs = {}
    if job.brief.get("operation") in {"video", "video_edit"}:
        from .video_repair import choose

        repairs, hold = choose(db, job, vision, soundtrack=_soundtrack(db, job))
        if hold:
            job.brief = job.brief | {"repair_hold": hold}
            db.commit()
            return None
        chosen = [p for p in visual if p.get("index", 0) in repairs]
    else:
        chosen = [p for p in visual if p["flagged"]] or visual
    if not chosen:
        return None
    try:
        return replace_parts(
            db, post, [(p["key"], "") for p in chosen], "pipeline", auto_attempt=attempt, auto_limit=limit, repairs=repairs
        )
    except ValueError:
        db.rollback()
        return None
