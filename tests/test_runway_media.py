import hashlib
import io
import json
import wave
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import select

from social.app import create_app
from social.db import now
from social.facts import load_facts
from social.gate import media_errors
from social.media_studio import CreativeBrief, process_jobs, queue_jobs, replenish_campaigns, resume_job
from social.models import Asset, Control, MediaCampaign, MediaJob, Post
from social.provider_errors import ProviderError
from social.runway import Runway, payload_for
from social.storage import ingest, storage


def brief_for(db, settings, **overrides):
    sheet = load_facts(db, settings.facts_path)
    values = {
        "campaign": "Fresh ideas",
        "prompt": "Warm architectural textures",
        "platforms": ["instagram"],
        "facts_id": sheet.id,
        "rights": "public",
        "owner": "Tudelu",
        "website": False,
    }
    return CreativeBrief(**(values | overrides))


def wave_bytes():
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        wav.writeframes(b"\0\0" * 48000 * 3)
    return output.getvalue()


@pytest.mark.parametrize(
    "operation,refs,endpoint,model",
    [
        ("image", ["runway://example"], "text_to_image", "gen4_image"),
        ("video", [], "text_to_video", "gen4.5"),
        ("video", ["runway://example"], "image_to_video", "gen4.5"),
        ("video_edit", ["runway://example"], "video_to_video", "aleph2"),
        ("audio", ["runway://example"], "sound_effect", "seed_audio"),
    ],
)
def test_official_runway_payloads(settings, operation, refs, endpoint, model):
    brief = {
        "operation": operation,
        "generation_prompt": "Custom idea",
        "audio_prompt": "Quiet",
        "seed": 42,
        "duration": 5,
    }
    path, payload = payload_for(settings, brief, "instagram", refs)
    assert path == endpoint and payload["model"] == model
    assert payload["promptText"] == "Custom idea"
    if operation == "video_edit":
        assert payload["videoUri"] == refs[0] and payload["targetAspectRatio"] == "9:16"
        assert "duration" not in payload and "ratio" not in payload
    if endpoint == "text_to_video":
        assert "promptImage" not in payload and payload["ratio"] == "720:1280"
    if endpoint == "image_to_video":
        assert payload["promptImage"] == refs[0]
    if operation == "audio":
        assert payload["referenceAudios"] == refs and "seed" not in payload


def test_uploads_private_reference_and_never_leaks_api_key(db, settings, assets, monkeypatch):
    settings.runway_api_key = SecretStr("private-test-key")
    monkeypatch.setattr("social.runway.public_url", lambda url: url)
    seen = []
    original = storage(settings).read(assets[0].storage_key)

    def handle(request):
        seen.append(request)
        if request.url.host == "api.dev.runwayml.com":
            assert request.headers["authorization"] == "Bearer private-test-key"
            assert request.headers["X-Runway-Version"] == "2024-11-06"
            return httpx.Response(
                200,
                json={
                    "uploadUrl": "https://uploads.test/object",
                    "fields": {"x": "signed"},
                    "runwayUri": "runway://reference-token",
                },
            )
        assert "authorization" not in request.headers
        assert original in request.content
        return httpx.Response(204)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert Runway(settings, client).reference(assets[0]) == "runway://reference-token"
    assert len(seen) == 2


def test_runway_auth_error_safe(settings):
    settings.runway_api_key = SecretStr("secret-value")
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(401, json={"error": "secret-value"}))
    ) as client:
        with pytest.raises(ProviderError) as error:
            Runway(settings, client).submit("text_to_video", {})
    assert "secret-value" not in str(error.value)


