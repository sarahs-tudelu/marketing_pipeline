"""Route a held shot using the visual review of its delivered pixels and its saved inputs."""

from .models import Asset, MediaJob
from .real_edit import video_frames
from .real_sources import real_asset
from .reference_grounding import asset_record, read_reference
from .video_segments import current_segment

REPAIR_LABELS = {
    "trim": "Trim clean footage",
    "library": "Use matching library media",
    "scene": "Correct the starting image",
    "motion": "Reanimate the approved image",
    "aleph": "Edit the failed clip with Aleph",
}


def library_records(db, settings, assets):
    from .media_search import campaign_scores, library_media, record

    if not settings.media_auto_retries:
        return []
    for asset in assets:
        if not asset.media["mime"].startswith("video/"):
            continue
        job = db.get(MediaJob, asset.tags.get("media_job_id") or "")
        if not job or job.brief.get("operation") not in {"video", "video_edit"}:
            continue
        brief = job.brief
        if not brief.get("prompt") or not brief.get("rights"):
            # Older generation records can still be reviewed without offering a library substitution.
            continue
        scope = set(brief.get("repair_library_scope", (brief.get("campaign_reference_ids") or []) +
                             (brief.get("original_candidates") or [])))
        rejected = set(brief.get("rejected_original_ids", []))
        media = library_media(
            db,
            product=brief.get("product", "company"),
            live=brief["execution_mode"] == "live",
            public=brief["rights"] != "not_public",
            allowed=lambda a: (not scope or a.id in scope) and a.id not in rejected,
            limit=None,
        )
        scores = campaign_scores(settings, brief["prompt"], media)
        ranked = sorted(media, key=lambda a: -scores[a.id])
        # Reserve room for recorded demonstrations even in a photo-heavy library.
        shown = {
            r.get("asset_id")
            for s in shots(db, job)
            for r in (s.get("reference_plan") or {}).get("references", [])
        }
        choices = list(
            dict.fromkeys(
                [a for a in ranked if a.id in shown]
                + [a for a in ranked if a.media["mime"].startswith("video/")][:6]
                + ranked[:10]
            )
        )
        return [r for a in choices if (r := record(a, settings))["description"] or a.id in shown]
    return []


def shots(db, job):
    if segments := job.brief.get("video_segments"):
        return segments
    raw = next(
        (a for i in job.source_asset_ids if (a := db.get(Asset, i)) and a.media["mime"].startswith("video/")),
        None,
    )
    if not raw:
        return []
    return [
        {
            "index": 0,
            "start": 0,
            "duration": min(job.brief["duration"], int(raw.media["duration"])),
            "output_asset_id": raw.id,
            "prompt": job.brief.get("generation_prompt", ""),
            "seed": job.brief["seed"],
            "reference_plan": job.brief.get("reference_plan", {}),
        }
    ]


def choose(db, job, vision, *, soundtrack):
    """All observed defects must be addressable. An ambiguous interval never triggers a blanket redo."""
    timeline = shots(db, job)
    by_clip = {}
    defects = vision.get("video_defects") or []
    for defect in defects:
        t = defect["second"]
        shot = next((s for s in timeline if s["start"] <= t < s["start"] + s["duration"]), None)
        if not shot or shot.get("original_asset_id") or defect["kind"] == "uncertain":
            return (
                {},
                "Compare the flagged frames with the product reference before requesting another render.",
            )
        by_clip.setdefault(shot["index"], []).append(defect)
    if not by_clip:
        return {}, "Recheck this post for a timestamped repair diagnosis or review the footage manually."
    all_times = vision.get("problem_seconds") or []
    if any(
        not any(s["index"] in by_clip and s["start"] <= t < s["start"] + s["duration"] for s in timeline)
        for t in all_times
    ):
        return {}, "The review flags other footage without a matching diagnosis. Inspect the complete render."
    repairs = {}
    for shot in timeline:
        issues = by_clip.get(shot["index"], [])
        if not issues:
            continue
        start, length = shot["start"], shot["duration"]
        plan = shot.get("reference_plan") or {}
        prior = shot.get("repair_history") or []
        components = sorted({d["component"] for d in issues})
        repeated = any(set(h["components"]) & set(components) for h in prior)
        required = any(d["needs_operation"] for d in issues)
        corrected = " ".join(dict.fromkeys(d["correction"] for d in issues))
        item = {
            "components": components,
            "second": issues[0]["second"] - start,
            "prompt": corrected,
            "source_id": shot["output_asset_id"],
            "needs_operation": required,
        }
        kinds = {d["kind"] for d in issues}
        a = max(d["clean_start"] for d in issues)
        b = min(d["clean_end"] for d in issues)
        bad_times = [d["second"] for d in issues] + list(vision.get("problem_seconds") or [])
        trim = (
            soundtrack
            and not required
            and start <= a < b <= start + length
            and b - a >= max(2, length * 0.75)
            and not any(a <= t < b for t in bad_times)
        )
        if trim and not repeated and not prior:
            item.update(route="trim", source_start=a - start, playback_rate=(b - a) / length)
        elif (
            repeated
            or shot.get("playback_rate")
            or required
            and "motion" in kinds
            or job.brief["operation"] == "video_edit"
            and kinds != {"detail"}
        ):
            candidate = next(
                (d for d in issues if d.get("library_asset_id") in vision.get("repair_library_ids", [])), {}
            )
            asset = db.get(Asset, candidate["library_asset_id"]) if candidate else None
            live = job.brief["execution_mode"] == "live"
            if (
                asset
                and not asset.removed_at
                and real_asset(asset, live=live, public=job.brief["rights"] != "not_public")
            ):
                video = asset.media["mime"].startswith("video/")
                at = candidate.get("library_start", 0)
                if (video and at + length <= asset.media["duration"] + 0.01) or (not video and not required):
                    item.update(route="library", asset_id=asset.id, source_start=at if video else 0)
            if "route" not in item:
                return {}, "This defect needs matching library footage or human review before another render."
        elif "opening" in kinds:
            item["route"] = "scene"
        elif kinds == {"detail"} and (
            plan.get("references")
            or job.brief["operation"] == "video_edit"
            and job.brief.get("reference_ids")
        ):
            item["route"] = "aleph"
        elif not plan.get("scene_check", {}).get("passed") or not plan.get("scene_reference"):
            return {}, "The starting image has no saved approval. Compare the footage and reference first."
        elif kinds == {"motion"}:
            item["route"] = "motion"
        else:
            return (
                {},
                "Several different defects affect this shot. Review its frames before choosing a repair.",
            )
        repairs[shot["index"]] = item
    return repairs, ""


