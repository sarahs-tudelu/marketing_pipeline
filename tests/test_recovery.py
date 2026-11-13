import json

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, TypeAdapter, ValidationError
from sqlalchemy import select
from test_clip_replace import REASON, held_video, run
from test_idea_plans import IDEA, PRODUCTS, facts, frames, logo  # noqa: F401
from test_runway_media import brief_for, written_prompt

from social.app import create_app
from social.lifecycle import transition
from social.media_studio import process_jobs, queue_jobs
from social.models import Asset, CheckResult, MediaJob, Post, Transition
from social.pipeline import draft
from social.provider_errors import ProviderError
from social.providers import Providers
from social.recovery import caption_error, held_reason, retry_option, task_failed
from social.storage import storage
from social.worker import advance_queue

QUANTITY = "Caption contains a quantity absent from its cited facts"


def reasons(db, post):
    return db.scalars(
        select(Transition.reason).where(Transition.post_id == post.id).order_by(Transition.id)
    ).all()


def test_caption_errors_read_as_sentences(db, settings, assets):
    assert (
        caption_error(ValueError(QUANTITY))
        == "The writer used a number that isn't in the approved facts it cited."
    )
    assert (
        caption_error(ProviderError("Claude timed out.", provider="Claude", category="timeout"))
        == "Claude timed out."
    )
    with pytest.raises(ValidationError) as invalid:
        TypeAdapter(list[dict]).validate_python("nope")
    assert caption_error(invalid.value) == "The writer's answer wasn't in the expected format."
    assert caption_error(KeyError("x")) == "An unexpected error stopped it (KeyError)."
    # Posts held before readable reasons were saved still get an explanation.
    post = draft(db, "instagram", "Older post", [assets[0].id], load_facts_id(db, settings))
    transition(db, post, "held", "Caption generation rejected: ValueError")
    db.commit()
    held = held_reason(db, post)
    assert "the exact reason wasn't saved" in held["caption_error"] and held["fixes"] == {"caption"}


def load_facts_id(db, settings):
    from social.facts import load_facts

    return load_facts(db, settings.facts_path).id


def caption_fails_once(monkeypatch):
    real = Providers.caption
    calls = []

    def caption(self, context):
        calls.append(context["platform"])
        if len(calls) == 1:
            raise ValueError(QUANTITY)
        return real(self, context)

    monkeypatch.setattr(Providers, "caption", caption)
    return calls


def video_without_caption(db, settings, client, facts, logo):  # noqa: F811
    token = client.get("/api/session").json()["csrf"]
    client.post(
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
            "soundtrack": "soft ambient music",
            "end_card": "on",
            "logo_id": logo.id,
            "mode": "once",
        },
    )
    job = db.scalar(select(MediaJob))
    for _ in range(12):
        process_jobs(db, settings)
        advance_queue(db, settings)
    return token, db.get(Post, job.post_id)


def test_failed_caption_is_explained_and_can_be_written_again(db, settings, facts, logo, monkeypatch):  # noqa: F811
    calls = caption_fails_once(monkeypatch)
    with TestClient(create_app(settings)) as client:
        token, post = video_without_caption(db, settings, client, facts, logo)
        assert post.status == "held" and post.caption == ""
        assert reasons(db, post)[-1] == (
            "Caption generation rejected: The writer used a number that isn't in the approved facts it cited."
        )
        page = client.get(f"/posts/{post.id}").text
        assert 'id="post-held"' in page and "The caption couldn't be written." in page
        assert "a number that isn&#39;t in the approved facts it cited" in page
        assert "Write the caption again" in page and "Recheck &amp; approve" not in page
        # The video's parts can still be regenerated while the caption is missing.
        assert 'id="fix-clip"' in page and "Regenerate clip 3" in page and "Regenerate the music" in page
        refused = client.post(f"/forms/posts/{post.id}/approve", data={"csrf": token})
        assert refused.status_code == 400 and "This post has no caption yet" in refused.text
        written = client.post(f"/forms/posts/{post.id}/rewrite", data={"csrf": token})
        assert written.status_code == 200 and str(written.url).endswith(f"/posts/{post.id}")
        assert 'id="post-held"' not in written.text
    db.refresh(post)
    assert post.caption and post.status == "approved" and calls == ["instagram", "instagram"]
    assert reasons(db, post)[-4:] == [
        "New caption requested",
        "Caption provider: fixture",
        "Automated checks completed",
        "All checks passed",
    ]


