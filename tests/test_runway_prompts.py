"""Runway prompts are written from tudelu.com, approved rules, Public uploads and prior generations."""

import json

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_gate import checks
from test_runway_media import brief_for

from social.db import now
from social.media_studio import CreativeBrief, WebsiteText, process_jobs, queue_jobs, resume_job
from social.models import Asset, MediaJob
from social.pipeline import draft
from social.prompting import PROMPT_LIMIT, grounded, utf16_trim
from social.providers import Claude, OpenAI
from social.seed import seed_media
from social.storage import ingest

SITE = """<html><head><title>Tudelu - Architectural Systems</title>
<meta name="description" content="Motorized walls, canopies and pergolas.">
<script>var promo = "Greenhouses";</script></head><body>
<nav><a href="/pergolas">Pergolas</a><a href="/carports">Carports</a></nav>
<img alt="Fixed architectural pergola in an outdoor setting">
<p>Pergolas</p><p>Coming soon</p><p>Ceiling Systems</p></body></html>"""


def snapshot():
    parser = WebsiteText()
    parser.feed(SITE)
    return {
        "url": "https://tudelu.com/",
        "text": " | ".join(parser.meta + parser.lines),
        "image_descriptions": parser.images,
        "sha256": "f" * 64,
        "fetched_at": now().isoformat(),
    }


def test_website_text_keeps_catalog_order_and_ignores_scripts():
    data = snapshot()
    assert "Greenhouses" not in data["text"]
    assert "Coming soon | Ceiling Systems" in data["text"]
    assert data["image_descriptions"] == ["Fixed architectural pergola in an outdoor setting"]


@pytest.mark.parametrize(
    "name,ok",
    [
        ("Pergolas", True),
        ("pergola", True),
        ("Ceiling Systems", True),
        ("Gazebo", False),
        ("Car", False),
        ("s", False),
    ],
)
def test_depicted_system_must_be_named_in_the_sources(name, ok):
    assert grounded(name, " | ".join([snapshot()["text"], "Carports"]).casefold()) is ok


def test_prompt_trim_respects_runway_utf16_limit_on_a_sentence():
    text = "A pergola at dusk. " * 60 + "🌅" * 40
    trimmed = utf16_trim(text)
    assert len(trimmed.encode("utf-16-le")) <= PROMPT_LIMIT * 2 and trimmed.endswith(".")


class FakeRunway:
    def __init__(self, db, job):
        self.db, self.job, self.payloads = db, job, []

    def submit(self, endpoint, payload):
        # The written prompt is committed before the paid request.
        with Session(self.db.get_bind()) as other:
            assert other.get(MediaJob, self.job.id).brief["generation_prompt"] == payload["promptText"]
        self.payloads.append((endpoint, payload))
        return "task-1"


def summary_response(text):
    return {
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps({"summary": text, "visible_text": [], "limitations": []}),
                    }
                ],
            }
        ],
    }


@pytest.fixture
def sources(db, settings, monkeypatch):
    image = seed_media()[0]
    public = ingest(
        db, settings, image, "pergola.jpg", rights="public", owner="Tudelu", tags={"product": "pergola"}
    )
    private = ingest(db, settings, image + b"\0", "unreleased.jpg", rights="not_public")
    legacy = ingest(db, settings, image + b"\0\0", "older.jpg", rights="Owned by company")
    facts_id = brief_for(db, settings).facts_id
    earlier = ingest(
        db,
        settings,
        image + b"\0\0\0",
        "runway-old.png",
        source="ai_generated",
        generator="runway/gen4.5",
        prompt="Old prompt",
        rights="public",
    )
    held = draft(db, "instagram", "Old", [earlier.id], facts_id)
    held.status = "held"
    previous_job = MediaJob(
        batch_key="old:instagram", platform="instagram", brief={}, state="complete", post_id=held.id
    )
    db.add(previous_job)
    db.flush()
    earlier.tags = {"media_job_id": previous_job.id}
    earlier.media = earlier.media | {"visual_summary": {"summary": "A glass greenhouse on a lawn."}}
    db.commit()
    monkeypatch.setattr(
        OpenAI,
        "call",
        lambda *args, **kwargs: summary_response("A slatted aluminium pergola over a stone terrace."),
    )
    return {"public": public, "private": private, "legacy": legacy, "earlier": earlier}


def live_job(db, settings, **overrides):
    job = queue_jobs(
        db, settings, brief_for(db, settings, operation="video", **overrides), snapshot=snapshot()
    )[0]
    job.brief = job.brief | {"execution_mode": "live"}
    db.commit()
    settings.ai_mode, settings.runway_api_key = "live", SecretStr("mock")
    return job


def test_live_prompt_uses_site_rules_public_uploads_and_previous_generations(
    db, settings, sources, monkeypatch
):
    seen = []

    def write(self, system, context):
        seen.append((system, context))
        return {
            "system": "pergolas",
            "prompt": "A slatted aluminium pergola over a stone terrace at dusk; slow dolly forward.",
            "sources_used": ["website", f"uploaded:{sources['public'].id}"],
        }

    monkeypatch.setattr(Claude, "call", write)
    job = live_job(db, settings)
    assert job.brief["generation_prompt"] == ""
    runway = FakeRunway(db, job)
    process_jobs(db, settings, runway)
    assert job.state == "running", job.error

    system, context = seen[0]
    assert "do not support negative prompts" in system
    assert context["admin_idea"] == "Warm architectural textures"
    assert "Pergolas" in context["website"]["text"] and context["website"]["image_descriptions"]
    assert context["rules"]["facts_version"] and context["visual_rules"]
    assert [u["asset_id"] for u in context["uploaded_media"]] == [sources["public"].id]
    assert "slatted aluminium pergola" in context["uploaded_media"][0]["summary"]
    assert context["previous_generations"][0]["post_status"] == "held"
    assert context["orientation"] == "portrait" and context["direction"]

    endpoint, payload = runway.payloads[0]
    assert endpoint == "text_to_video"
    assert payload["promptText"] == job.brief["generation_prompt"]
    assert job.brief["depicted_system"] == "pergolas"
    recorded = job.brief["prompt_writer"]["sources"]
    assert recorded["uploaded_asset_ids"] == [sources["public"].id]
    assert recorded["previous_asset_ids"] == [sources["earlier"].id]
    assert recorded["website"]["sha256"] == "f" * 64
    # The upload was summarized once and kept for later prompts and captions.
    db.refresh(sources["public"])
    assert sources["public"].media["visual_summary"]["summary"].startswith("A slatted")