def apply_repairs(db, job, brief, sources, repairs):
    """Compose one replacement, including zero-credit edits, before the job is made visible."""
    old = shots(db, job)
    by_index = {s["index"]: s for s in brief.get("video_segments", [])}
    for s in old:
        if s["index"] not in repairs:
            by_index.setdefault(s["index"], s)
            continue
        r = repairs[s["index"]]
        route = r["route"]
        target = dict(by_index.get(s["index"], s))
        target.pop("output_asset_id", None)
        target.pop("source_start", None)
        target.pop("playback_rate", None)
        target["repair"] = r
        target["repair_history"] = [
            *s.get("repair_history", []),
            {"components": r["components"], "route": route},
        ]
        if route == "scene":
            target.pop("reference_plan", None)
            target["scene_retry"] = {
                **target.get("scene_retry", brief.get("scene_retry", {})),
                "failed_clip_id": r["source_id"],
                "failed_second": r["second"],
            }
        else:
            target.pop("scene_retry", None)
            target["reference_plan"] = s.get("reference_plan", {})
        if route in {"motion", "aleph"}:
            from .prompting import utf16_trim

            preserve = ("Keep the recorded camera path, framing, timing and all unaffected details."
                        if route == "aleph" else "Start from the supplied approved image and maintain the visible construction.")
            target["prompt"] = utf16_trim(r["prompt"] + " " + preserve)
            if target["reference_plan"]:
                target["reference_plan"] = target["reference_plan"] | {"prompt": target["prompt"]}
        elif route == "trim":
            target.update(
                output_asset_id=r["source_id"],
                source_start=r["source_start"],
                playback_rate=r["playback_rate"],
            )
        elif route == "library":
            asset = db.get(Asset, r["asset_id"])
            target.pop("reference_plan", None)
            target.update(
                output_asset_id=asset.id,
                original_asset_id=asset.id,
                source_start=r["source_start"],
                original_kind="video" if asset.media["mime"].startswith("video/") else "photo",
                required_action=r["prompt"] if r["needs_operation"] else "",
            )
        by_index[s["index"]] = target
    brief["video_segments"] = [by_index[s["index"]] for s in old]
    brief["repair_routes"] = {str(i): r["route"] for i, r in repairs.items()}
    # Shared shots and all-library replacements still need their originals at assembly.
    for target in brief["video_segments"]:
        output = target.get("output_asset_id")
        if output and output not in sources:
            sources.append(output)
    if any(not s.get("output_asset_id") for s in by_index.values()):
        return "visual"
    if brief.get("audio_prompt") and not brief.get("soundtrack_asset_id"):
        return "audio"
    return "render"


def evidence_times(second, duration):
    return sorted({round(max(0, min(duration - 0.12, t)), 3) for t in (second - 1, second, second + 1)})


def evidence_frames(settings, asset, seconds):
    data = read_reference(settings, asset_record(asset))
    return video_frames(data, seconds)


def source_for_repair(db, job, repair):
    from .asset_edit import ancestry
    from .reference_grounding import source_cleared

    asset = db.get(Asset, repair["source_id"])
    if not asset or not asset.media["mime"].startswith("video/") or asset.media["duration"] > 30.1:
        raise ValueError("Repair requires the saved original clip, at most 30 seconds long.")
    parents, missing = ancestry(db, [asset])
    if missing or any(
        not source_cleared(a, job.brief["rights"] == "not_public", scene_rights=asset.rights) for a in parents
    ):
        raise ValueError("The failed clip or a source is no longer cleared for repair.")
    return asset