def test_failed_checks_are_listed_with_their_fix(db, settings, facts, logo):  # noqa: F811
    with TestClient(create_app(settings)) as client:
        token, job, post = held_video(db, settings, client, facts, logo)
        transition(db, post, "generated", "Requested automated recheck")
        details = {
            "vision_review": (False, {"passed": False, "reasons": [REASON], "garbled_text": True}),
            "duplicates": (False, {"maximum": 0.973, "cross_platform_identical": False}),
            "audio_review": (
                False,
                {"error_type": "ProviderError", "reason": "OpenAI reported a service error."},
            ),
            "grounding": (True, {}),
        }
        for name, (passed, detail) in details.items():
            db.add(CheckResult(post_id=post.id, run_id="run-2", name=name, passed=passed, details=detail))
        transition(db, post, "held", "vision_review, duplicates, audio_review")
        db.commit()
        page = client.get(f"/posts/{post.id}").text
    assert "Why this post is held" in page and "The visual check found a problem in the picture." in page
    assert "garbled brand-like lettering" in page and "Regenerate the part with the problem below." in page
    assert (
        "The caption is too similar to another recent post." in page
        and "97% similar to a recent post." in page
    )
    assert "The check couldn&#39;t run: OpenAI reported a service error." in page  # rendered from a variable
    assert "Correct the cause, then recheck the post." in page
    assert 'href="#fix-clip">Regenerate part of the video</a>' in page and "Write the caption again" in page
    held = held_reason(db, post)
    assert [p["kind"] for p in held["problems"]] == ["visual", "caption", "recheck"]


def test_music_is_regenerated_alone(db, settings, facts, logo):  # noqa: F811
    with TestClient(create_app(settings)) as client:
        token, job, post = held_video(db, settings, client, facts, logo)
        page = client.get(f"/posts/{post.id}").text
        assert "<b>Music</b> · 30 s" in page
        assert "Regenerate the music? This makes a new 30-second simulated music track." in page
        queued = client.post(
            f"/forms/posts/{post.id}/parts/music/regenerate",
            data={"csrf": token, "prompt": "  Bright acoustic guitar "},
        )
        assert queued.status_code == 200 and str(queued.url).endswith("/admin/media-queue")
        assert (
            "Regenerating the music of a" in queued.text
            and "The clips and the logo end card are reused." in queued.text
        )
    redo = db.scalars(select(MediaJob).order_by(MediaJob.created_at)).all()[1]
    old_music = next(i for i in job.source_asset_ids if db.get(Asset, i).media["mime"].startswith("audio/"))
    clips = [s["output_asset_id"] for s in job.brief["video_segments"]]
    assert redo.stage == "audio" and redo.brief["audio_prompt"] == "Bright acoustic guitar"
    assert redo.source_asset_ids == clips and redo.brief["replaces"]["label"] == "the music"
    run(db, settings, redo)
    assert [h["stage"] for h in redo.task_history] == ["audio"]
    result = db.get(Asset, redo.output_asset_id)
    music = [i for i in redo.source_asset_ids if db.get(Asset, i).media["mime"].startswith("audio/")]
    assert len(music) == 1 and music[0] != old_music and result.media["audio_codecs"] == ["aac"]
    assert [s["output_asset_id"] for s in redo.brief["video_segments"]] == clips and result.media[
        "duration"
    ] == 30
    last = frames(storage(settings).read(result.storage_key))[-1]
    assert last.getpixel((5, 5)) == (255, 255, 255)
    db.refresh(post)
    assert post.status == "failed" and reasons(db, post)[-1].endswith("after the music was regenerated")


def test_failed_runway_task_can_be_generated_again(db, settings, assets, monkeypatch):
    monkeypatch.setattr("social.media_studio.write_prompt", written_prompt)
    monkeypatch.setattr(
        "social.providers.OpenAI.call",
        lambda *args, **kwargs: {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                {"summary": "A green square.", "visible_text": [], "limitations": []}
                            ),
                        }
                    ],
                }
            ],
        },
    )
    job = queue_jobs(db, settings, brief_for(db, settings, reference_ids=[assets[0].id]))[0]
    job.brief = job.brief | {"execution_mode": "live"}
    db.commit()
    settings.ai_mode, settings.runway_api_key = "live", SecretStr("mock")
    original = storage(settings).read(assets[0].storage_key)

    class FakeRunway:
        prompts = []

        def reference(self, asset):
            return "runway://ref"

        def submit(self, endpoint, payload):
            self.prompts.append((payload["promptText"], payload["seed"]))
            return f"task-{len(self.prompts)}"

        def task(self, task_id):
            if task_id == "task-1":
                return {"status": "FAILED", "failureCode": "SAFETY.INPUT.TEXT"}
            return {"status": "SUCCEEDED", "output": ["https://output.test/result"]}

        def download(self, url):
            return original

    provider = FakeRunway()
    process_jobs(db, settings, provider)
    process_jobs(db, settings, provider)
    assert job.state == "failed" and job.task_id == "task-1" and task_failed(job)
    assert job.error == (
        "Runway couldn't make the image. Runway's content moderation declined it. Change the prompt below, then "
        "generate it again. Runway code: SAFETY.INPUT.TEXT."
    )
    assert retry_option(job)["label"] == "the image"
    seed = job.brief["seed"]
    with TestClient(create_app(settings)) as client:
        token = client.get("/api/session").json()["csrf"]
        page = client.get("/admin/media-queue").text
        assert "Generate the image again" in page and "Resume saved task" not in page
        assert "This starts one new paid Runway task." in page
        resumed = client.post(f"/forms/media-jobs/{job.id}/resume", data={"csrf": token})
        assert resumed.status_code == 400 and "Use Generate again instead" in resumed.text
        retried = client.post(
            f"/forms/media-jobs/{job.id}/resubmit",
            data={"csrf": token, "prompt": "A pergola beside a plain white wall"},
        )
        assert retried.status_code == 200 and str(retried.url).endswith("/admin/media-queue")
    db.refresh(job)
    assert job.state == "queued" and job.task_id == "" and job.error == ""
    assert job.brief["generation_prompt"] == "A pergola beside a plain white wall"
    assert job.brief["seed"] == (seed + 1) % 2**32 and job.brief["failed_tasks"][0]["task_id"] == "task-1"
    for _ in range(4):
        process_jobs(db, settings, provider)
    assert job.state == "complete", job.error
    assert provider.prompts == [
        ("A pergola at dusk", seed),
        ("A pergola beside a plain white wall", (seed + 1) % 2**32),
    ]
    assert retry_option(job) is None


