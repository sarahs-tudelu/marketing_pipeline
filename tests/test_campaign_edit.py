import asyncio
import os
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from social.adapters import ADAPTERS
from social.app import create_app
from social.db import now
from social.facts import save_facts
from social.media_studio import CreativeBrief, replenish_campaigns
from social.models import MediaCampaign, MediaJob


@pytest.fixture(name="campaign")
def campaign_fixture(db):
    sheet = save_facts(db, {"version": "campaign-edit", "brand_voice": "Simple", "hashtags": {},
                            "facts": [{"id": "name", "product": "company", "text": "Test brand."}]})
    brief = CreativeBrief(campaign="Weekly projects", prompt="Show a pergola", rights="not_public",
                          facts_id=sheet.id, platforms=["instagram"], interval_hours=48, website=False)
    saved = MediaCampaign(name=brief.campaign, brief=brief.model_dump() | {"prompted_by": "original-admin"})
    db.add(saved)
    db.commit()
    return saved


def test_edit_changes_next_batch_preserves_history_and_rejects_stale_forms(db, settings, campaign, monkeypatch):
    campaign.enabled = True
    campaign.sequence = 1
    last_run = now() - timedelta(hours=3)
    campaign.next_run_at = last_run + timedelta(hours=48)
    old = MediaJob(batch_key="first", platform="instagram", campaign_id=campaign.id, state="queued",
                   created_at=last_run, brief=campaign.brief)
    db.add(old)
    db.commit()
    edit = f"/admin/media-campaigns/{campaign.id}/edit"
    save = f"/forms/media-campaigns/{campaign.id}/save"
    with TestClient(create_app(settings)) as client:
        assert edit in client.get("/admin/media-studio").text
        html = client.get(edit).text
        assert 'value="Weekly projects"' in html and 'name="website" >' in html
        data = {"csrf": client.get("/api/session").json()["csrf"],
                "revision": re.search(r'name="revision" value="([^"]+)"', html)[1],
                "campaign": "Pergolas in motion", "prompt": "Show a courtyard at dusk", "platforms": "facebook",
                "rights": "not_public", "interval_hours": "0.5", "facts_id": campaign.brief["facts_id"]}
        assert client.post(save, data=data | {"csrf": ""}).status_code == 403
        invalid = client.post(save, data=data | {"rights": "public"})
        assert invalid.status_code == 400 and "Public campaigns require" in invalid.text
        assert save in invalid.text and "Show a courtyard at dusk" in invalid.text
        assert client.post(save, data=data).status_code == 200
        stale = client.post(save, data=data | {"prompt": "Overwrite from another tab"})
        assert stale.status_code == 409 and "Reload saved settings" in stale.text
    db.refresh(campaign)
    db.refresh(old)
    assert campaign.name == campaign.brief["campaign"] == "Pergolas in motion"
    assert campaign.brief["prompt"] == data["prompt"] and campaign.brief["prompted_by"] == "original-admin"
    assert campaign.enabled and campaign.sequence == 1 and campaign.next_run_at == last_run + timedelta(minutes=30)
    assert old.platform == "instagram" and old.brief["prompt"] == "Show a pergola"
    assert db.scalars(select(MediaJob)).all() == [old]

    monkeypatch.setitem(ADAPTERS, "facebook", lambda *args: SimpleNamespace(dry=False))
    settings.ai_mode, settings.live_platforms, settings.runway_api_key = "live", "facebook", SecretStr("test")
    replenish_campaigns(db, settings)
    assert campaign.sequence == 1 and "previous batch" in campaign.error
    old.state = "cancelled"
    db.commit()
    replenish_campaigns(db, settings)
    new = db.scalar(select(MediaJob).where(MediaJob.id != old.id))
    assert campaign.sequence == 2 and new.platform == "facebook" and new.campaign_id == campaign.id
    assert new.brief["prompt"] == data["prompt"] and new.brief["prompted_by"] == "original-admin"


def test_prompt_edit_preserves_existing_rights_and_next_run(db, settings, campaign):
    campaign.brief = campaign.brief | {"rights": "Company-owned", "duration": "3"}
    last_run = now() - timedelta(days=1)
    due = now() + timedelta(minutes=10)
    campaign.next_run_at = due
    db.add(MediaJob(batch_key="previous", campaign_id=campaign.id, platform="instagram",
                    created_at=last_run, state="complete", brief=campaign.brief))
    db.commit()
    edit = f"/admin/media-campaigns/{campaign.id}/edit"
    save = f"/forms/media-campaigns/{campaign.id}/save"
    with TestClient(create_app(settings)) as client:
        page = client.get(edit)
        assert page.status_code == 200 and 'value="Company-owned" selected' in page.text
        form = {"csrf": client.get("/api/session").json()["csrf"],
                "revision": re.search(r'name="revision" value="([^"]+)"', page.text)[1],
                "campaign": campaign.name, "prompt": "Feature a different project", "platforms": "instagram",
                "interval_hours": "48", "duration": "3", "facts_id": campaign.brief["facts_id"]}
        assert client.post(save, data=form | {"rights": "Other rights"}).status_code == 400
        assert client.post(save, data=form | {"rights": "Company-owned"}, follow_redirects=False).status_code == 303
    db.refresh(campaign)
    assert campaign.next_run_at == due and campaign.brief["rights"] == "Company-owned"
    assert campaign.brief["prompt"] == form["prompt"] and campaign.brief["duration"] == 3


@pytest.mark.skipif(not os.getenv("BROWSER_BINARY"), reason="BROWSER_BINARY is not configured")
def test_edit_prefills_selections_and_saves_paused_campaign_in_browser(db, settings, campaign, assets):
    from test_studio_ui import serve_studio_request

    playwright = pytest.importorskip("playwright.async_api")
    campaign.brief = campaign.brief | {"reference_ids": [assets[0].id], "owner": "Studio", "rights": "public"}
    due = campaign.next_run_at
    db.commit()

    async def exercise(client):
        async with playwright.async_playwright() as driver:
            browser = await driver.chromium.launch(executable_path=os.environ["BROWSER_BINARY"], headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.route("**/*", lambda route: serve_studio_request(route, client))
            await page.goto("http://studio.test/admin/media-studio")
            await page.get_by_role("link", name="Edit campaign").click()
            await playwright.expect(page.get_by_label("Campaign name")).to_have_value(campaign.name)
            await playwright.expect(page.locator(f'input[value="{assets[0].id}"]')).to_be_checked()
            await playwright.expect(page.get_by_label("Use website text")).not_to_be_checked()
            await playwright.expect(page.get_by_label("Usage rights")).to_have_value("public")
            await page.get_by_label("Usage rights").select_option("not_public")
            interval = page.get_by_label("Recurring interval")
            await playwright.expect(interval).to_have_attribute("step", "any")
            await interval.fill("0.25")
            await page.get_by_label("Your prompt").fill("Explore the courtyard")
            await page.get_by_role("button", name="Save changes").click()
            card = page.locator(f"#campaign-{campaign.id}")
            await playwright.expect(card).to_contain_text("Explore the courtyard")
            await playwright.expect(card).to_contain_text("every 0.25 hours")
            await playwright.expect(card.get_by_role("button", name="Enable campaign")).to_be_visible()
            await browser.close()

    with TestClient(create_app(settings), base_url="http://studio.test") as client:
        asyncio.run(exercise(client))
    db.refresh(campaign)
    assert not campaign.enabled and campaign.sequence == 0 and campaign.next_run_at == due
    assert campaign.brief["reference_ids"] == [assets[0].id] and campaign.brief["owner"] == "Studio"
    assert db.scalar(select(MediaJob)) is None
