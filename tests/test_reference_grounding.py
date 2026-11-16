import hashlib
import io
import json
from datetime import timedelta

import httpx
import pytest
from PIL import Image
from pydantic import SecretStr
from test_product_rules import job_for
from test_product_rules import sheet as sheet

from social import reference_grounding as refs
from social.db import now
from social.media_studio import _save_output, process_jobs
from social.models import Asset
from social.product_rules import HANGER_ROD_CUE, VisualGuidance, profile_for
from social.providers import OpenAI
from social.runway import payload_for
from social.storage import ingest, storage


@pytest.fixture
def reference_job(db, settings, sheet, monkeypatch):
    job = job_for(db, settings, sheet, rights="public")
    job.brief = job.brief | {
        "execution_mode": "live",
        "generation_prompt": "A pergola on a terrace.",
        "depicted_system": "Pergolas",
        "prompt_writer": {
            "writer": "test",
            "sources": {
                "facts_version": sheet.version,
                "uploaded_asset_ids": [],
                "reference_ids": [],
                "previous_asset_ids": [],
                "website": None,
            },
        },
    }
    db.commit()
    monkeypatch.setattr(refs, "website_references", lambda *args, **kwargs: [])
    return job


@pytest.fixture
def photo(db, settings):
    stream = io.BytesIO()
    Image.new("RGB", (720, 960), "gray").save(stream, format="PNG")
    asset = ingest(
        db,
        settings,
        stream.getvalue(),
        "pergola-photo.png",
        rights="public",
        tags={"product": "pergolas", "google_drive_file_id": "drive-photo"},
    )
    db.commit()
    return asset


def choice(record, prompt="The existing fixed pergola remains stationary; a slow camera push."):
    return refs.Selection(
        reference_id=record["id"],
        physical_description="Straight aluminum frame, fixed louvers and four posts.",
        prompt=prompt,
        scene_prompt="New white aluminum pergola in a courtyard, warm soft light, using @ref0 construction.",
        creative_changes="New courtyard and white finish.",
        reason="The photographed roof and supports match the Builder.",
        problem="",
    )


def test_scene_product_requirements_survive_long_creative_prompt():
    guidance = VisualGuidance.model_validate(refs.reference_config("visual-guidance.builder.yaml"))
    profile = profile_for(guidance, "canopies")
    requirements = [
        "One rectangular canopy roof",
        "Warm Wood Look aluminum on fascia and both front posts",
        "Sharp stepped Crown trim",
        "Smooth underside lit only by the continuous Crown LED line",
        "Plain unmarked fascia and clear entrance glazing",
    ]
    prompt = refs.compose_scene_prompt("A peaceful plaza at dusk. " * 150 + "\U0001f305" * 40,
                                      requirements, profile, has_style=True)
    assert all(item in prompt for item in requirements)
    assert "@ref0" in prompt and "@ref1" in prompt
    assert prompt.endswith(HANGER_ROD_CUE)
    assert len(prompt.encode("utf-16-le")) <= 2000


def test_overlong_product_requirements_stop_instead_of_silently_disappearing():
    guidance = VisualGuidance.model_validate(refs.reference_config("visual-guidance.builder.yaml"))
    with pytest.raises(ValueError, match="requirements exceed"):
        refs.compose_scene_prompt("An entrance", ["Required detail " * 100], profile_for(guidance, "canopies"))


def test_image_generation_keeps_the_image_request_with_required_product_details(
    db, settings, reference_job, photo, monkeypatch
):
    monkeypatch.setattr(refs, "select_reference", lambda s, r, c: choice(r[0]).model_copy(update={
        "prompt": "A sunny courtyard with the pergola as the subject.",
        "scene_prompt": "Unused video scene request.",
        "scene_requirements": ["White aluminum pergola with connected fixed louvers"],
    }))
    refs.prepare(db, settings, reference_job)
    prompt = refs.current_plan(reference_job)["prompt"]
    assert "White aluminum pergola with connected fixed louvers" in prompt
    assert "sunny courtyard" in prompt and "Unused video" not in prompt