@pytest.mark.parametrize("operation", ["image", "video", "audio"])
def test_fixture_generation_offline_platform_rendition_and_post(db, settings, operation, monkeypatch):
    def network_forbidden(*args, **kwargs):
        raise AssertionError("Fixture mode must not call Runway")

    monkeypatch.setattr(Runway, "submit", network_forbidden)
    brief = brief_for(db, settings, operation=operation, create_posts=operation != "audio")
    job = queue_jobs(db, settings, brief)[0]
    db.commit()
    for _ in range(4):
        process_jobs(db, settings)
    assert job.state == "complete", job.error
    result = db.get(Asset, job.output_asset_id)
    assert result.demo_only and result.generator.startswith("fixture/runway/")
    if operation == "audio":
        assert result.media["mime"] == "audio/wav" and not job.post_id
    else:
        assert job.post_id and db.get(Post, job.post_id).status == "draft"
        assert (result.media["width"], result.media["height"]) == (
            (1080, 1350) if operation == "image" else (720, 1280)
        )
        assert result.tags["source_asset_ids"]
        if operation == "image":
            assert result.media["embedded_provenance"]["ai_declared"]
        assert db.get(Asset, result.tags["rendition"]["original_asset_id"]).checksum != result.checksum
        from social.config import profiles

        assert media_errors([result], profiles(settings)["instagram"], "instagram") == []
        assert len(db.scalars(select(Post)).all()) == 1


def test_video_with_generated_audio_is_muxed_and_originals_retained(db, settings):
    job = queue_jobs(db, settings, brief_for(db, settings, operation="video", audio_prompt="Quiet ambience"))[
        0
    ]
    db.commit()
    for _ in range(4):
        process_jobs(db, settings)
    assert job.state == "complete", job.error
    assert len(job.source_asset_ids) == 2
    result = db.get(Asset, job.output_asset_id)
    assert result.media["audio_codecs"] == ["aac"]
    assert result.tags["generated_audio"]


def written_prompt(*args):
    return {"generation_prompt": "A pergola at dusk", "depicted_system": "Pergolas", "prompt_writer": {}}


def test_live_task_recovery_download_retry_never_resubmits(db, settings, assets, monkeypatch):
    monkeypatch.setattr("social.media_studio.write_prompt", written_prompt)
    monkeypatch.setattr("social.providers.OpenAI.call", lambda *args, **kwargs: {
        "status": "completed", "output": [{"type": "message", "content": [{
            "type": "output_text", "text": json.dumps({
                "summary": "A green square.", "visible_text": [], "limitations": [],
            }),
        }]}],
    })
    job = queue_jobs(db, settings, brief_for(db, settings, reference_ids=[assets[0].id]))[0]
    job.brief = job.brief | {"execution_mode": "live"}
    db.commit()
    settings.ai_mode, settings.runway_api_key = "live", SecretStr("mock")
    original = storage(settings).read(assets[0].storage_key)

    class FakeRunway:
        submissions, polls, downloads = 0, 0, 0

        def reference(self, asset):
            return "runway://ref"

        def submit(self, endpoint, payload):
            self.submissions += 1
            assert endpoint == "text_to_image"
            assert payload["referenceImages"] == [{"uri": "runway://ref", "tag": "ref0"}]
            # A separate session sees the checkpoint before the paid call.
            from sqlalchemy.orm import Session

            with Session(db.get_bind()) as other:
                assert other.get(MediaJob, job.id).state == "submitting"
            return "task-123"

        def task(self, task_id):
            self.polls += 1
            assert task_id == "task-123"
            return {"status": "SUCCEEDED", "output": ["https://output.test/secret-url"]}

        def download(self, url):
            self.downloads += 1
            if self.downloads == 1:
                raise ProviderError("Temporary download failure", provider="Runway", category="download")
            return original

    provider = FakeRunway()
    process_jobs(db, settings, provider)
    assert job.state == "running" and job.task_id == "task-123"
    process_jobs(db, settings, provider)
    assert job.state == "failed" and job.task_id == "task-123"
    resume_job(job)
    db.commit()
    process_jobs(db, settings, provider)
    process_jobs(db, settings, provider)
    process_jobs(db, settings, provider)
    assert job.state == "complete", job.error
    assert provider.submissions == 1
    assert "secret-url" not in json.dumps(job.brief) + json.dumps(job.task_history)