@pytest.mark.parametrize(
    "answer,message",
    [
        ({"system": "Greenhouses", "prompt": "A greenhouse."}, "not named on tudelu.com"),
        (
            {"system": "", "prompt": "", "problem": "Tudelu does not sell greenhouses."},
            "does not sell greenhouses",
        ),
    ],
)
def test_ungrounded_prompt_fails_before_runway_and_can_resume(
    db, settings, sources, monkeypatch, answer, message
):
    answers = [answer, {"system": "Pergolas", "prompt": "A pergola at noon."}]
    monkeypatch.setattr(Claude, "call", lambda *args: answers.pop(0))
    job = live_job(db, settings)
    runway = FakeRunway(db, job)
    process_jobs(db, settings, runway)
    assert job.state == "failed" and message in job.error
    assert not runway.payloads and "prompt_writer" not in job.brief
    resume_job(job)
    db.commit()
    process_jobs(db, settings, runway)
    assert job.state == "running" and runway.payloads[0][1]["promptText"] == "A pergola at noon."


def test_fixture_prompt_is_recorded_and_older_queued_jobs_are_rewritten(db, settings):
    job = queue_jobs(db, settings, brief_for(db, settings, product="Pergolas"))[0]
    job.brief = job.brief | {"generation_prompt": "Old suffix prompt with 90 characters of website text"}
    db.commit()
    for _ in range(3):
        process_jobs(db, settings)
    assert job.state == "complete", job.error
    assert job.brief["prompt_writer"]["writer"] == "fixture/deterministic"
    assert job.brief["generation_prompt"].startswith("Warm architectural textures. Subject: Tudelu Pergolas.")
    original = db.get(Asset, job.source_asset_ids[0])
    assert original.prompt == job.brief["generation_prompt"]
    assert original.tags["creative_brief"]["system"] == "Pergolas"
    assert (original.rights, original.owner) == ("public", "Tudelu")


def test_rights_choice_owner_and_legacy_briefs(db, settings, assets):
    with pytest.raises(ValidationError, match="Public or Not public"):
        CreativeBrief.model_validate(brief_for(db, settings).model_dump() | {"rights": "Company permitted"})
    with pytest.raises(ValidationError, match="200 characters"):
        brief_for(db, settings, owner="x" * 201)
    stored = brief_for(db, settings).model_dump() | {"rights": "Company permitted"}
    assert CreativeBrief.model_validate(stored, context={"stored": True}).rights == "Company permitted"

    assets[0].rights = "not_public"
    db.commit()
    with pytest.raises(ValueError, match="marked Not public"):
        queue_jobs(db, settings, brief_for(db, settings, reference_ids=[assets[0].id]))
    job = queue_jobs(
        db, settings, brief_for(db, settings, rights="not_public", reference_ids=[assets[0].id])
    )[0]
    db.commit()
    for _ in range(3):
        process_jobs(db, settings)
    assert job.state == "complete", job.error
    assert db.get(Asset, job.output_asset_id).rights == "not_public"


def test_not_public_media_supports_dry_runs_but_never_live_publication(db, settings, post, assets):
    assets[0].rights = "not_public"
    assert checks(db, settings, post)["rights"].passed
    live = checks(db, settings, post, live=True)["rights"]
    assert not live.passed and live.details == {"not_public_for_live": [assets[0].id]}
    assets[0].rights = "public"
    assert checks(db, settings, post, live=True)["rights"].passed


def test_reviewers_do_not_require_ai_concept_labels(db, settings, monkeypatch):
    prompts = []
    monkeypatch.setattr(
        Claude,
        "call",
        lambda self, system, context: (
            prompts.append(system) or {"score": 1, "reasons": [], "part_checks": []}
        ),
    )
    Claude(settings).review({})
    assert "Do not require the caption or media to say that media is AI-generated, a concept" in prompts[0]

    sent = []

    def vision_call(self, endpoint, payload):
        sent.append(payload["input"][0]["content"][0]["text"])
        return {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                {
                                    "passed": True,
                                    "reasons": [],
                                    "garbled_text": False,
                                    "misused_logos": False,
                                    "unexpected_people": False,
                                    "visible_claims": [],
                                }
                            ),
                        }
                    ],
                }
            ],
        }

    monkeypatch.setattr(OpenAI, "call", vision_call)
    image = ingest(db, settings, seed_media()[0], "concept.jpg", rights="public")
    assert OpenAI(settings).vision([image], {}).passed
    assert "never fail media for lacking an AI-generated, concept" in sent[0]


def test_owner_is_saved_with_normalized_spacing(db, settings):
    asset = ingest(db, settings, seed_media()[0], "owned.jpg", rights="public", owner="  Tudelu   Inc ")
    db.commit()
    assert db.scalar(select(Asset.owner).where(Asset.id == asset.id)) == "Tudelu Inc"
