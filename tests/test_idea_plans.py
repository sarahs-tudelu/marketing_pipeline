import io
import json
import os
import re
from pathlib import Path

import av
import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import SecretStr
from sqlalchemy import select
from test_runway_media import brief_for

from social.app import create_app
from social.facts import load_facts, save_facts
from social.idea_plan import IdeaPlan, Shot, finalize, plan_idea, read_signals, shot_seconds
from social.media_studio import process_jobs, queue_jobs
from social.models import Asset, MediaJob
from social.provider_errors import ProviderError
from social.renditions import logo_card
from social.runway import payload_for
from social.storage import ingest, storage

# The idea that produced a single image instead of a video.
IDEA = (
    "create a cinematic series of clips that show all the products that tudelu sells in different settings. "
    "backyard, walkway, storefront, etc. soft ambient music in the morning. 30 second reel with the tudelu "
    "logo at the end. introductory post for tudelu."
)
PRODUCTS = ["Pergolas", "Canopies", "Sunshades", "Carports"]


@pytest.fixture
def facts(db, settings):
    sheet = load_facts(db, settings.facts_path)
    saved = save_facts(
        db, dict(sheet.document) | {"version": "products", "products": [{"name": n} for n in PRODUCTS]}
    )
    db.commit()
    return saved


def png(color, size=(400, 100)):
    stream = io.BytesIO()
    image = Image.new("RGBA", size, (0, 0, 0, 0))
    image.paste(color, (20, 20, size[0] - 20, size[1] - 20))
    image.save(stream, "PNG")
    return stream.getvalue()


@pytest.fixture
def logo(db, settings):
    asset = ingest(
        db, settings, png((230, 110, 30, 255)), "tudelu-logo.png", rights="public", tags={"brand_logo": True}
    )
    db.commit()
    return asset


def frames(data):
    with av.open(io.BytesIO(data)) as container:
        return [f.to_image() for f in container.decode(video=0)]


def test_idea_signals_read_length_music_logo_settings_and_products():
    signals = read_signals(IDEA, PRODUCTS)
    assert signals["video"] and signals["seconds"] == 30
    assert signals["soundtrack"] == "soft ambient music in the morning"
    assert signals["end_card"] and signals["all_products"] and not signals["editorial"]
    assert signals["settings"] == ["backyard", "walkway", "storefront"]
    assert read_signals("A photo of a pergola at dusk")["video"] is False
    silent = read_signals("A 1-minute video of partition walls, no music")
    assert silent["seconds"] == 60 and silent["silent"] and not silent["soundtrack"]
    named = read_signals("Pergolas and canopies at dusk with gentle piano", PRODUCTS)
    assert named["systems"] == ["Pergolas", "Canopies"] and named["soundtrack"] == "gentle piano"


def test_reported_idea_plans_a_30_second_multi_product_video(db, settings, facts):
    plan = plan_idea(db, settings, idea=IDEA, facts_id=facts.id, has_logo=True, platforms=["instagram"])
    assert (plan.operation, plan.duration, plan.end_card) == ("video", 30, True)
    assert plan.soundtrack == "soft ambient music in the morning" and plan.kind == "promotional"
    assert [s.setting for s in plan.shots] == ["backyard", "walkway", "storefront", "backyard"]
    assert [s.system for s in plan.shots] == PRODUCTS
    assert shot_seconds(plan) == [7, 7, 7, 6]
    image = plan_idea(db, settings, idea=IDEA, requested_format="image", facts_id=facts.id)
    assert image.operation == "image" and not image.soundtrack and not image.end_card
    assert any("Images can't carry music" in n for n in image.notes)


def test_live_planner_cannot_turn_an_explicit_video_request_into_an_image(db, settings, facts, monkeypatch):
    seen = {}

    def claude(self, system, context):
        seen.update(context)
        return {
            "title": "Introducing Tudelu",
            "operation": "image",
            "duration": 12,
            "kind": "promotional",
            "shots": [
                {"setting": "backyard", "system": "Pergolas"},
                {"setting": "walkway", "system": "Gazebos"},
            ]
            + [{"setting": f"place {i}", "system": "Canopies"} for i in range(12)],
            "soundtrack": "",
            "end_card": False,
            "available_systems": ["Pergolas", "Canopies", "Treehouses"],
            "notes": ["Read as a product tour."],
        }

    monkeypatch.setattr("social.providers.Claude.call", claude)
    settings.ai_mode = "live"
    plan = plan_idea(db, settings, idea=IDEA, facts_id=facts.id, has_logo=True, snapshot={"text": "Walkways"})
    assert seen["explicit_signals"]["seconds"] == 30 and "Pergolas" in json.dumps(seen["catalog"])
    assert (plan.operation, plan.duration, plan.end_card) == ("video", 30, True)
    assert plan.soundtrack == "soft ambient music in the morning"
    # Unknown systems are cleared for the grounded prompt writer; 27 seconds of footage fits 9 clips.
    assert plan.shots[1] == Shot(setting="walkway") and len(plan.shots) == 9
    assert plan.available_systems == ["Pergolas", "Canopies"]
    assert any("Gazebos" in n for n in plan.notes) and any("first 9" in n for n in plan.notes)

    monkeypatch.setattr("social.providers.Claude.call", lambda *args: {"operation": "sculpture"})
    with pytest.raises(ProviderError, match="nothing was generated"):
        plan_idea(db, settings, idea=IDEA, facts_id=facts.id)


