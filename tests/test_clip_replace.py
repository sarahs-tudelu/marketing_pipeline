import asyncio
import os
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_idea_plans import IDEA, PRODUCTS, facts, frames, logo  # noqa: F401

from social.app import create_app
from social.clip_replace import FIXES, flagged, suggested_prompt
from social.lifecycle import transition
from social.media_studio import process_jobs
from social.models import Asset, CheckResult, MediaJob, Post, Transition
from social.storage import storage

REASON = (
    "At about 15–17 seconds, inside the generated storefront scene, garbled brand-like lettering appears on a "
    "neighboring storefront."
)


def run(db, settings, job):
    for _ in range(12):
        process_jobs(db, settings)
        db.refresh(job)
        if job.state == "complete":
            return
    raise AssertionError(job.error)


def held_video(db, settings, client, facts, logo):  # noqa: F811
    token = client.get("/api/session").json()["csrf"]
    created = client.post(
        "/forms/ideas/create",
        data={
            "csrf": token,
            "prompt": IDEA,
            "format": "auto",
            "kind": "auto",
            "platforms": ["instagram"],
            "rights": "public",
            "website": "on",
            "facts_id": facts.id,
            "campaign": "Introducing Tudelu",
            "operation": "video",
            "duration": "30",
            "plan_kind": "promotional",
            "shot_setting": ["backyard", "walkway", "storefront", "storefront at dusk"],
            "shot_system": PRODUCTS,
            "soundtrack": "soft ambient music in the morning",
            "end_card": "on",
            "logo_id": logo.id,
            "mode": "once",
        },
    )
    assert created.status_code == 200
    job = db.scalar(select(MediaJob))
    run(db, settings, job)
    post = db.get(Post, job.post_id)
    transition(db, post, "held", "Visual check failed")
    db.add(
        CheckResult(
            post_id=post.id,
            run_id="vision-run",
            name="vision_review",
            passed=False,
            details={
                "passed": False,
                "reasons": [REASON],
                "garbled_text": True,
                "misused_logos": True,
                "unexpected_people": False,
                "visible_claims": [],
            },
        )
    )
    db.commit()
    return token, job, post


def test_flagged_times_and_suggested_wording():
    segments = [
        {"index": i, "start": s, "duration": d} for i, (s, d) in enumerate([(0, 7), (7, 7), (14, 7), (21, 6)])
    ]
    assert flagged([REASON], segments) == {2}
    assert flagged(["Lettering at 7 s and around 22 seconds."], segments) == {1, 3}
    assert flagged(["The track is 30.0 s"], segments) == set()
    clip = {"prompt": "Tudelu Sunshades at a storefront. Plain, unmarked surfaces."}
    assert suggested_prompt(clip, {"garbled_text": True}).endswith(FIXES["text"])
    assert suggested_prompt(clip, {"unexpected_people": True}).endswith(FIXES["people"])
    assert suggested_prompt(clip, {"visible_claims": ["x"]}) == clip["prompt"]
    long = {"prompt": "word " * 400}
    fixed = suggested_prompt(long, {"misused_logos": True})
    assert fixed.endswith(FIXES["text"]) and len(fixed.encode("utf-16-le")) <= 2000


