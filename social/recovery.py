"""Plain-language reasons a post or media job stopped, and which step can be redone."""

import re

from pydantic import ValidationError
from sqlalchemy import select

from .db import now
from .models import Asset, CheckResult, Transition
from .prompting import PROMPT_LIMIT
from .provider_errors import ProviderError
from .video_segments import current_segment

CAPTION_FAILED = "Caption generation rejected: "
TASK_FAILED = "Runway couldn't make "
LEGACY_TASK_FAILED = "Runway task failed or was cancelled"
MUSIC_PROMPT_LIMIT = 1800

# validate_parts() messages, reworded for someone reading the post page.
CAPTION_REASONS = {
    "Caption contains a quantity absent from its cited facts": "The writer used a number that isn't in the approved facts it cited.",
    "Caption cites an unknown fact": "The writer cited a fact that isn't in the approved facts.",
    "Prices, offers and testimonials must keep exact approved wording": "The writer reworded a price, offer or testimonial. Those must keep the approved wording.",
    "Claims require fact IDs; nonfactual copy must use an empty citation list": "The writer made a claim without citing an approved fact.",
    "Caption needs approved fact content": "The writer didn't include any approved fact.",
    "A caption needs between one and twelve source-linked parts": "The writer returned an empty or overlong caption.",
    "Caption parts need nonempty text within the supported length": "The writer returned an empty or overlong line.",
    "Caption parts must be objects": "The writer's answer wasn't in the expected format.",
    "Composed caption parts need kind, text and fact_ids": "The writer's answer wasn't in the expected format.",
}

# check name: (what went wrong, what fixes it). Fix kinds pick the buttons on the post page.
CHECKS = {
    "grounding": ("The caption doesn't match the approved facts it was written from.", "caption"),
    "urls": ("A link in the caption isn't approved or doesn't load.", "caption"),
    "media_urls": ("The platform couldn't load the media file from storage.", "recheck"),
    "length": ("The caption is longer than this platform allows.", "caption"),
    "hashtags": (
        "The caption has too many hashtags, or ones that aren't approved for this platform.",
        "caption",
    ),
    "mentions_links": ("The caption has @mentions or links this platform's settings don't allow.", "caption"),
    "banned_phrases": ("The caption uses a banned phrase.", "caption"),
    "testimonials": (
        "The caption reads like a customer quote, and no customer permission is recorded.",
        "caption",
    ),
    "disclosure": ("The AI label can't be applied on this platform.", "recheck"),
    "rights": ("Media rights or customer permission are missing or expired.", "media"),
    "media": ("The media doesn't meet this platform's format limits.", "media"),
    "provenance_integrity": ("The media file changed after it was saved.", "media"),
    "duplicates": ("The caption is too similar to another recent post.", "caption"),
    "audio_review": ("The audio check found a problem with the sound.", "music"),
    "vision_review": ("The visual check found a problem in the picture.", "visual"),
    "model_review": ("The caption reviewer found a claim its cited facts don't support.", "caption"),
    "live_readiness": (
        "Live posting isn't ready: it needs live AI mode and facts and media that aren't demo-only.",
        "recheck",
    ),
}
FIXES = {
    "caption": "Write a new caption, or edit the current one.",
    "visual": "Regenerate the part with the problem below.",
    "music": "Regenerate the music below.",
    "media": "Correct the media details, then recheck the post.",
    "recheck": "Correct the cause, then recheck the post.",
}


def caption_error(exc):
    """A caption failure in words, without raw provider or validation output."""
    if isinstance(exc, ProviderError):
        return str(exc)
    if isinstance(exc, ValidationError):
        return "The writer's answer wasn't in the expected format."
    if type(exc) is ValueError:
        return CAPTION_REASONS.get(str(exc), str(exc))
    return f"An unexpected error stopped it ({type(exc).__name__})."