def test_every_clip_uses_the_actual_drive_photo_and_cached_selection(
    db, settings, reference_job, photo, monkeypatch
):
    job = reference_job
    job.brief = job.brief | {
        "operation": "video",
        "video_segments": [
            {
                "index": i,
                "start": i * 6,
                "duration": 6,
                "seed": i,
                "system": "Pergolas",
                "prompt": "Show the pergola.",
                "shot": {"system": "Pergolas", "setting": "terrace"},
            }
            for i in range(3)
        ],
    }
    db.commit()
    observed = []

    def select(s, records, context):
        observed.append(context)
        assert records[0]["asset_id"] == photo.id and records[0]["origin"] == "google_drive"
        assert refs.read_reference(settings, records[0]) == storage(settings).read(photo.storage_key)
        return choice(records[0])

    monkeypatch.setattr(refs, "select_reference", select)
    for index in range(3):
        selected = refs.prepare(db, settings, job)
        assert refs.prepare(db, settings, job) == selected  # resume never repeats the model call
        endpoint, payload = payload_for(
            settings, job.brief | {"duration": 6}, job.platform, ["runway://" + selected[0]["checksum"]]
        )
        assert endpoint == "image_to_video" and payload["promptImage"].startswith("runway://")
        assert len(observed) == index + 1
        assert len(observed[-1]["prior_scene_references"]) == index
        job.brief = job.brief | {
            "video_segments": [
                s | {"output_asset_id": "saved"} if s["index"] == index else s
                for s in job.brief["video_segments"]
            ]
        }
        db.commit()
    assert job.brief["grounded_reference_ids"] == [photo.id]
    assert all(
        s["reference_plan"]["references"][0]["asset_id"] == photo.id for s in job.brief["video_segments"]
    )


@pytest.mark.parametrize("change", ["rights", "expiry", "checksum", "source_tags"])
def test_saved_reference_is_revalidated_before_generation(
    db, settings, reference_job, photo, monkeypatch, change
):
    monkeypatch.setattr(refs, "select_reference", lambda s, r, c: choice(r[0]))
    refs.prepare(db, settings, reference_job)
    if change == "rights":
        photo.rights = "not_public"
    elif change == "expiry":
        photo.rights_expire_at = now() - timedelta(days=1)
    elif change == "checksum":
        photo.checksum = "0" * 64
    else:
        photo.tags = photo.tags | {"product": "carports"}
    db.commit()
    with pytest.raises(ValueError, match="permissions changed"):
        refs.prepare(db, settings, reference_job)


def test_restricted_and_conflicting_references_are_not_chosen(
    db, settings, reference_job, photo, monkeypatch
):
    photo.rights = "not_public"
    db.commit()
    with pytest.raises(ValueError, match="No Public product reference"):
        refs.prepare(db, settings, reference_job)
    photo.rights, photo.tags = "public", photo.tags | {"real_customer_content": True}
    db.commit()
    with pytest.raises(ValueError, match="No Public product reference"):
        refs.prepare(db, settings, reference_job)
    photo.tags = {"brand_logo": True}
    db.commit()
    with pytest.raises(ValueError, match="No Public product reference"):
        refs.prepare(db, settings, reference_job)
    photo.tags = {"product": "carports"}
    db.commit()
    monkeypatch.setattr(
        refs,
        "select_reference",
        lambda *args: refs.Selection(
            reference_id="",
            physical_description="",
            prompt="",
            reason="",
            problem="Only a carport is visible, not a pergola.",
        ),
    )
    with pytest.raises(ValueError, match="Only a carport"):
        refs.prepare(db, settings, reference_job)


def test_regenerated_clip_reselects_when_the_requested_configuration_changes(
    db, settings, reference_job, photo, monkeypatch
):
    calls = []
    monkeypatch.setattr(refs, "select_reference", lambda s, r, c: calls.append(c) or choice(r[0]))
    refs.prepare(db, settings, reference_job)
    reference_job.brief = reference_job.brief | {"generation_prompt": "Show a different view of the pergola."}
    db.commit()
    refs.prepare(db, settings, reference_job)
    assert len(calls) == 2
    assert "different view" in calls[1]["prompt"]


def test_unknown_selection_and_incomplete_review_stop_without_fallback(
    db, settings, reference_job, photo, monkeypatch
):
    select_reference = refs.select_reference
    monkeypatch.setattr(refs, "select_reference", lambda *args: choice({"id": "invented"}))
    with pytest.raises(ValueError, match="unknown image"):
        refs.prepare(db, settings, reference_job)
    monkeypatch.setattr(OpenAI, "call", lambda *args: {"status": "incomplete"})
    with pytest.raises(ValueError, match="did not finish"):
        select_reference(settings, [refs.asset_record(photo)], {})


