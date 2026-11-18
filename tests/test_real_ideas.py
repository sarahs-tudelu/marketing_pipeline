"""Ideas about the company's own work find real library media and edit it, with no AI generation."""

import json
from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_idea_plans import facts, logo  # noqa: F401
from test_real_edit import footage, jpeg

from social import media_search
from social.app import create_app
from social.media_search import search
from social.media_studio import process_jobs
from social.models import Asset, MediaJob, Post
from social.providers import OpenAI
from social.real_edit import sample_times
from social.storage import ingest

IDEA = "Installation process of our partition walls"


class FormFields(HTMLParser):
    """The values a browser would submit from one form: hidden and text inputs, ticked boxes, selections."""

    def __init__(self, form_id):
        super().__init__()
        self.form_id, self.inside, self.fields, self.select, self.first, self.chosen = form_id, False, [], None, None, False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.inside = a.get("id") == self.form_id
        elif not self.inside:
            return
        elif tag == "input" and a.get("name") and a.get("type", "text") not in {"submit", "button", "file"}:
            if a.get("type") in {"checkbox", "radio"} and "checked" not in a:
                return
            self.fields.append((a["name"], a.get("value", "")))
        elif tag == "select":
            self.select, self.first, self.chosen = a.get("name"), None, False
        elif tag == "option" and self.select:
            self.first = a.get("value", "") if self.first is None else self.first
            if "selected" in a:
                self.fields.append((self.select, a.get("value", "")))
                self.chosen = True

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False
        elif tag == "select" and self.select:
            if not self.chosen and self.first is not None:
                self.fields.append((self.select, self.first))
            self.select = None

    def values(self):
        result = {}
        for name, value in self.fields:
            result.setdefault(name, []).append(value)
        return result


def plan_form(html):
    parser = FormFields("idea-plan")
    parser.feed(html)
    return parser.values()


def library(db, settings):
    install = ingest(db, settings, footage(), "partition-wall-install.mp4", rights="public",
                     tags={"product": "Retractable Partition Walls"})
    finished = ingest(db, settings, jpeg((180, 160, 140)), "partition-wall-finished.jpg", rights="public",
                      tags={"product": "Retractable Partition Walls"})
    sunset = ingest(db, settings, jpeg((200, 120, 60)), "pergola-sunset.jpg", rights="public")
    concept = ingest(db, settings, jpeg((90, 90, 90)), "partition-wall-install-concept.jpg", source="ai_generated",
                     generator="runway/gen4_image", prompt="Partition wall install", rights="public")
    db.commit()
    return install, finished, sunset, concept


def plan(client, facts, **fields):  # noqa: F811
    data = {
        "csrf": client.get("/api/session").json()["csrf"], "prompt": IDEA, "format": "auto", "kind": "auto",
        "platforms": ["instagram"], "rights": "public", "facts_id": facts.id,
    } | fields
    return client.post("/forms/ideas/plan", data=data)


def test_the_search_finds_real_media_by_what_the_idea_describes(db, settings):
    install, finished, sunset, concept = library(db, settings)
    matches, problem = search(db, settings, "installation process of partitionwalls")
    # AI media is never a match, however well its name fits; unrelated real media isn't either.
    assert [a.id for a, _ in matches] == [install.id, finished.id] and not problem
    matches, problem = search(db, settings, "a rooftop bar at night")
    assert matches == [] and "mentions what the idea asks for" in problem