def _legacy_caption_reason(reason):
    # Before 2026-11-13 only the error type was saved for anything but provider errors.
    if not re.fullmatch(r"[A-Za-z_]+", reason):
        return reason
    if reason == "ValueError":
        return "The written caption didn't pass the fact rules (the exact reason wasn't saved for this post)."
    if reason == "ValidationError":
        return "The writer's answer wasn't in the expected format."
    return f"An unexpected error stopped it ({reason})."


def _short(value, limit=300):
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _check_details(check):
    d = check.details or {}
    if d.get("error_type"):
        return [f"The check couldn't run: {d.get('reason') or 'unknown error'}"]
    out = []
    review = d.get("review") if isinstance(d.get("review"), dict) else {}
    for item in [*d.get("reasons", []), *review.get("reasons", []), *d.get("errors", [])]:
        out.append(item.get("reason", "") if isinstance(item, dict) else item)
    for claim in [*d.get("unsupported_claims", []), *review.get("unsupported_claims", [])]:
        out.append(f"Unsupported: {claim}")
    if d.get("reason"):
        out.append(d["reason"])
    if d.get("matches"):
        out.append("Found: " + ", ".join(d["matches"]))
    if check.name == "length" and "length" in d:
        out.append(f"{d['length']} characters; the limit is {d.get('limit')}.")
    if check.name == "hashtags" and d.get("hashtags"):
        out.append("Hashtags: " + " ".join(d["hashtags"]))
    if check.name == "duplicates":
        if d.get("cross_platform_identical"):
            out.append("It's identical to a caption on another platform.")
        elif isinstance(d.get("maximum"), (int, float)):
            out.append(f"{d['maximum']:.0%} similar to a recent post.")
    if d.get("not_public_for_live"):
        out.append("Media marked not public can't be posted live.")
    if d.get("missing_source_assets"):
        out.append("Media it was made from is missing from the asset library.")
    if d.get("missing_customer_permission"):
        out.append("Customer media needs a recorded permission.")
    return [_short(x) for x in dict.fromkeys(x for x in out if x)][:6]


def held_reason(db, post):
    """Why a held post stopped and which steps can fix it, or None for any other status."""
    if post.status != "held":
        return None
    held = db.scalar(
        select(Transition)
        .where(Transition.post_id == post.id, Transition.to_status == "held")
        .order_by(Transition.id.desc())
    )
    reason = held.reason if held else ""
    if reason.startswith(CAPTION_FAILED):
        return {
            "caption_error": _legacy_caption_reason(reason.removeprefix(CAPTION_FAILED)),
            "problems": [],
            "reason": reason,
            "fixes": {"caption"},
            "assets": [],
        }
    names = reason.removeprefix("Pre-publish checks: ").split(", ")
    problems = []
    if reason and all(n in CHECKS for n in names):
        run = db.scalar(
            select(CheckResult.run_id).where(CheckResult.post_id == post.id).order_by(CheckResult.id.desc())
        )
        failed = db.scalars(
            select(CheckResult)
            .where(CheckResult.post_id == post.id, CheckResult.run_id == run, CheckResult.passed.is_(False))
            .order_by(CheckResult.id)
        ).all()
        for check in failed:
            title, fix = CHECKS.get(
                check.name, (check.name.replace("_", " ").capitalize() + " failed.", "recheck")
            )
            details = _check_details(check)
            if details and details[0].startswith("The check couldn't run"):
                fix = "recheck"
            problems.append(
                {"name": check.name, "title": title, "details": details, "fix": FIXES[fix], "kind": fix}
            )
    fixes = {p["kind"] for p in problems}
    return {
        "caption_error": "",
        "problems": problems,
        "reason": reason,
        "fixes": fixes,
        "assets": [i for i in post.asset_ids if db.get(Asset, i)] if "media" in fixes else [],
    }


def task_failed(job):
    """A Runway task that finished as failed or cancelled: polling it again cannot succeed."""
    return (
        job.state == "failed"
        and bool(job.task_id)
        and (job.error or "").startswith((TASK_FAILED, LEGACY_TASK_FAILED))
    )