def test_selector_receives_pixels_and_builder_rules(db, settings, reference_job, photo, monkeypatch):
    seen = []

    def response(self, endpoint, body):
        seen.append(body)
        return {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": choice(refs.asset_record(photo)).model_dump_json()}
                    ],
                }
            ],
        }

    monkeypatch.setattr(OpenAI, "call", response)
    refs.prepare(db, settings, reference_job)
    content = seen[0]["input"][0]["content"]
    assert any(p["type"] == "input_image" and p["image_url"].startswith("data:image/") for p in content)
    assert "fixed" in content[0]["text"] and "higher wall anchors" in content[0]["text"]
    assert "not proof of an installed project" in content[0]["text"]


def test_worker_sends_reference_and_records_ancestry(db, settings, reference_job, photo, monkeypatch):
    monkeypatch.setattr(refs, "select_reference", lambda s, r, c: choice(r[0]))
    settings.ai_mode, settings.runway_api_key = "live", SecretStr("test")

    class Provider:
        def reference(self, asset):
            assert asset.asset_id == photo.id
            return "runway://company-photo"

        def submit(self, endpoint, payload):
            assert endpoint == "text_to_image"
            assert payload["referenceImages"] == [{"uri": "runway://company-photo", "tag": "ref0"}]
            db.refresh(reference_job)
            assert reference_job.brief["reference_plan"]["references"][0]["asset_id"] == photo.id
            return "new-task"

    process_jobs(db, settings, Provider())
    assert reference_job.state == "running", reference_job.error
    _save_output(db, settings, reference_job, storage(settings).read(photo.storage_key))
    output = db.get(Asset, reference_job.source_asset_ids[-1])
    assert photo.id in output.tags["source_asset_ids"]
    assert output.tags["reference_plan"]["references"][0]["checksum"] == photo.checksum


def test_conflicting_reference_stops_worker_before_any_runway_call(
    db, settings, reference_job, photo, monkeypatch
):
    monkeypatch.setattr(
        refs,
        "select_reference",
        lambda *args: refs.Selection(
            reference_id="",
            physical_description="",
            prompt="",
            reason="",
            problem="The reference shows operable blades, not fixed louvers.",
        ),
    )
    settings.ai_mode, settings.runway_api_key = "live", SecretStr("test")

    class Provider:
        def reference(self, *args):
            pytest.fail("Must not upload conflicting reference")

        def submit(self, *args):
            pytest.fail("Must not buy ungrounded generation")

    process_jobs(db, settings, Provider())
    assert reference_job.state == "failed" and "operable blades" in reference_job.error
    assert not reference_job.task_id


def test_library_candidate_selection_uses_both_sources_and_excludes_unselected_ai(
    db, settings, reference_job, photo, monkeypatch
):
    from social.product_rules import profile_for

    site = refs.asset_record(photo) | {"id": "website:candidate", "origin": "company_website"}
    monkeypatch.setattr(refs, "website_references", lambda *args: [site])
    ai = ingest(
        db,
        settings,
        storage(settings).read(photo.storage_key),
        "pergola-ai.png",
        source="ai_generated",
        generator="test",
        prompt="previous generation",
        rights="public",
        tags={"product": "pergolas"},
    )
    db.commit()
    result = refs.candidates(
        db, settings, reference_job, profile_for(refs.builder_guidance(db, reference_job), "Pergolas")
    )
    assert [r["origin"] for r in result] == ["google_drive", "company_website"]
    assert ai.id not in [r["id"] for r in result]


def test_ancestor_permissions_block_reference(db, settings, reference_job, photo, monkeypatch):
    source = ingest(
        db, settings, storage(settings).read(photo.storage_key), "restricted-source.png", rights="not_public"
    )
    photo.tags = photo.tags | {"source_asset_ids": [source.id]}
    db.commit()
    monkeypatch.setattr(refs, "select_reference", lambda s, r, c: choice(r[0]))
    with pytest.raises(ValueError, match="source media is not cleared"):
        refs.prepare(db, settings, reference_job)