def test_ambiguous_submission_is_not_retried(db, settings, monkeypatch):
    monkeypatch.setattr("social.media_studio.write_prompt", written_prompt)
    job = queue_jobs(db, settings, brief_for(db, settings))[0]
    job.brief = job.brief | {"execution_mode": "live"}
    db.commit()
    settings.ai_mode, settings.runway_api_key = "live", SecretStr("mock")

    class Interrupted:
        calls = 0

        def submit(self, *args):
            self.calls += 1
            raise ProviderError("Timed out", provider="Runway", category="timeout")

    provider = Interrupted()
    process_jobs(db, settings, provider)
    assert job.state == "uncertain"
    process_jobs(db, settings, provider)
    assert provider.calls == 1
    with pytest.raises(ValueError, match="Reconcile"):
        resume_job(job)


def test_reference_validation_and_audio_not_directly_publishable(db, settings, assets):
    a = assets[0]
    a.rights_expire_at = now() - timedelta(seconds=1)
    with pytest.raises(ValueError, match="expired"):
        queue_jobs(db, settings, brief_for(db, settings, reference_ids=[a.id]))
    a.rights_expire_at = None
    with pytest.raises(ValueError, match="media type"):
        queue_jobs(db, settings, brief_for(db, settings, operation="video_edit", reference_ids=[a.id]))
    audio = ingest(db, settings, wave_bytes(), "original.wav", rights="Company permitted")
    from social.pipeline import draft

    with pytest.raises(ValueError, match="combined with a video"):
        draft(db, "instagram", "test", [audio.id], brief_for(db, settings).facts_id)
    assert hashlib.sha256(storage(settings).read(audio.storage_key)).hexdigest() == audio.checksum


def test_campaign_rotates_concepts_media_and_promotional_mix(db, settings, assets):
    brief = brief_for(db, settings, operation="mixed", kind="mixed", reference_ids=[assets[0].id])
    campaign = MediaCampaign(name="Rotation", brief=brief.model_dump())
    db.add(campaign)
    db.flush()
    jobs = []
    for sequence in range(8):
        jobs += queue_jobs(db, settings, brief, campaign=campaign, sequence=sequence)
        db.commit()
    assert len({j.brief["concept"] for j in jobs}) == 8
    assert {j.brief["operation"] for j in jobs} == {"image", "video"}
    assert {j.brief["kind"] for j in jobs} == {"promotional", "non_promotional"}
    assert len({j.brief["seed"] for j in jobs}) == 8


def test_campaign_waits_for_live_and_kill_switch_stops_jobs(db, settings):
    brief = brief_for(db, settings, operation="mixed", kind="mixed")
    campaign = MediaCampaign(name="Future live", brief=brief.model_dump(), enabled=True)
    db.add(campaign)
    db.commit()
    replenish_campaigns(db, settings)
    assert db.scalars(select(MediaJob)).all() == []
    settings.ai_mode = "live"
    replenish_campaigns(db, settings)
    assert "Waiting" in campaign.error and not campaign.sequence
    settings.ai_mode = "fixture"
    job = queue_jobs(db, settings, brief_for(db, settings))[0]
    db.get(Control, 1).killed = True
    db.commit()
    process_jobs(db, settings)
    assert job.state == "queued"


def test_live_campaign_creates_one_batch_and_blocks_backlog(db, settings, monkeypatch):
    from types import SimpleNamespace

    from social.adapters import ADAPTERS
    from social.facts import save_facts

    sheet = save_facts(
        db,
        {
            "version": "campaign-test",
            "brand_voice": "Simple",
            "hashtags": {},
            "facts": [{"id": "name", "product": "company", "text": "Test brand."}],
        },
    )
    brief = brief_for(db, settings, operation="mixed", kind="mixed").model_copy(update={"facts_id": sheet.id})
    campaign = MediaCampaign(name="Live campaign", brief=brief.model_dump(), enabled=True)
    db.add(campaign)
    db.commit()
    monkeypatch.setitem(ADAPTERS, "instagram", lambda *args: SimpleNamespace(dry=False))
    settings.ai_mode, settings.live_platforms, settings.runway_api_key = (
        "live",
        "instagram",
        SecretStr("mock"),
    )
    replenish_campaigns(db, settings)
    assert campaign.sequence == 1
    assert len(db.scalars(select(MediaJob)).all()) == 1
    campaign.next_run_at = now() - timedelta(hours=1)
    db.commit()
    replenish_campaigns(db, settings)
    assert campaign.sequence == 1 and "previous batch" in campaign.error
    db.scalar(select(MediaJob)).state = "cancelled"
    db.commit()
    replenish_campaigns(db, settings)
    assert campaign.sequence == 2