def test_recovery_panels_in_browser(db, settings, facts, logo, monkeypatch, tmp_path):  # noqa: F811
    import asyncio
    import os
    from pathlib import Path

    from test_studio_ui import serve_studio_request

    from social.recovery import runway_failure

    playwright = pytest.importorskip("playwright.async_api")
    capture = Path(os.getenv("UI_CAPTURE_DIR") or tmp_path)
    capture.mkdir(parents=True, exist_ok=True)
    caption_fails_once(monkeypatch)

    async def exercise(client, post, job):
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(
                executable_path=os.environ["BROWSER_BINARY"], headless=True
            )
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            page.set_default_timeout(5000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("dialog", lambda dialog: asyncio.ensure_future(dialog.accept()))
            await page.route("**/*", lambda route: serve_studio_request(route, client))
            await page.goto(f"http://studio.test/posts/{post.id}")
            held = page.locator("#post-held")
            await playwright.expect(held).to_contain_text("The caption couldn't be written.")
            await playwright.expect(page.get_by_role("button", name="Recheck & approve")).to_have_count(0)
            await page.screenshot(path=str(capture / "held-caption-desktop.png"), full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.screenshot(path=str(capture / "held-caption-mobile.png"), full_page=True)
            await page.set_viewport_size({"width": 1440, "height": 1000})
            async with page.expect_navigation():
                await held.get_by_role("button", name="Write the caption again").click()
            await playwright.expect(page.locator("#post-held")).to_have_count(0)
            await playwright.expect(page.locator(".status.approved")).to_be_visible()
            await page.goto("http://studio.test/admin/media-queue")
            card = page.locator(f'[data-job-id="{job.id}"]')
            await playwright.expect(card).to_contain_text("Runway's content moderation declined it.")
            await playwright.expect(
                card.get_by_role("button", name="Generate the image again")
            ).to_be_visible()
            await card.scroll_into_view_if_needed()
            await page.screenshot(path=str(capture / "generate-again-desktop.png"), full_page=True)
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.screenshot(path=str(capture / "generate-again-mobile.png"), full_page=True)
            assert not errors
            await browser.close()

    with TestClient(create_app(settings), base_url="http://studio.test") as client:
        _, post = video_without_caption(db, settings, client, facts, logo)
        job = queue_jobs(db, settings, brief_for(db, settings, platforms=["facebook"]))[0]
        job.brief = job.brief | {
            "generation_prompt": "A pergola at dusk beside a plain white wall",
            "prompt_writer": {},
        }
        job.state, job.task_id = "failed", "task-1"
        job.error = runway_failure(job, {"status": "FAILED", "failureCode": "SAFETY.INPUT.TEXT"})
        db.commit()
        asyncio.run(exercise(client, post, job))


def test_single_image_is_regenerated(db, settings):
    job = queue_jobs(db, settings, brief_for(db, settings))[0]
    db.commit()
    run(db, settings, job)
    post = db.get(Post, job.post_id)
    transition(db, post, "held", "Visual check failed")
    db.add(
        CheckResult(
            post_id=post.id,
            run_id="v",
            name="vision_review",
            passed=False,
            details={"passed": False, "reasons": ["Garbled text on the wall."], "garbled_text": True},
        )
    )
    db.commit()
    with TestClient(create_app(settings)) as client:
        token = client.get("/api/session").json()["csrf"]
        page = client.get(f"/posts/{post.id}").text
        assert (
            "Regenerate part of the image" in page
            and "Regenerate the image? This makes a new simulated image." in page
        )
        assert "Regenerate the music" not in page
        queued = client.post(f"/forms/posts/{post.id}/parts/visual/regenerate", data={"csrf": token})
        assert "Regenerating the image of a" in queued.text
    redo = db.scalars(select(MediaJob).order_by(MediaJob.created_at)).all()[1]
    assert redo.source_asset_ids == [] and redo.brief["seed"] == (job.brief["seed"] + 1) % 2**32
    assert redo.brief["generation_prompt"].endswith("greenery.")
    run(db, settings, redo)
    assert [h["stage"] for h in redo.task_history] == ["visual"]
    replacement = db.get(Post, redo.post_id)
    db.refresh(post)
    assert replacement.asset_ids == [redo.output_asset_id] and replacement.status == "draft"
    assert post.status == "failed" and reasons(db, post)[-1].endswith("after the image was regenerated")
