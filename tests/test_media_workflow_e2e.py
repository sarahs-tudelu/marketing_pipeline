"""Exercise the upload -> creative queue -> worker -> caption HTTP workflow offline."""

import hashlib
import io
import wave

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from social.app import create_app
from social.media_studio import process_jobs
from social.models import Asset, FactSheet, Post
from social.seed import seed_media
from social.storage import storage


@pytest.mark.parametrize("operation", ["image", "video", "video_edit", "audio", "soundtrack"])
def test_uploaded_reference_to_generated_media_and_caption(db, settings, monkeypatch, operation):
    def no_network(*args, **kwargs):
        raise AssertionError("The fixture workflow must not contact external services")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    settings.auto_advance = False
    image, video = seed_media()
    audio = io.BytesIO()
    with wave.open(audio, "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\0\0" * 16000)
    filename, data, mime = {
        "image": ("reference.jpg", image, "image/jpeg"),
        "video": ("reference.jpg", image, "image/jpeg"),
        "video_edit": ("reference.mp4", video, "video/mp4"),
        "audio": ("reference.wav", audio.getvalue(), "audio/wav"),
        "soundtrack": ("reference.wav", audio.getvalue(), "audio/wav"),
    }[operation]

    with TestClient(create_app(settings)) as client:
        token = client.get("/api/session").json()["csrf"]
        uploaded = client.post(
            "/forms/assets",
            data={
                "csrf": token,
                "source": "company",
                "rights": "not_public",
                "owner": "Synthetic test media",
                "tags": '{"product":"demo"}',
            },
            files={"file": (filename, data, mime)},
        )
        assert uploaded.status_code == 200, uploaded.text
        assert filename in uploaded.text
        db.expire_all()
        source = db.scalar(select(Asset).where(Asset.original_name == filename))
        assert source.checksum == hashlib.sha256(data).hexdigest()
        assert storage(settings).read(source.storage_key) == data
        served = client.get(f"/media/{source.storage_key}")
        assert served.status_code == 200 and served.content == data
        if mime.startswith("video/"):
            partial = client.get(f"/media/{source.storage_key}", headers={"Range": "bytes=0-31"})
            assert partial.status_code == 206 and partial.content == data[:32]

        # Populated library rendering is part of the generation workflow.
        studio = client.get("/admin/media-studio")
        assert studio.status_code == 200 and filename in studio.text
        facts = db.scalar(select(FactSheet))
        form = {
            "csrf": token,
            "campaign": f"E2E {operation}",
            "prompt": "An internal architectural test scene with calm light",
            "operation": "video" if operation == "soundtrack" else operation,
            "platforms": "instagram",
            "facts_id": facts.id,
            "product": "demo",
            "kind": "promotional",
            "rights": "not_public",
            "duration": "3",
        }
        if operation == "soundtrack":
            form.update(audio_prompt="Quiet ambient tone", audio_reference_ids=source.id)
        else:
            form["reference_ids"] = source.id
        if operation != "audio":
            form["create_posts"] = "on"
        queued = client.post("/forms/media-studio", data=form)
        assert queued.status_code == 200, queued.text
        jobs = client.get("/api/media-jobs").json()
        assert len(jobs) == 1 and jobs[0]["state"] == "queued"
        for _ in range(4):
            process_jobs(db, settings)
        job = client.get("/api/media-jobs").json()[0]
        assert job["state"] == "complete", job["error"]
        result = db.get(Asset, job["asset_id"])
        assert result.demo_only and result.generator.startswith("fixture/runway/")
        assert client.get(f"/media/{result.storage_key}").status_code == 200
        assert client.get("/admin/media-studio").status_code == 200
        assert client.get("/admin/assets").status_code == 200
        if operation == "audio":
            assert result.media["mime"] == "audio/wav" and not job["post_id"]
            return
        if operation == "soundtrack":
            assert result.media["audio_codecs"] == ["aac"]
        post_id = job["post_id"]
        generated = client.post(f"/forms/posts/{post_id}/generate", data={"csrf": token})
        assert generated.status_code == 200, generated.text
        db.expire_all()
        post = db.get(Post, post_id)
        assert post.status == "generated" and post.caption and post.fact_ids
        assert post.dry_run and post.ai_disclosure
        assert post.caption in generated.text
        checked = client.post(f"/forms/posts/{post_id}/check", data={"csrf": token})
        assert checked.status_code == 200, checked.text
        db.refresh(post)
        assert post.status == "approved"
        assert post.remote_id == "" and post.published_at is None


def test_invalid_upload_preserves_metadata_then_valid_retry_succeeds(db, settings):
    with TestClient(create_app(settings)) as client:
        form = {
            "csrf": client.get("/api/session").json()["csrf"],
            "source": "company",
            "rights": "not_public",
            "owner": "Internal retry test",
            "tags": '{"product":"demo"}',
        }
        failed = client.post(
            "/forms/assets", data=form, files={"file": ("broken.jpg", b"not an image", "image/jpeg")}
        )
        assert failed.status_code == 400
        assert "Internal retry test" in failed.text and "broken.jpg" in failed.text
        assert not db.scalars(select(Asset)).all()
        image, _ = seed_media()
        retried = client.post("/forms/assets", data=form, files={"file": ("valid.jpg", image, "image/jpeg")})
        assert retried.status_code == 200 and "valid.jpg" in retried.text
        assert len(db.scalars(select(Asset)).all()) == 1