def repair_payload(db, settings, job, runway):
    """Build only motion/keyframe/edit tasks here. Normal submissions keep their existing checkpoints."""
    from types import SimpleNamespace

    from .real_edit import _picture
    from .reference_grounding import validate_record
    from .runway import payload_for
    from .scene_generation import validate_scene
    from .video_segments import segment_brief

    shot = current_segment(job) or {}
    repair = shot.get("repair") or {}
    if job.stage != "visual" or repair.get("route") not in {"motion", "aleph"}:
        return None
    plan = shot["reference_plan"]
    if not plan.get("references") and job.brief["operation"] == "video_edit":
        from .reference_grounding import save_plan, video_reference_frame

        source_id = shot.get("input_asset_id") or job.brief["reference_ids"][0]
        source = db.get(Asset, source_id)
        if not source or source.removed_at:
            raise ValueError("The reference footage for the edit is unavailable.")
        reference = video_reference_frame(db, settings, job, source)
        plan = plan | {
            "references": [asset_record(reference)],
            "system": shot.get("system") or job.brief["product"],
            "physical_description": "Company-supplied footage of this shot.",
            "visual_direction": job.brief.get("visual_direction", ""),
            "scene_prompt": repair["prompt"],
        }
        save_plan(job, plan)
        db.commit()
    private = job.brief["rights"] == "not_public"
    for record in plan["references"]:
        validate_record(db, settings, record, private=private)
    scene = plan.get("scene_reference")
    if scene:
        validate_scene(db, settings, scene)
    original = source_for_repair(db, job, repair)
    brief = segment_brief(job)
    if repair["route"] == "motion":
        endpoint, payload = payload_for(
            settings, brief, job.platform, [runway.reference(SimpleNamespace(**scene))]
        )
        return endpoint, payload, "visual"
    if frame := repair.get("keyframe"):
        from .scene_generation import builder_guidance, review_scene

        validate_scene(db, settings, frame)
        check = repair.get("keyframe_check")
        if not check:
            check_plan = plan | {
                "scene_prompt": repair["prompt"],
                "scene_retry": {"video_check": {"reasons": [repair["prompt"]]}},
                "keyframe_source": {"clip": asset_record(original), "second": repair["second"]},
            }
            check = review_scene(settings, check_plan, builder_guidance(db, job), frame).model_dump()
            save_repair(job, shot, repair | {"keyframe_check": check})
            db.commit()
        if not check["passed"]:
            raise ValueError(
                "Review the corrected keyframe before another edit. " + " ".join(check["reasons"])[:600]
            )
        endpoint, payload = payload_for(
            settings.model_copy(update={"runway_edit_model": "aleph2"}),
            brief | {"operation": "video_edit"},
            job.platform,
            [runway.reference(original)],
        )
        payload["keyframes"] = [
            {"seconds": repair["second"], "uri": runway.reference(SimpleNamespace(**frame))}
        ]
        return endpoint, payload, "repair_edit"
    frames = evidence_frames(settings, original, [repair["second"]])
    if not frames:
        raise ValueError("The flagged frame is missing from the saved clip.")
    ref = _picture(frames[0])["image_url"]
    prompt = (
        "Edit @ref0 at this exact camera position. Keep its framing, lighting and subjects. "
        + repair["prompt"]
    )
    endpoint, payload = payload_for(
        settings,
        brief | {"operation": "image", "generation_prompt": prompt[:950], "video_scene": True},
        job.platform,
        [ref],
    )
    return endpoint, payload, "repair_keyframe"


def save_repair(job, shot, repair):
    job.brief = job.brief | {
        "video_segments": [
            s | {"repair": repair} if s["index"] == shot["index"] else s for s in job.brief["video_segments"]
        ]
    }


def save_keyframe(db, settings, job, data):
    from .storage import ingest

    shot = current_segment(job)
    repair = shot["repair"]
    src = source_for_repair(db, job, repair)
    asset = ingest(
        db,
        settings,
        data,
        f"repair-{job.id}-{shot['index']}.png",
        source="ai_edited",
        generator="runway/" + settings.runway_image_model,
        prompt=repair["prompt"],
        rights=src.rights,
        owner=src.owner,
        rights_expire_at=src.rights_expire_at,
        demo_only=src.demo_only,
        tags={
            "generated_scene": True,
            "source_asset_ids": [src.id],
            "media_job_id": job.id,
            "runway_task_id": job.task_id,
            "product": shot.get("system") or job.brief["product"],
        },
    )
    if not asset.media["mime"].startswith("image/"):
        raise ValueError("The repair keyframe task returned an unexpected file type.")
    save_repair(job, shot, repair | {"keyframe": asset_record(asset)})
    job.task_history = [
        *job.task_history,
        {
            "stage": "repair_keyframe",
            "task_id": job.task_id,
            "model": settings.runway_image_model,
            "segment_index": shot["index"],
            "output_asset_id": asset.id,
        },
    ]
    job.state, job.task_id, job.submitted_at = "queued", "", None
    db.commit()