def test_explicit_format_and_style_choices_win():
    signals = read_signals(IDEA)
    plan = IdeaPlan(operation="video", duration=30, kind="promotional", end_card=True, soundtrack="Piano")
    result = finalize(plan, signals, IDEA, "video_edit", "non_promotional", True, "")
    assert result.operation == "video_edit" and result.kind == "non_promotional" and not result.shots


def test_music_uses_full_length_text_to_music(settings):
    brief = {
        "operation": "video",
        "duration": 30,
        "audio_prompt": "Soft ambient music",
        "generation_prompt": "",
        "seed": 1,
        "soundtrack_model": "music",
    }
    assert payload_for(settings, brief, "instagram", [], audio=True) == (
        "text_to_music",
        {
            "model": "eleven_music_v2_5",
            "promptText": "Soft ambient music",
            "duration": 30,
            "instrumental": True,
        },
    )
    _, sung = payload_for(
        settings, brief | {"audio_prompt": "Warm folk song with vocals"}, "instagram", [], audio=True
    )
    assert "instrumental" not in sung
    legacy = {k: v for k, v in brief.items() if k != "soundtrack_model"}
    assert payload_for(settings, legacy, "instagram", ["runway://a"], audio=True)[0] == "sound_effect"


def test_logo_card_keeps_logo_visible_on_a_contrasting_background():
    dark = frames(logo_card(png((230, 110, 30, 255)), 720, 1280, 3))
    assert len(dark) == 90 and dark[0].getpixel((360, 640))[0] > 240  # Fades in from the background.
    assert dark[-1].getpixel((5, 5)) == (255, 255, 255) and dark[-1].getpixel((360, 640))[2] < 80
    light = frames(logo_card(png((255, 255, 255, 255)), 720, 1280, 3))
    assert sum(light[-1].getpixel((5, 5))) < 60 and sum(light[-1].getpixel((360, 640))) > 700


def test_brief_rules_for_clips_and_end_cards(db, settings, logo):
    with pytest.raises(ValueError, match="requires a video"):
        brief_for(db, settings, end_card_asset_id=logo.id)
    with pytest.raises(ValueError, match="at least 5 seconds"):
        brief_for(db, settings, operation="video", duration=4, end_card_asset_id=logo.id)
    with pytest.raises(ValueError, match="at least 8 seconds of footage"):
        brief_for(
            db,
            settings,
            operation="video",
            duration=10,
            end_card_asset_id=logo.id,
            shots=[Shot(setting=str(i)) for i in range(4)],
        )
    private = ingest(db, settings, png((0, 0, 0, 255)), "logo-private.png", rights="not_public")
    with pytest.raises(ValueError, match="cannot close a Public video"):
        queue_jobs(
            db,
            settings,
            brief_for(db, settings, operation="video", duration=10, end_card_asset_id=private.id),
        )