def pending_step(job):
    """The generation step a job stopped at, with the prompt it would send."""
    brief = job.brief
    done = {h["stage"] for h in job.task_history}
    if brief["operation"] == "audio" or job.stage == "audio":
        # An audio-only job has one step; a video's music step follows its clips.
        finished = bool(job.task_history) if brief["operation"] == "audio" else "audio" in done
        if finished:
            return None
        label = "the audio" if brief["operation"] == "audio" else "the music"
        return {
            "kind": "music",
            "label": label,
            "prompt": brief.get("audio_prompt", ""),
            "limit": MUSIC_PROMPT_LIMIT,
        }
    segment = current_segment(job)
    if segment:
        return {
            "kind": "clip",
            "index": segment["index"],
            "label": f"clip {segment['index'] + 1}",
            "prompt": segment.get("prompt", ""),
            "limit": PROMPT_LIMIT,
        }
    if not brief.get("video_segments") and "visual" not in done:
        label = "the image" if brief["operation"] == "image" else "the video"
        return {
            "kind": "visual",
            "label": label,
            "prompt": brief.get("generation_prompt", ""),
            "limit": PROMPT_LIMIT,
        }
    return None


def runway_failure(job, task):
    """A Runway task failure in words. Only the failure code is kept, never Runway's free text."""
    step = pending_step(job)
    code = str(task.get("failureCode") or "")
    code = code if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code) else ""
    upper = code.upper()
    if task.get("status") != "FAILED":
        why = "The task was cancelled in Runway. Generate it again below."
    elif "SAFETY" in upper or "MODERATION" in upper:
        why = "Runway's content moderation declined it. Change the prompt below, then generate it again."
    elif "INTERNAL" in upper:
        why = "Runway had an internal error. Generating it again usually works."
    elif upper.startswith("ASSET") or "INPUT" in upper:
        why = "Runway couldn't use the reference media. Generate it again, or create the post with another image."
    else:
        why = "Runway reported that the task failed. Generating it again usually works."
    return f"{TASK_FAILED}{step['label'] if step else 'this step'}. {why}" + (
        f" Runway code: {code}." if code else ""
    )


def retry_option(job):
    """What the In progress page offers to generate again for a failed job, or None."""
    step = pending_step(job) if job.state == "failed" else None
    if not step or not step["prompt"] or (job.task_id and not task_failed(job)):
        return None
    return step


def retry_step(db, job, prompt):
    """Submit the failed step again with a new seed, and the edited prompt when one is given."""
    step = retry_option(job)
    if not step:
        raise ValueError("This step can't be generated again from here. Refresh the page, or use Resume.")
    text = " ".join(str(prompt or "").split()) or step["prompt"]
    too_long = (
        len(text) > MUSIC_PROMPT_LIMIT
        if step["kind"] == "music"
        else len(text.encode("utf-16-le")) > PROMPT_LIMIT * 2
    )
    if too_long:
        raise ValueError(f"Keep the prompt under {step['limit']} characters")
    brief = dict(job.brief)
    failed = {"task_id": job.task_id, "stage": job.stage, "error": job.error[:500], "at": now().isoformat()}
    if step["kind"] == "clip":
        failed["segment_index"] = step["index"]
        brief["video_segments"] = [
            s | {"prompt": text, "seed": (s["seed"] + 1) % 2**32} if s["index"] == step["index"] else s
            for s in brief["video_segments"]
        ]
        if step["index"] == 0:
            brief["generation_prompt"] = text
    elif step["kind"] == "visual":
        brief["generation_prompt"], brief["seed"] = text, (brief["seed"] + 1) % 2**32
    else:
        brief["audio_prompt"] = text
    brief["failed_tasks"] = [*brief.get("failed_tasks", []), failed]
    job.brief = brief
    job.task_id, job.submitted_at, job.state, job.error = "", None, "queued", ""
    db.commit()
    return job