def test_held_video_regenerates_only_the_flagged_clip(db, settings, facts, logo, monkeypatch):  # noqa: F811
    def offline(*args, **kwargs):
        raise AssertionError("Fixture mode must not contact external services")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", offline)
    with TestClient(create_app(settings)) as client:
        token, job, post = held_video(db, settings, client, facts, logo)
        page = client.get(f"/posts/{post.id}").text
        assert (
            'id="fix-clip"' in page and "What the visual check found" in page and "garbled brand-like" in page
        )
        assert "<details open><summary><b>Clip 3</b> · 14–21 s · storefront · Sunshades" in page
        assert page.count("Flagged by the check") == 1 and FIXES["text"] in page
        assert "one new 7-second simulated clip" in page

        too_far = client.post(f"/forms/posts/{post.id}/clips/9/regenerate", data={"csrf": token})
        assert (
            too_far.status_code == 400 and "Choose one of the parts listed on the post page" in too_far.text
        )
        queued = client.post(f"/forms/posts/{post.id}/clips/2/regenerate", data={"csrf": token, "prompt": ""})
        assert queued.status_code == 200 and str(queued.url).endswith("/admin/media-queue")
        assert (
            "Regenerating clip 3 of a" in queued.text
            and "the music and the logo end card are reused" in queued.text
        )
        again = client.post(f"/forms/posts/{post.id}/clips/1/regenerate", data={"csrf": token})
        assert again.status_code == 200
        assert "Regenerating clip 3…" in client.get(f"/posts/{post.id}").text

    jobs = db.scalars(select(MediaJob).order_by(MediaJob.created_at)).all()
    assert len(jobs) == 2
    redo = jobs[1]
    old, new = job.brief["video_segments"], redo.brief["video_segments"]
    music = next(i for i in job.source_asset_ids if db.get(Asset, i).media["mime"].startswith("audio/"))
    assert redo.brief["replaces"] == {
        "post_id": post.id,
        "media_job_id": job.id,
        "part": "2",
        "label": "clip 3",
        "clip_index": 2,
        "requested_by": "local-developer",
    }
    assert redo.brief["soundtrack_asset_id"] == music and redo.brief["workflow"] == "immediate"
    assert old[2]["output_asset_id"] not in redo.source_asset_ids and music in redo.source_asset_ids
    assert "output_asset_id" not in new[2] and new[2]["replaced_asset_id"] == old[2]["output_asset_id"]
    assert new[2]["seed"] == (old[2]["seed"] + 1) % 2**32 and new[2]["prompt"].endswith(FIXES["text"])
    assert [new[i] for i in (0, 1, 3)] == [old[i] for i in (0, 1, 3)]

    run(db, settings, redo)
    # One new clip; the other three clips and the music are reused.
    assert [(h["stage"], h.get("segment_index")) for h in redo.task_history] == [("visual", 2)]
    clips = [s["output_asset_id"] for s in redo.brief["video_segments"]]
    assert clips[:2] + clips[3:] == [old[i]["output_asset_id"] for i in (0, 1, 3)]
    assert clips[2] != old[2]["output_asset_id"]
    result = db.get(Asset, redo.output_asset_id)
    assert result.media["duration"] == 30 and result.media["audio_codecs"] == ["aac"]
    assert result.tags["end_card"]["asset_id"] == logo.id and result.tags["media_job_id"] == redo.id
    assert result.tags["replaces"]["clip_index"] == 2 and music in result.tags["source_asset_ids"]
    assert all(f"Clip {n}: " in result.prompt for n in range(1, 5)) and FIXES["text"] in result.prompt
    last = frames(storage(settings).read(result.storage_key))[-1]
    assert last.getpixel((5, 5)) == (255, 255, 255)

    db.refresh(post)
    replacement = db.get(Post, redo.post_id)
    assert (
        replacement.id != post.id and replacement.status == "draft" and replacement.asset_ids == [result.id]
    )
    assert post.status == "failed"
    reason = db.scalars(select(Transition.reason).where(Transition.post_id == post.id)).all()[-1]
    assert reason == f"Replaced by post {replacement.id} after clip 3 was regenerated"
    with TestClient(create_app(settings)) as client:
        assert 'id="fix-clip"' not in client.get(f"/posts/{post.id}").text


def test_clip_regeneration_needs_a_held_multi_clip_post(db, settings, facts, logo):  # noqa: F811
    with TestClient(create_app(settings)) as client:
        token, job, post = held_video(db, settings, client, facts, logo)
        transition(db, post, "failed", "Rejected by internal user")
        db.commit()
        assert 'id="fix-clip"' not in client.get(f"/posts/{post.id}").text
        refused = client.post(f"/forms/posts/{post.id}/clips/0/regenerate", data={"csrf": token})
        assert (
            refused.status_code == 400
            and "Only a held post can have part of its media made again" in refused.text
        )
        long = client.post(
            f"/forms/posts/{post.id}/clips/0/regenerate", data={"csrf": token, "prompt": "x" * 1001}
        )
        assert long.status_code == 400
    assert len(db.scalars(select(MediaJob)).all()) == 1


def test_fix_one_clip_in_browser(db, settings, facts, logo, tmp_path):  # noqa: F811
    from test_studio_ui import serve_studio_request

    playwright = pytest.importorskip("playwright.async_api")
    capture = Path(os.getenv("UI_CAPTURE_DIR") or tmp_path)
    capture.mkdir(parents=True, exist_ok=True)

    async def exercise(client, post):
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(
                executable_path=os.environ["BROWSER_BINARY"], headless=True
            )
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            page.set_default_timeout(5000)
            errors, dialogs = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))

            async def accept(dialog):
                dialogs.append(dialog.message)
                await dialog.accept()

            page.on("dialog", accept)
            await page.route("**/*", lambda route: serve_studio_request(route, client))
            await page.goto(f"http://studio.test/posts/{post.id}")
            section = page.locator("#fix-clip")
            await playwright.expect(section.get_by_text("What the visual check found")).to_be_visible()
            flagged = section.locator("details[open]")
            await playwright.expect(flagged).to_have_count(1)
            await playwright.expect(flagged.locator("summary")).to_contain_text("Clip 3")
            await playwright.expect(flagged.get_by_label("Prompt for this clip")).to_have_value(
                re.compile(re.escape(FIXES["text"]) + "$")
            )
            await section.scroll_into_view_if_needed()
            await page.screenshot(path=str(capture / "fix-clip-desktop.png"), full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.screenshot(path=str(capture / "fix-clip-mobile.png"), full_page=True)
            await page.set_viewport_size({"width": 1440, "height": 1000})
            async with page.expect_navigation():
                await flagged.get_by_role("button", name="Regenerate clip 3").click()
            assert dialogs == ["Regenerate clip 3? This makes one new 7-second simulated clip."]
            queue = page.locator("#media-queue")
            await playwright.expect(queue).to_contain_text("Regenerating clip 3 of a held post")
            await playwright.expect(queue).to_contain_text("Video clips saved: 3 / 4")
            await page.screenshot(path=str(capture / "fix-clip-progress.png"), full_page=True)
            assert not errors
            await browser.close()

    with TestClient(create_app(settings), base_url="http://studio.test") as client:
        _, _, post = held_video(db, settings, client, facts, logo)
        asyncio.run(exercise(client, post))
    assert len(db.scalars(select(MediaJob)).all()) == 2
