"""Every video has sound and ends with a cut to the Tudelu logo."""

import io
import wave

import av
import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_idea_plans import facts, logo  # noqa: F401
from test_idea_uploads import create, finish, offline, plan  # noqa: F401

from social.app import create_app
from social.gate import run_gate
from social.media_studio import CreativeBrief, process_jobs, queue_jobs
from social.models import Asset, MediaJob, Post
from social.pipeline import draft, generate
from social.renditions import video_sequence
from social.seed import seed_media
from social.storage import ingest, storage
from social.video_rules import AMBIENT_STYLES, ambient_track, has_sound, next_ambient_style
from social.worker import advance_queue

ORANGE = (230, 110, 30)  # The test logo's color.


def tone(seconds, frequency=440):
    t = np.arange(round(24000 * seconds)) / 24000
    samples = (0.3 * np.sin(2 * np.pi * frequency * t) * 32767).astype("<i2")
    output = io.BytesIO()
    with wave.open(output, "wb") as file:
        file.setparams((2, 2, 24000, 0, "NONE", "not compressed"))
        file.writeframes(np.stack([samples, samples], axis=1).tobytes())
    return output.getvalue()


def house_video(seconds=4, width=720, height=720):
    """Footage with sound that cuts to a logo card, like every video the app makes."""
    from test_idea_plans import png

    from social.renditions import logo_card

    card = logo_card(png(ORANGE + (255,)), width, height, 3)
    footage = seed_media(duration=seconds)[1]
    return video_sequence([(footage, 0, seconds), (card, 0, 3)], width, height, tone(seconds + 3))


# Tags of a video that ends on the logo; tests of other steps ingest house_video() with them.
HOUSE_TAGS = {"end_card": {"asset_id": "", "seconds": 3, "note": "Test video ending on a logo card."}}


def frames(data):
    with av.open(io.BytesIO(data)) as container:
        return [f.to_image() for f in container.decode(video=0)]


def near(pixel, color, tolerance=40):
    return all(abs(a - b) <= tolerance for a, b in zip(pixel, color, strict=False))


def brief(facts, **changes):  # noqa: F811
    values = dict(
        campaign="Pergola at dusk",
        prompt="A pergola at dusk",
        operation="video",
        platforms=["instagram"],
        facts_id=facts.id,
        rights="public",
        duration=10,
        website=False,
    )
    return CreativeBrief(**values | changes)


def test_ambient_tracks_vary_by_style_and_fill_the_video():
    tracks = {style: ambient_track(style, 7.5, seed=3) for style in AMBIENT_STYLES}
    assert len(set(tracks.values())) == len(AMBIENT_STYLES)
    for data in tracks.values():
        with wave.open(io.BytesIO(data)) as file:
            assert file.getnframes() == 7.5 * 24000 and file.getnchannels() == 2
        assert has_sound(data)
    # Each video also gets its own key and chords within a style.
    assert ambient_track("felt_piano", 5, seed=1) != ambient_track("felt_piano", 5, seed=2)
    assert not has_sound(seed_media(duration=3)[1])


def test_generated_videos_get_ambient_music_in_rotation_and_the_logo(db, settings, facts, logo):  # noqa: F811
    first = queue_jobs(db, settings, brief(facts))[0]
    second = queue_jobs(db, settings, brief(facts))[0]
    chosen = queue_jobs(db, settings, brief(facts, audio_prompt="Upbeat acoustic guitar"))[0]
    styles = list(AMBIENT_STYLES)
    assert (first.brief["ambient_style"], second.brief["ambient_style"]) == (styles[0], styles[1])
    assert first.brief["audio_prompt"] == AMBIENT_STYLES[styles[0]][1]
    assert first.brief["soundtrack_model"] == "music"
    assert chosen.brief["audio_prompt"] == "Upbeat acoustic guitar" and "ambient_style" not in chosen.brief
    for job in (first, second, chosen):
        assert job.brief["end_card"] == {"asset_id": logo.id, "seconds": 3}
        # The 3-second logo follows 7 seconds of generated footage.
        assert sum(s["duration"] for s in job.brief["video_segments"]) == 7
    assert next_ambient_style(db) == styles[2]
    with pytest.raises(ValueError, match="at least 5 seconds"):
        queue_jobs(db, settings, brief(facts, duration=4))


