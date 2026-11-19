"""Clip lengths in ideas, editing clips on the plan page, and describing media changes on the review page."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_clip_replace import held_video, run
from test_idea_plans import facts, logo  # noqa: F401
from test_real_edit import footage, jpeg
from test_real_ideas import plan, plan_form

from social.app import create_app
from social.idea_plan import read_signals
from social.lifecycle import transition
from social.media_studio import process_jobs
from social.models import Asset, MediaJob, Post, Transition
from social.providers import OpenAI
from social.real_edit import EditItem, RealEditPlan, fit_plan, plan_edit
from social.storage import ingest, storage
from social.visual_context import visual_inputs

IDEA = (
    "short video collection of real clips from partition wall installation. 3-5 second clips of showing the "
    "walls opening and closing"
)


@pytest.mark.parametrize("idea,seconds,clips", [
    (IDEA, None, [3.0, 5.0]),
    ("a 30 second reel of 5 second clips", 30, [5.0, 5.0]),
    ("a 15-30 second reel", 30, None),
    ("clips of about 4 seconds each, 20 seconds total", 20, [4.0, 4.0]),
    ("between 3 and 5 seconds per clip", None, [3.0, 5.0]),
    ("5-second shots of the canopy", None, [5.0, 5.0]),
    ("a 30-second video", 30, None),
])
def test_a_length_per_clip_is_not_the_length_of_the_video(idea, seconds, clips):
    signals = read_signals(idea)
    assert (signals["seconds"], signals["clip_seconds"], signals["video"]) == (seconds, clips, True)


def test_fitting_an_edit_keeps_each_clip_within_the_lengths_asked_for():
    video = SimpleNamespace(id="v", media={"mime": "video/mp4", "duration": 30})
    photo = SimpleNamespace(id="p", media={"mime": "image/jpeg"})
    planned = RealEditPlan(layout="sequence", items=[
        EditItem(asset_id="v", start=1, end=12), EditItem(asset_id="v", start=20, end=21),
        EditItem(asset_id="p", end=6),
    ])
    fitted = fit_plan(planned, [video, photo], [], True, 17, clip_seconds=[3, 5])
    assert [(i.start, i.end) for i in fitted.items] == [(1, 6), (20, 23), (0, 5)]
    # Without lengths from the idea, the editor's own trims stand.
    assert [(i.start, i.end) for i in fit_plan(planned, [video, photo], [], True, 30).items] == [
        (1, 12), (20, 21.5), (0, 6)
    ]


def library(db, settings):
    walls = ingest(db, settings, footage(16), "partition-walls-opening.mp4", rights="public",
                   tags={"product": "Retractable Partition Walls"})
    closed = ingest(db, settings, jpeg((180, 160, 140)), "partition-wall-closed.jpg", rights="public",
                    tags={"product": "Retractable Partition Walls"})
    sunset = ingest(db, settings, jpeg((200, 120, 60)), "pergola-sunset.jpg", rights="public")
    db.commit()
    return walls, closed, sunset


def test_short_clips_of_an_installation_are_cut_from_the_footage(db, settings, facts, logo):  # noqa: F811
    walls, closed, sunset = library(db, settings)
    with TestClient(create_app(settings)) as client:
        response = plan(client, facts, prompt=IDEA)
        assert response.status_code == 200, response.text
    page = response.text
    assert "Each clip lasts 3–5 seconds, as written in your idea." in page
    form = plan_form(page)
    edit = json.loads(form["real_plan"][0])
    # The idea asks for clips, so the matching photo isn't used; the one video gives several clips.
    assert {i["asset_id"] for i in edit["items"]} == {walls.id} and len(edit["items"]) == 3
    assert all(3 <= i["end"] - i["start"] <= 5 for i in edit["items"])
    assert form["duration"] == ["18"] and "Clips, in viewing order" in page


def test_the_plan_page_plans_the_edit_again_with_the_changes_asked_for(db, settings, facts, logo):  # noqa: F811
    walls, closed, sunset = library(db, settings)
    with TestClient(create_app(settings)) as client:
        form = plan_form(plan(client, facts, prompt=IDEA).text)
        before = json.loads(form["real_plan"][0])["items"]
        again = form | {"mode": ["replan"], "edit_request": ["Add the closed wall photo at the end"],
                        "real_media": [closed.id]}
        response = client.post("/forms/ideas/create", data=again)
        assert response.status_code == 200, response.text
        assert "Planned again with your changes: Add the closed wall photo at the end" in response.text
        after = json.loads(plan_form(response.text)["real_plan"][0])["items"]
    # Offline, the previous edit is kept and the added photo follows it.
    assert [i["asset_id"] for i in after] == [i["asset_id"] for i in before] + [closed.id]
    assert not db.scalars(select(MediaJob)).all()


def review(db, settings, client, facts, prompt=IDEA):  # noqa: F811
    form = plan_form(plan(client, facts, prompt=prompt).text)
    assert client.post("/forms/ideas/create", data=form | {"mode": ["once"]}).status_code == 200
    job = db.scalar(select(MediaJob))
    for _ in range(3):
        process_jobs(db, settings)
    db.refresh(job)
    assert job.state == "complete", job.error
    post = db.get(Post, job.post_id)
    for status in ("generated", "checked", "awaiting_review"):
        transition(db, post, status, "Test")
    db.commit()
    return job, post


def test_a_real_post_is_edited_again_from_the_changes_described_on_its_page(db, settings, facts, logo):  # noqa: F811
    walls, closed, sunset = library(db, settings)
    with TestClient(create_app(settings)) as client:
        token = client.get("/api/session").json()["csrf"]
        job, post = review(db, settings, client, facts)
        page = client.get(f"/posts/{post.id}").text
        assert 'id="change-media"' in page and "What should change in the media?" in page
        assert "Add media from the library (optional)" in page and "pergola-sunset.jpg" in page
        assert "Regenerate part of the" not in page
        blank = client.post(f"/forms/posts/{post.id}/change-media", data={"csrf": token, "change_request": " "})
        assert blank.status_code == 400 and "Describe what should change in the media" in blank.text
        request = "Make every clip 3 seconds and end on the closed wall photo"
        queued = client.post(f"/forms/posts/{post.id}/change-media",
                             data={"csrf": token, "change_request": request, "real_media": [closed.id]})
        assert queued.status_code == 200 and str(queued.url).endswith("/admin/media-queue")
        assert "Editing the media of a" in queued.text and "Nothing is generated" in queued.text
        page = client.get(f"/posts/{post.id}").text
        assert "Editing your media again…" in page and request in page and 'id="change-media"' not in page
        again = client.post(f"/forms/posts/{post.id}/change-media", data={"csrf": token, "change_request": "x"})
        assert again.status_code == 400

    revised = db.scalars(select(MediaJob).where(MediaJob.id != job.id)).one()
    assert revised.brief["revision"]["request"] == request
    assert revised.brief["revision"]["previous_edit"]["items"] == job.brief["real_edit"]["items"]
    assert revised.brief["reference_ids"] == [walls.id, closed.id] and "real_edit" not in revised.brief
    assert revised.brief["replaces"]["post_id"] == post.id and revised.brief["replaces"]["request"] == request
    db.refresh(post)
    assert post.status == "held"
    for _ in range(3):
        process_jobs(db, settings)
    db.refresh(revised)
    assert revised.state == "complete", revised.error
    items = revised.brief["real_edit"]["items"]
    # The previous clips are kept, now 3 seconds each as asked, and the added photo ends the video.
    assert [i["asset_id"] for i in items][-1] == closed.id and all(i["end"] - i["start"] <= 3.01 for i in items)
    result = db.get(Asset, revised.output_asset_id)
    assert result.source == "company" and result.tags["change_request"] == request
    db.refresh(post)
    replacement = db.get(Post, revised.post_id)
    assert post.status == "failed" and replacement.asset_ids == [result.id]
    reason = db.scalars(select(Transition.reason).where(Transition.post_id == post.id)).all()[-1]
    assert reason == f"Replaced by post {replacement.id} with the media changes requested: {request}"
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/posts/{replacement.id}").text
    assert "Changes you asked for" in page and "What the editor changed" in page


def test_the_editor_gets_the_change_and_the_edit_it_changes(db, settings, facts, logo, monkeypatch):  # noqa: F811
    walls, closed, sunset = library(db, settings)
    with TestClient(create_app(settings)) as client:
        job, post = review(db, settings, client, facts)
        token = client.get("/api/session").json()["csrf"]
        client.post(f"/forms/posts/{post.id}/change-media", data={"csrf": token, "change_request": "Brighter"})
    revised = db.scalars(select(MediaJob).where(MediaJob.id != job.id)).one()
    revised.brief = revised.brief | {"execution_mode": "live"}
    settings.ai_mode = "live"
    sent = {}

    def call(self, endpoint, payload):
        sent.update(payload)
        answer = job.brief["real_edit"] | {"changes": "Brightened every clip a little."}
        answer = {k: v for k, v in answer.items() if k in RealEditPlan.model_fields}
        answer["items"] = [i | {"brightness": 1.1} for i in answer["items"]]
        return {"status": "completed",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(answer)}]}]}

    monkeypatch.setattr(OpenAI, "call", call)
    edit = plan_edit(db, settings, revised)
    context = json.loads(sent["input"][0]["content"][0]["text"].split(" Input: ", 1)[1])
    assert context["change_request"] == "Brighter" and context["recently_shown"] == []
    # The idea's 3-5 second clips don't override the reviewed trims unless the request gives lengths.
    assert context["clip_seconds"] is None
    assert context["previous_edit"]["items"] == job.brief["real_edit"]["items"]
    assert edit.changes == "Brightened every clip a little." and all(i.brightness == 1.1 for i in edit.items)


def test_described_changes_to_ai_media_make_only_the_parts_they_need_again(db, settings, facts, logo, monkeypatch):  # noqa: F811
    with TestClient(create_app(settings)) as client:
        token, job, post = held_video(db, settings, client, facts, logo)
        page = client.get(f"/posts/{post.id}").text
        assert 'id="change-media"' in page and "Only the parts your change needs are made again" in page
        assert 'id="fix-clip"' in page  # Prompts can still be edited part by part.
        queued = client.post(f"/forms/posts/{post.id}/change-media",
                             data={"csrf": token, "change_request": "Calmer music with soft piano"})
        assert queued.status_code == 200 and "Your changes: “Calmer music with soft piano”" in queued.text
    redo = db.scalars(select(MediaJob).where(MediaJob.id != job.id)).one()
    assert redo.brief["replaces"]["part"] == "music" and redo.brief["replaces"]["request"] == "Calmer music with soft piano"
    assert redo.brief["audio_prompt"].endswith("Calmer music with soft piano")
    run(db, settings, redo)
    replacement = db.get(Post, redo.post_id)
    with TestClient(create_app(settings)) as client:
        assert "Changes you asked for" in client.get(f"/posts/{replacement.id}").text


def test_the_model_chooses_which_ai_parts_to_make_again(db, settings, facts, logo, monkeypatch):  # noqa: F811
    with TestClient(create_app(settings)) as client:
        token, job, post = held_video(db, settings, client, facts, logo)
        settings.ai_mode = "live"
        answers = iter([
            {"changes": [], "problem": "The request is about the caption, not the media."},
            {"changes": [{"key": "1", "prompt": "A black pergola on a walkway at dusk"},
                         {"key": "3", "prompt": "A black carport at dusk"},
                         {"key": "music", "prompt": "Soft piano"}], "problem": ""},
        ])
        sent = []

        def call(self, endpoint, payload):
            sent.append(payload)
            answer = next(answers)
            return {"status": "completed", "output": [
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps(answer)}]}]}

        monkeypatch.setattr(OpenAI, "call", call)
        refused = client.post(f"/forms/posts/{post.id}/change-media",
                              data={"csrf": token, "change_request": "Shorter caption"})
        assert refused.status_code == 400 and "The request is about the caption, not the media." in refused.text
        assert "Shorter caption" in refused.text  # The request is kept in the box.
        queued = client.post(f"/forms/posts/{post.id}/change-media",
                             data={"csrf": token, "change_request": "Make the structures black at dusk"})
        assert queued.status_code == 200
    assert sent[0]["text"]["format"]["name"] == "media_changes"
    parts = json.loads(sent[0]["input"][0]["content"][0]["text"].split(" Input: ", 1)[1])["parts"]
    assert [p["key"] for p in parts] == ["0", "1", "2", "3", "music"]
    redo = db.scalars(select(MediaJob).where(MediaJob.id != job.id)).one()
    # Several parts can only be clips, so the two clips are made again and the music is reused.
    assert redo.brief["replaces"]["part"] == "1,3" and redo.brief["replaces"]["label"] == "clips 2 and 4"
    segments = redo.brief["video_segments"]
    assert segments[1]["prompt"] == "A black pergola on a walkway at dusk"
    assert segments[1]["scene_retry"]["operator_prompt"] == "A black pergola on a walkway at dusk"
    assert segments[0] == job.brief["video_segments"][0]


def test_only_held_or_unreviewed_posts_take_media_changes(db, settings, facts, logo):  # noqa: F811
    library(db, settings)
    with TestClient(create_app(settings)) as client:
        token = client.get("/api/session").json()["csrf"]
        job, post = review(db, settings, client, facts)
        transition(db, post, "deleted", "Test")
        db.commit()
        gone = client.post(f"/forms/posts/{post.id}/change-media", data={"csrf": token, "change_request": "x"})
        assert gone.status_code == 404
    assert db.scalars(select(MediaJob)).all() == [job]


def test_long_videos_are_described_from_frames_found_by_seeking():
    data = footage(40, size=(160, 90), sound=False)
    images, basis, times, _ = visual_inputs(SimpleNamespace(media={"mime": "video/mp4", "duration": 40}), data)
    assert basis == "sampled_video_frames" and len(images) == len(times) == 12
    assert times == sorted(times) and times[0] < 1 and times[-1] > 39


def test_storage_is_untouched_by_a_refused_change(db, settings, facts, logo):  # noqa: F811
    walls, closed, sunset = library(db, settings)
    with TestClient(create_app(settings)) as client:
        token = client.get("/api/session").json()["csrf"]
        job, post = review(db, settings, client, facts)
        concept = ingest(db, settings, jpeg((90, 90, 90)), "concept.jpg", source="ai_generated",
                         generator="runway/gen4_image", prompt="Wall", rights="public")
        db.commit()
        refused = client.post(f"/forms/posts/{post.id}/change-media",
                              data={"csrf": token, "change_request": "Add this", "real_media": [concept.id]})
        assert refused.status_code == 400 and "be used in this post" in refused.text
    db.refresh(post)
    assert post.status == "awaiting_review" and storage(settings).read(walls.storage_key)