def test_reference_links_render_in_both_review_screens(db, settings, reference_job, photo, post, monkeypatch):
    from fastapi.testclient import TestClient

    from social.app import create_app

    monkeypatch.setattr(refs, "select_reference", lambda s, r, c: choice(r[0]))
    refs.prepare(db, settings, reference_job)
    plan = reference_job.brief["reference_plan"] | {"scene_reference": refs.asset_record(photo),
                                                  "scene_check": {"passed": True}}
    reference_job.brief = reference_job.brief | {"reference_plan": plan}
    photo.tags = photo.tags | {"reference_plan": plan}
    post.asset_ids = [photo.id]
    db.commit()
    with TestClient(create_app(settings)) as client:
        for path in (f"/posts/{post.id}", "/admin/media-queue"):
            response = client.get(path)
            assert response.status_code == 200
            assert "Google Drive photo" in response.text and "View source image" in response.text
            assert "Straight aluminum frame" in response.text
            assert "View generated scene" in response.text and "Shared visual direction" in response.text


def test_fixture_and_existing_video_edits_do_not_call_reference_selector(
    db, settings, reference_job, monkeypatch
):
    monkeypatch.setattr(refs, "select_reference", lambda *args: pytest.fail("unexpected model call"))
    for updates in (
        {"execution_mode": "fixture"},
        {"execution_mode": "live", "operation": "video_edit"},
        {"operation": "audio"},
    ):
        reference_job.brief = reference_job.brief | updates
        assert refs.prepare(db, settings, reference_job) == []


def test_website_bytes_are_bounded_cached_and_not_claimed_to_be_real(db, settings, sheet):
    job = job_for(db, settings, sheet)
    stream = io.BytesIO()
    Image.new("RGB", (720, 720), "silver").save(stream, format="PNG")
    data = stream.getvalue()
    urls = []

    def respond(request):
        urls.append(str(request.url))
        return httpx.Response(200, content=data)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        records = refs.website_references(db, settings, job, "pergolas", client)
        assert refs.website_references(db, settings, job, "pergolas", client) == records
    assert len(urls) == 1 and urls[0].startswith("https://tudelu.com/system4/")
    assert records[0]["source"] == "website_image_creation_method_unverified"
    assert records[0]["checksum"] == hashlib.sha256(data).hexdigest()
    assert refs.read_reference(settings, records[0]) == data
    assert not records[0].get("asset_id")


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"location": "http://127.0.0.1/"}),
        httpx.Response(200, content=b"not an image"),
        httpx.Response(503),
    ],
)
def test_website_errors_do_not_follow_redirects_or_block_library_references(db, settings, sheet, response):
    job = job_for(db, settings, sheet)
    calls = []
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: calls.append(request) or response)
    ) as client:
        assert refs.website_references(db, settings, job, "pergolas", client) == []
    assert len(calls) == 1


def test_final_visual_review_gets_reference_images_separately(
    db, settings, reference_job, photo, monkeypatch
):
    from social.providers import VisionReview

    monkeypatch.setattr(refs, "select_reference", lambda s, r, c: choice(r[0]))
    refs.prepare(db, settings, reference_job)
    output = ingest(
        db,
        settings,
        storage(settings).read(photo.storage_key),
        "output.png",
        source="ai_edited",
        generator="test",
        prompt="test",
        rights="public",
        tags={"reference_plan": reference_job.brief["reference_plan"], "video_sequence": [
            {"reference_plan": reference_job.brief["reference_plan"] | {"creative_changes": "Wood Look at a cafe"}}
        ]},
    )
    seen = []

    def respond(self, endpoint, payload):
        seen.append(payload)
        return {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": VisionReview(
                                passed=True,
                                reasons=[],
                                garbled_text=False,
                                misused_logos=False,
                                unexpected_people=False,
                                visible_claims=[],
                            ).model_dump_json(),
                        }
                    ],
                }
            ],
        }

    monkeypatch.setattr(OpenAI, "call", respond)
    OpenAI(settings).vision([output], {})
    content = seen[0]["input"][0]["content"]
    context = json.loads(content[0]["text"].split("Context: ")[1])
    assert context["media_inputs"][0]["image_numbers"] == [1]
    assert context["reference_inputs"][0]["image_number"] == 2
    assert context["reference_inputs"][0]["creative_changes"] == "New courtyard and white finish."
    assert context["reference_inputs"][1]["image_number"] == 2
    assert context["reference_inputs"][1]["creative_changes"] == "Wood Look at a cafe"
    assert context["reference_inputs"][0]["role"] == "product_construction"
    assert len([p for p in content if p["type"] == "input_image"]) == 2