def test_the_live_search_reads_descriptions_and_keeps_only_library_files(db, settings, monkeypatch):
    install, finished, sunset, _ = library(db, settings)
    settings.ai_mode = "live"
    described = []

    def describe(db, settings, asset):
        described.append(asset.id)
        asset.media = asset.media | {"visual_summary": {
            "schema_version": 1, "asset_checksum": asset.checksum, "simulated": False, "visible_text": [],
            "summary": "Two installers lift a glass partition panel into a ceiling track.",
        }}

    monkeypatch.setattr("social.visual_context.ensure_visual_summary", describe)
    monkeypatch.setattr(media_search, "DESCRIBE_NOW", 2)
    sent = {}

    def call(self, endpoint, payload):
        sent.update(payload)
        answer = {"matches": [{"asset_id": "not-in-library", "why": "x"},
                              {"asset_id": install.id, "why": "Crew fitting panels into the track"}], "problem": ""}
        return {"status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(answer)}]}]}

    monkeypatch.setattr(OpenAI, "call", call)
    matches, problem = search(db, settings, IDEA)
    assert [(a.id, why) for a, why in matches] == [(install.id, "Crew fitting panels into the track")]
    # The two newest undescribed files were described first, and their descriptions were sent.
    assert len(described) == 2 and "ceiling track" in sent["input"][0]["content"][0]["text"]
    assert sent["text"]["format"]["strict"] is True


def test_an_installation_idea_becomes_a_real_post_from_library_footage(db, settings, facts, logo):  # noqa: F811
    install, finished, sunset, _ = library(db, settings)
    with TestClient(create_app(settings)) as client:
        response = plan(client, facts)
        assert response.status_code == 200, response.text
        page = response.text
        assert "Adjust anything before editing" in page and "partition-wall-install.mp4" in page
        assert "Nothing is sent to Runway" in page and "Clips and photos, in viewing order" in page
        # Files the search didn't match can still be added from the library list; AI media can't.
        assert "Add media from the library" in page and "pergola-sunset.jpg" in page
        assert "partition-wall-install-concept.jpg" not in page
        form = plan_form(page)
        assert form["operation"] == ["real"] and form["real_format"] == ["video"]
        assert form["real_media"] == [install.id, finished.id]
        response = client.post("/forms/ideas/create", data=form | {"mode": ["once"]})
        assert response.status_code == 200
    job = db.scalar(select(MediaJob))
    # The edit planned on the plan page is used, since the media, format and length weren't changed.
    assert job.brief["operation"] == "real" and job.brief["prepared_edit"]["items"]
    for _ in range(3):
        process_jobs(db, settings)
    assert job.state == "complete", job.error
    assert [i["asset_id"] for i in job.brief["real_edit"]["items"]] == [install.id, finished.id]
    result = db.get(Asset, job.output_asset_id)
    assert result.source == "company" and result.tags["real_project"] and db.get(Post, job.post_id)


def test_changing_the_chosen_media_plans_the_edit_again(db, settings, facts, logo):  # noqa: F811
    install, finished, sunset, _ = library(db, settings)
    with TestClient(create_app(settings)) as client:
        form = plan_form(plan(client, facts).text)
        form["real_media"] = [install.id]
        assert client.post("/forms/ideas/create", data=form | {"mode": ["once"]}).status_code == 200
    job = db.scalar(select(MediaJob))
    assert job.brief["reference_ids"] == [install.id] and job.brief["prepared_edit"] == {}


@pytest.mark.parametrize("items,kept", [
    ([{"asset_id": "INSTALL", "end": 3, "brightness": 99}], True),
    ([{"asset_id": "INSTALL", "end": 3}, "not an item"], False),
])
def test_an_edit_sent_back_from_the_plan_page_is_checked_before_it_is_used(db, settings, facts, logo, items, kept):  # noqa: F811
    install, finished, sunset, _ = library(db, settings)
    with TestClient(create_app(settings)) as client:
        form = plan_form(plan(client, facts).text)
        edit = json.loads(form["real_plan"][0])
        edit["items"] = [i | {"asset_id": install.id} if isinstance(i, dict) else i for i in items]
        form |= {"real_plan": [json.dumps(edit)], "real_media": [install.id]}
        assert client.post("/forms/ideas/create", data=form | {"mode": ["once"]}).status_code == 200
    prepared = db.scalar(select(MediaJob)).brief["prepared_edit"]
    # Values are held to the editing limits; an edit that doesn't validate is planned again instead.
    assert (prepared["items"][0]["brightness"] == 1.2) if kept else prepared == {}


def test_an_idea_with_no_matching_media_says_what_is_missing(db, settings, facts):  # noqa: F811
    library(db, settings)
    with TestClient(create_app(settings)) as client:
        response = plan(client, facts, prompt="Behind the scenes at a carport job site")
    assert response.status_code == 400
    assert "No real photos or videos in the library show this idea" in response.text
    assert "choose Image or Video to make AI inspiration instead" in response.text


def test_pinterest_takes_real_project_ideas_from_drive_media_only(db, settings, facts):  # noqa: F811
    library(db, settings)
    drive = ingest(db, settings, jpeg((120, 140, 160), (1000, 1500)), "partition-wall-office.jpg", rights="public",
                   tags={"google_drive_file_id": "drive-1", "google_drive_folder_id": "projects"})
    db.commit()
    with TestClient(create_app(settings)) as client:
        assert "Real project only" in client.get("/admin/create").text
        page = plan(client, facts, prompt="Our finished partition wall projects", platforms=["pinterest"]).text
        assert plan_form(page)["real_media"] == [drive.id] and "Google Drive is used" in page
        response = plan(client, facts, prompt="A pergola at sunset", platforms=["pinterest"])
        assert response.status_code == 400 and "Choose Real project" in response.text


def test_files_added_with_an_idea_can_be_edited_into_a_real_post(db, settings, facts):  # noqa: F811
    first, second = jpeg((150, 110, 80)), jpeg((80, 110, 150))
    with TestClient(create_app(settings)) as client:
        data = {
            "csrf": client.get("/api/session").json()["csrf"], "prompt": "Our new terrace", "format": "auto",
            "kind": "auto", "platforms": ["instagram"], "rights": "public", "facts_id": facts.id,
            "upload_use": "real",
        }
        response = client.post("/forms/ideas/plan", data=data,
                               files=[("files", ("one.jpg", first, "image/jpeg")), ("files", ("two.jpg", second, "image/jpeg"))])
    assert response.status_code == 200, response.text
    form = plan_form(response.text)
    assert form["real_format"] == ["photo"] and len(form["real_media"]) == 1
    assert "Add media from the library" in response.text and "one.jpg" in response.text


@pytest.mark.parametrize("seconds,count", [(4, 6), (40, 8), (120, 20)])
def test_longer_videos_get_more_frames_for_the_editor(seconds, count):
    times = sample_times(seconds)
    assert len(times) == count and 0 < times[0] < times[-1] < seconds