def test_fixture_mode_does_not_poll_previously_live_task(db, settings):
    job = queue_jobs(db, settings, brief_for(db, settings))[0]
    job.brief = job.brief | {"execution_mode": "live"}
    job.state, job.task_id, job.submitted_at = "running", "saved-task", now()
    db.commit()

    class NoNetwork:
        def task(self, task_id):
            raise AssertionError("Fixture mode cannot poll live tasks")

    process_jobs(db, settings, NoNetwork())
    assert job.state == "running" and not job.error


def test_media_studio_form_api_csrf_and_saved_paused_campaign(db, settings):
    brief = brief_for(db, settings)
    db.commit()
    with TestClient(create_app(settings)) as client:
        assert client.get("/admin/media-studio").status_code == 200
        assert client.post("/api/media-jobs", json=brief.model_dump()).status_code == 403
        token = client.get("/api/session").json()["csrf"]
        response = client.post("/api/media-jobs", json=brief.model_dump(), headers={"x-csrf-token": token})
        assert response.status_code == 200 and len(response.json()["jobs"]) == 1
        response = client.post(
            "/forms/media-studio",
            data={
                "csrf": token,
                "mode": "recurring",
                "campaign": "New campaign",
                "prompt": "Warm textures",
                "platforms": "instagram",
                "facts_id": brief.facts_id,
                "rights": "public",
                "operation": "mixed",
                "kind": "mixed",
                "create_posts": "on",
            },
        )
        assert response.status_code == 200 and "New campaign" in response.text
        assert not db.scalar(select(MediaCampaign)).enabled
        response = client.get("/api/media-jobs")
        assert response.json()[0]["state"] == "queued"


def test_audio_reference_limit_is_total_across_both_inputs(db, settings):
    with pytest.raises(ValueError, match="three reference bites"):
        brief_for(
            db,
            settings,
            operation="audio",
            create_posts=False,
            reference_ids=["a", "b"],
            audio_reference_ids=["c", "d"],
        )


def test_media_form_error_preserves_unchecked_choices(db, settings):
    brief = brief_for(db, settings)
    db.commit()
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/forms/media-studio",
            data={
                "csrf": client.get("/api/session").json()["csrf"],
                "campaign": "My audio",
                "prompt": "A warm melody",
                "platforms": "instagram",
                "facts_id": brief.facts_id,
                "rights": "",
                "operation": "audio",
            },
        )
    assert response.status_code == 400
    assert 'name="create_posts" >' in response.text and 'name="website" >' in response.text
    assert "A warm melody" in response.text


def test_import_existing_private_bucket_object_preserves_source(db, settings, monkeypatch):
    from types import SimpleNamespace

    from social.seed import seed_media

    original = seed_media()[0]
    objects = {"uploads/original.jpg": original}

    class Bucket:
        def __init__(self, settings):
            self.client = SimpleNamespace(close=lambda: None)

        def head(self, key):
            return {"ContentLength": len(objects[key])}

        def read(self, key):
            return objects[key]

        def put(self, key, data, mime):
            objects[key] = data

    monkeypatch.setattr("social.storage.S3Store", Bucket)
    settings.storage_backend, settings.s3_bucket, settings.media_base_url = "s3", "test", "https://media.test"
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/forms/import-bucket-asset",
            data={
                "csrf": client.get("/api/session").json()["csrf"],
                "key": "uploads/original.jpg",
                "rights": "public",
                "owner": "Tudelu",
                "tags": '{"product":"company"}',
            },
            follow_redirects=False,
        )
    assert response.status_code == 303
    asset = db.scalar(select(Asset))
    assert objects["uploads/original.jpg"] == objects[asset.storage_key] == original
    assert asset.source == "company" and asset.tags["imported_bucket_key"] == "uploads/original.jpg"
    assert (asset.rights, asset.owner) == ("public", "Tudelu")