def test_review_flow_creates_the_reported_idea_as_a_video_with_music_and_logo(
    db, settings, facts, logo, monkeypatch
):
    def offline(*args, **kwargs):
        raise AssertionError("Fixture mode must not contact external services")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", offline)
    with TestClient(create_app(settings)) as client:
        assert 'id="new-idea"' in client.get("/admin/create").text
        token = client.get("/api/session").json()["csrf"]
        idea = {
            "csrf": token,
            "prompt": IDEA,
            "format": "auto",
            "kind": "auto",
            "platforms": ["instagram"],
            "rights": "public",
            "website": "on",
            "facts_id": facts.id,
        }
        review = client.post("/forms/ideas/plan", data=idea)
        assert review.status_code == 200
        assert "A 30-second video: 4 clips, a 3-second logo end card and music, for Instagram." in review.text
        assert f'<option value="{logo.id}">tudelu-logo.png</option>' in review.text
        assert "Nothing is generated until you choose below" in review.text
        assert not db.scalars(select(MediaJob)).all()

        back = client.post("/forms/ideas/create", data=idea | {"mode": "back"})
        assert back.status_code == 200 and "soft ambient music in the morning" in back.text
        plan = {
            "campaign": "Introducing Tudelu",
            "operation": "video",
            "duration": "30",
            "plan_kind": "promotional",
            "shot_setting": ["backyard", "walkway", "storefront", "storefront at dusk", ""],
            "shot_system": PRODUCTS + [""],
            "soundtrack": "soft ambient music in the morning",
            "end_card": "on",
            "logo_id": logo.id,
        }
        missing = client.post("/forms/ideas/create", data=idea | plan | {"logo_id": "", "mode": "once"})
        assert missing.status_code == 400 and "Choose the logo" in missing.text
        assert 'value="storefront at dusk"' in missing.text
        created = client.post("/forms/ideas/create", data=idea | plan | {"mode": "once"})
        assert created.status_code == 200 and str(created.url).endswith("/admin/media-queue")
    job = db.scalar(select(MediaJob))
    assert job.brief["workflow"] == "immediate" and job.brief["soundtrack_model"] == "music"
    assert [s["duration"] for s in job.brief["video_segments"]] == [7, 7, 7, 6]
    assert job.brief["video_segments"][3]["shot"] == {"setting": "storefront at dusk", "system": "Carports"}
    for _ in range(12):
        process_jobs(db, settings)
        if job.state == "complete":
            break
    assert job.state == "complete", job.error
    assert [h["model"] for h in job.task_history] == ["gen4.5"] * 4 + ["eleven_music_v2_5"]
    assert [s["system"] for s in job.brief["video_segments"]] == PRODUCTS
    assert all(s["shot"]["setting"] in s["prompt"] for s in job.brief["video_segments"])
    result = db.get(Asset, job.output_asset_id)
    assert result.media["duration"] == 30 and (result.media["width"], result.media["height"]) == (720, 1280)
    assert result.media["audio_codecs"] == ["aac"] and result.tags["generated_audio"]
    assert result.tags["end_card"]["asset_id"] == logo.id and logo.id in result.tags["source_asset_ids"]
    last = frames(storage(settings).read(result.storage_key))[-1]
    assert last.getpixel((5, 5)) == (255, 255, 255) and last.getpixel((360, 640))[2] < 80
    with TestClient(create_app(settings)) as client:
        queue = client.get("/admin/media-queue").text
    assert "3 s · Tudelu logo end card" in queue and "Clip 4 · Carports" in queue


def test_live_writer_returns_one_grounded_prompt_per_clip(db, settings, facts, logo, monkeypatch):
    from social.prompting import write_prompt

    brief = brief_for(
        db,
        settings,
        facts_id=facts.id,
        prompt=IDEA,
        operation="video",
        duration=30,
        end_card_asset_id=logo.id,
        shots=[Shot(setting=s) for s in ("backyard", "walkway", "storefront")],
    )
    job = queue_jobs(db, settings, brief)[0]
    job.brief = job.brief | {"execution_mode": "live"}
    db.commit()
    settings.ai_mode = "live"
    replies = []

    def claude(self, system, context):
        replies.append(context)
        return reply

    monkeypatch.setattr("social.providers.Claude.call", claude)
    reply = {
        "system": "Pergolas",
        "prompt": "One",
        "shots": [
            {"system": name, "prompt": f"{name} clip"} for name in ("Pergolas", "Canopies", "Sunshades")
        ],
    }
    written = write_prompt(db, settings, job)
    assert replies[0]["storyboard"][0] == {"clip": 1, "seconds": 9, "setting": "backyard", "system": ""}
    assert replies[0]["end_card"] is True
    assert [s["prompt"] for s in written["video_segments"]] == [
        "Pergolas clip",
        "Canopies clip",
        "Sunshades clip",
    ]
    assert written["depicted_system"] == "Pergolas, Canopies, Sunshades"
    reply["shots"] = reply["shots"][:2]
    with pytest.raises(ProviderError, match="one prompt for each of the 3"):
        write_prompt(db, settings, job)
    reply["shots"] = [{"system": "Gazebos", "prompt": "x"}] * 3
    with pytest.raises(ValueError, match="'Gazebos', which is not named"):
        write_prompt(db, settings, job)