def test_videos_need_a_logo_in_the_library(db, settings, facts):  # noqa: F811
    with pytest.raises(ValueError, match="there's no logo in the asset library yet"):
        queue_jobs(db, settings, brief(facts))
    with TestClient(create_app(settings)) as client:
        planned = plan(client, facts, [], prompt="A 10-second video of a pergola at dusk", upload_use="reference")
        assert "There's no logo in the asset library yet, so this video can't be made." in planned.text


def test_generated_video_from_an_idea_ends_on_the_logo(db, settings, facts, logo, offline):  # noqa: F811
    idea = "A 10-second video of a pergola at dusk, no music"
    with TestClient(create_app(settings)) as client:
        planned = plan(client, facts, [], prompt=idea, upload_use="reference")
        page = planned.text
        assert "Every video ends with a cut to the Tudelu logo (3 seconds)." in page
        assert 'placeholder="Leave blank for calm ambient music"' in page and 'name="end_card"' not in page
        assert "gets calm ambient music instead of silence" in page and "1 ambient music track" in page
        created = create(
            client, facts, [], prompt=idea, upload_use="reference", as_uploaded="", operation="video",
            duration="10", shot_setting=["terrace at dusk"], shot_system=[""],
        )
        assert created.status_code == 200, created.text
    job = db.scalar(select(MediaJob))
    assert job.brief["ambient_style"] and job.brief["end_card"]["asset_id"] == logo.id
    finish(db, settings)
    db.refresh(job)
    assert job.state == "complete", job.error
    post = db.get(Post, job.post_id)
    video = db.get(Asset, post.asset_ids[0])
    assert post.status == "awaiting_review" and video.media["audio_codecs"] == ["aac"]
    assert video.tags["end_card"]["asset_id"] == logo.id and abs(video.media["duration"] - 10) < 0.2
    shown = frames(storage(settings).read(video.storage_key))
    middle = (video.media["width"] // 2, video.media["height"] // 2)
    # A cut, not a fade: the logo is fully there from the card's first frame.
    assert near(shown[7 * 30].getpixel(middle), ORANGE) and not near(shown[7 * 30 - 1].getpixel(middle), ORANGE)


def test_your_own_video_keeps_its_sound_or_gets_ambient_music(db, settings, facts, logo, offline):  # noqa: F811
    loud = video_sequence([(seed_media(duration=6)[1], 0, 6)], 720, 720, tone(6))
    with TestClient(create_app(settings)) as client:
        planned = plan(client, facts, [("files", ("quiet.mp4", seed_media(duration=6)[1], "video/mp4"))])
        assert "Your video has no sound, so it gets ambient music made by the app (not by AI)." in planned.text
        assert 'name="silent_video" value="on"' in planned.text and 'name="sound"' not in planned.text
        quiet = db.scalar(select(Asset).where(Asset.original_name == "quiet.mp4"))
        assert create(client, facts, [quiet.id], operation="video", duration="9", silent_video="on").status_code == 200
        planned = plan(client, facts, [("files", ("loud.mp4", loud, "video/mp4"))])
        assert '<option value="own" selected>Keep its own sound</option>' in planned.text
        own = db.scalar(select(Asset).where(Asset.original_name == "loud.mp4"))
        assert create(client, facts, [own.id], operation="video", duration="9", sound="own").status_code == 200
        replaced = create(client, facts, [own.id], operation="video", duration="9", sound="ambient")
        assert replaced.status_code == 200
    finish(db, settings)
    jobs = db.scalars(select(MediaJob).order_by(MediaJob.created_at)).all()
    assert [j.state for j in jobs] == ["complete"] * 3, [j.error for j in jobs]
    copies = [db.get(Asset, db.get(Post, j.post_id).asset_ids[0]) for j in jobs]
    assert [c.tags["video_sound"] for c in copies] == ["ambient", "own", "ambient"]
    assert copies[0].tags["ambient_music"]["style"] in AMBIENT_STYLES
    for job, copy in zip(jobs, copies, strict=True):
        data = storage(settings).read(copy.storage_key)
        assert has_sound(data) and copy.source == "company" and not db.get(Post, job.post_id).ai_disclosure
        assert copy.tags["end_card"]["asset_id"] == logo.id and abs(copy.media["duration"] - 9) < 0.2
        shown = frames(data)
        assert near(shown[6 * 30].getpixel((360, 360)), ORANGE) and not near(shown[6 * 30 - 1].getpixel((360, 360)), ORANGE)


def test_library_videos_get_the_logo_ending_and_sound(db, settings, facts, logo):  # noqa: F811
    clip = ingest(
        db, settings, seed_media(duration=5)[1], "drive-clip.mp4", rights="public",
        tags={"google_drive_file_id": "drive-1"},
    )
    db.commit()
    with TestClient(create_app(settings)) as client:
        csrf = client.get("/api/session").json()["csrf"]
        for platform in ["instagram", "pinterest"]:
            response = client.post(
                "/forms/posts",
                data={"csrf": csrf, "campaign": "Drive clip", "platform": platform, "kind": "promotional",
                      "product": "company", "facts_id": facts.id, "asset_ids": [clip.id]},
            )
            assert response.status_code == 200 and "/posts/" in str(response.url), response.text
    for post in db.scalars(select(Post)).all():
        copy = db.get(Asset, post.asset_ids[0])
        assert copy.tags["rendition"]["original_asset_id"] == clip.id
        assert copy.tags["end_card"]["asset_id"] == logo.id and copy.tags["video_sound"] == "ambient"
        assert abs(copy.media["duration"] - 8) < 0.2 and copy.source == "company"
    assert storage(settings).read(clip.storage_key) == seed_media(duration=5)[1]


def test_held_video_gets_the_logo_ending_and_sound_from_its_page(db, settings, facts, logo):  # noqa: F811
    old = ingest(db, settings, seed_media(duration=5)[1], "old.mp4", rights="public")
    post = draft(db, "tiktok", "Old video", [old.id], facts.id, human_review_required=False)
    generate(db, settings, post)
    run_gate(db, settings, post)
    assert post.status == "held"
    with TestClient(create_app(settings)) as client:
        page = client.get(f"/posts/{post.id}").text
        assert "Every video needs sound and must end with a cut to the Tudelu logo." in page
        assert "The video has no sound." in page and "The video doesn&#39;t end with the Tudelu logo." in page
        assert "Add the logo ending and sound</button>" in page
        csrf = client.get("/api/session").json()["csrf"]
        response = client.post(f"/forms/posts/{post.id}/finish_video", data={"csrf": csrf})
        assert response.status_code == 200, response.text
    db.refresh(post)
    copy = db.get(Asset, post.asset_ids[0])
    assert post.status == "awaiting_review" and copy.tags["end_card"]["asset_id"] == logo.id
    assert copy.tags["video_sound"] == "ambient" and abs(copy.media["duration"] - 8) < 0.2


def test_videos_planned_before_the_rule_still_end_on_the_logo(db, settings, facts, logo):  # noqa: F811
    job = queue_jobs(db, settings, brief(facts))[0]
    # A job queued before every video had music and the logo ending.
    job.brief = job.brief | {"end_card": None, "end_card_asset_id": "", "audio_prompt": "", "ambient_style": None}
    db.commit()
    for _ in range(6):
        process_jobs(db, settings)
        advance_queue(db, settings)
    db.refresh(job)
    assert job.state == "complete", job.error
    video = db.get(Asset, db.get(Post, job.post_id).asset_ids[0])
    assert video.tags["end_card"]["asset_id"] == logo.id and video.tags["video_sound"] == "ambient"
    assert has_sound(storage(settings).read(video.storage_key))