def test_start_image_is_used_for_the_first_planned_clip_only(db, settings, assets, monkeypatch):
    shots = [Shot(setting="backyard"), Shot(setting="walkway")]
    job = queue_jobs(
        db,
        settings,
        brief_for(db, settings, operation="video", duration=10, shots=shots, reference_ids=[assets[0].id]),
    )[0]
    job.brief = job.brief | {
        "execution_mode": "live",
        "prompt_writer": {},
        "video_segments": [
            s | {"prompt": f"clip {s['index']}", "seed": 1} for s in job.brief["video_segments"]
        ],
    }
    db.commit()
    settings.ai_mode, settings.runway_api_key = "live", SecretStr("mock")
    clip = storage(settings).read(db.get(Asset, assets[0].id).storage_key)
    submitted = []

    class FakeRunway:
        def reference(self, asset):
            return "runway://start"

        def submit(self, endpoint, payload):
            submitted.append((endpoint, payload))
            return f"task-{len(submitted)}"

        def task(self, task_id):
            return {"status": "SUCCEEDED", "output": ["https://output.test/clip"]}

        def download(self, url):
            from social.seed import seed_media

            return seed_media(duration=5)[1]

    for _ in range(4):
        process_jobs(db, settings, FakeRunway())
    assert [e for e, _ in submitted] == ["image_to_video", "text_to_video"], job.error
    assert submitted[0][1]["promptImage"] == "runway://start" and "promptImage" not in submitted[1][1]
    assert clip and re.fullmatch(r"task-\d", job.task_history[-1]["task_id"])


@pytest.mark.skipif(not os.getenv("BROWSER_BINARY"), reason="BROWSER_BINARY is not configured")
def test_idea_to_plan_to_progress_in_browser(db, settings, facts, logo, tmp_path):
    import asyncio

    from test_studio_ui import serve_studio_request

    playwright = pytest.importorskip("playwright.async_api")
    capture = Path(os.getenv("UI_CAPTURE_DIR") or tmp_path)
    capture.mkdir(parents=True, exist_ok=True)

    async def exercise(client):
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(
                executable_path=os.environ["BROWSER_BINARY"], headless=True
            )
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            page.set_default_timeout(5000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.route("**/*", lambda route: serve_studio_request(route, client))
            await page.goto("http://studio.test/admin/create")
            await playwright.expect(page.get_by_role("link", name="New post from an idea")).to_have_attribute(
                "aria-current", "page"
            )
            form = page.locator("#new-idea")
            await form.get_by_label("Your idea", exact=True).fill(IDEA)
            await playwright.expect(form.get_by_label("Let the idea decide (recommended)")).to_be_checked()
            await page.screenshot(path=str(capture / "idea-create-desktop.png"), full_page=True)
            async with page.expect_navigation():
                await form.get_by_role("button", name="Plan this idea").click()
            plan = page.locator("#idea-plan")
            await playwright.expect(plan.locator(".plan-summary")).to_have_text(
                "A 30-second video: 4 clips, a 3-second logo end card and music, for Instagram."
            )
            await playwright.expect(plan.get_by_label("Format")).to_have_value("video")
            await playwright.expect(plan.get_by_label("Length (seconds)")).to_have_value("30")
            await playwright.expect(plan.get_by_label("Music")).to_have_value(
                "soft ambient music in the morning"
            )
            await playwright.expect(plan.get_by_label("End with the Tudelu logo (3 seconds)")).to_be_checked()
            await playwright.expect(plan.locator('input[name="shot_setting"]')).to_have_count(5)
            await page.screenshot(path=str(capture / "idea-plan-desktop.png"), full_page=True)
            await plan.get_by_label("Format").select_option("image")
            await playwright.expect(plan.get_by_label("Length (seconds)")).to_be_hidden()
            await playwright.expect(plan.get_by_label("Music")).to_be_hidden()
            await plan.get_by_label("Format").select_option("video")
            await playwright.expect(plan.get_by_label("Music")).to_be_visible()
            await page.set_viewport_size({"width": 390, "height": 844})
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            await page.screenshot(path=str(capture / "idea-plan-mobile.png"), full_page=True)
            await page.set_viewport_size({"width": 1440, "height": 1000})
            async with page.expect_navigation():
                await plan.get_by_role("button", name="Create now").click()
            queue = page.locator("#media-queue")
            await playwright.expect(queue).to_contain_text("Video clips saved: 0 / 4")
            await playwright.expect(queue).to_contain_text("3 s · Tudelu logo end card")
            await page.screenshot(path=str(capture / "idea-progress-desktop.png"), full_page=True)
            await page.goto("http://studio.test/")
            await page.screenshot(path=str(capture / "overview-desktop.png"), full_page=True)
            assert not errors
            await browser.close()

    with TestClient(create_app(settings), base_url="http://studio.test") as client:
        asyncio.run(exercise(client))
    assert db.scalar(select(MediaJob)).brief["end_card"]["asset_id"] == logo.id
