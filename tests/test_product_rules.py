import json
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from social.app import create_app
from social.facts import Facts, load_facts, save_facts
from social.gate import evaluate
from social.idea_plan import catalog_for, plan_idea
from social.media_studio import CreativeBrief, process_jobs, queue_jobs
from social.models import FactSheet
from social.product_rules import VisualGuidance, constrain_prompt, enforce_job_prompt, profile_for
from social.prompting import PROMPT_LIMIT, write_prompt
from social.providers import Claude, OpenAI, Providers, VisionReview
from social.security import digest
from social.video_segments import segment_brief

GUIDE_PATH = Path("config/visual-guidance.builder.yaml")
SHEET_PATH = Path("config/facts.tudelu-2026-11-16-builder-v1.yaml")


@pytest.fixture
def guide():
    return VisualGuidance.model_validate(yaml.safe_load(GUIDE_PATH.read_text(encoding="utf-8")))


@pytest.fixture
def sheet(db, settings):
    load_facts(db, settings.facts_path)
    result = load_facts(db, SHEET_PATH)
    db.commit()
    return result


def job_for(db, settings, sheet, **updates):
    brief = CreativeBrief(
        **(
            {
                "campaign": "Builder product accuracy",
                "prompt": "A calm terrace in daylight",
                "platforms": ["instagram"],
                "facts_id": sheet.id,
                "rights": "not_public",
                "website": False,
                "operation": "image",
                "product": "pergolas",
                "create_posts": False,
            }
            | updates
        )
    )
    job = queue_jobs(db, settings, brief)[0]
    db.commit()
    return job


def test_source_records_and_saved_sheet_have_identical_guidance(guide):
    document = yaml.safe_load(SHEET_PATH.read_text(encoding="utf-8"))
    facts = Facts.model_validate(document)
    assert facts.visual_guidance == guide
    assert len(guide.products) == 7 and len(guide.sources) == 5
    assert {p.product_id for p in guide.products} <= {p["id"] for p in facts.products}
    assert "fixed" in profile_for(guide, "Pergola").prompt_cue
    assert "ceiling retraction" in profile_for(guide, "MUTE").prompt_cue


def test_legacy_facts_keep_hash_and_can_be_loaded_after_upgrade(db, settings):
    document = Facts.model_validate(yaml.safe_load(settings.facts_path.read_text())).model_dump(mode="json")
    document.pop("visual_guidance")
    old = FactSheet(id=digest(document), version=document["version"], document=document)
    db.add(old)
    db.commit()
    assert load_facts(db, settings.facts_path).id == old.id
    assert "visual_guidance" not in old.document


def test_new_sheet_persists_guidance_and_remains_immutable(db, sheet):
    db.expire_all()
    assert db.get(FactSheet, sheet.id).document["visual_guidance"]["version"]
    changed = deepcopy(sheet.document)
    changed["visual_guidance"]["products"][0]["prompt_cue"] = "Different geometry"
    with pytest.raises(ValueError, match="immutable"):
        save_facts(db, changed)


def test_rejects_missing_provenance_and_unknown_families(guide, sheet):
    bad = guide.model_dump()
    bad["products"][0]["rules"][0]["source_ids"] = ["invented"]
    with pytest.raises(ValidationError, match="recorded source"):
        VisualGuidance.model_validate(bad)
    bad_sheet = deepcopy(sheet.document)
    bad_sheet["visual_guidance"]["products"][0]["product_id"] = "greenhouses"
    with pytest.raises(ValidationError, match="products in the facts"):
        Facts.model_validate(bad_sheet)


@pytest.mark.parametrize("product", ["Ceiling Systems", "Greenhouses", "company"])
def test_unconfigured_families_cannot_generate(guide, product):
    with pytest.raises(ValueError, match="no approved Builder visual profile"):
        profile_for(guide, product)


def test_cues_survive_utf16_limit_and_retries_without_duplication(guide):
    profile = profile_for(guide, "pergolas")
    prompt = constrain_prompt("A terrace. " * 160 + "🌅" * 30, profile)
    assert len(prompt.encode("utf-16-le")) <= PROMPT_LIMIT * 2
    assert prompt.endswith(profile.prompt_cue)
    assert constrain_prompt(prompt, profile) == prompt


def test_writer_receives_rules_and_persists_exact_source_provenance(db, settings, sheet, monkeypatch):
    job = job_for(db, settings, sheet)
    job.brief = job.brief | {"execution_mode": "live"}
    observed = []

    def respond(self, instructions, context):
        observed.append((instructions, context))
        return {"system": "Pergolas", "prompt": "An aluminum pergola on an empty terrace in daylight."}

    monkeypatch.setattr(Claude, "call", respond)
    result = write_prompt(db, settings, job)
    instructions, context = observed[0]
    assert "override conflicting website" in instructions
    assert context["rules"]["visual_guidance"] == sheet.document["visual_guidance"]
    assert result["generation_prompt"].endswith(
        profile_for(Facts.model_validate(sheet.document).visual_guidance, "Pergolas").prompt_cue
    )
    assert result["prompt_writer"]["sources"]["visual_guidance"] == sheet.document["visual_guidance"]


@pytest.mark.parametrize(
    "answer",
    [
        {"system": "Greenhouses", "prompt": "A greenhouse"},
        {"system": "Pergolas", "prompt": "A roof", "problem": "Motorized louvers are not offered."},
    ],
)
def test_writer_rejects_unapproved_or_conflicting_product_before_runway(
    db, settings, sheet, monkeypatch, answer
):
    job = job_for(db, settings, sheet)
    job.brief = job.brief | {"execution_mode": "live"}
    monkeypatch.setattr(Claude, "call", lambda *args: answer)
    with pytest.raises(ValueError):
        write_prompt(db, settings, job)


def test_every_storyboard_clip_gets_its_own_cues_and_manual_retry_keeps_them(
    db, settings, sheet, house_logo, monkeypatch
):
    job = job_for(
        db,
        settings,
        sheet,
        operation="video",
        duration=12,
        product="company",
        shots=[
            {"setting": "courtyard", "system": "Pergolas"},
            {"setting": "conference room", "system": "Retractable Partition Walls"},
        ],
    )
    job.brief = job.brief | {"execution_mode": "live"}
    monkeypatch.setattr(
        Claude,
        "call",
        lambda *args: {
            "system": "Pergolas",
            "prompt": "First shot",
            "shots": [
                {"system": "Pergolas", "prompt": "A terrace. " * 100},
                {"system": "Retractable Partition Walls", "prompt": "A boardroom. " * 100},
            ],
        },
    )
    job.brief = job.brief | write_prompt(db, settings, job)
    first, second = job.brief["video_segments"]
    assert "fixed stationary louvers" in first["prompt"]
    assert "ceiling retraction" in second["prompt"]
    assert "louvers" not in second["prompt"]
    job.brief = job.brief | {"video_segments": [first | {"prompt": "A closer camera angle."}, second]}
    enforce_job_prompt(db, job)
    sent = segment_brief(job)["generation_prompt"]
    assert sent.startswith("A closer camera angle.") and "fixed stationary louvers" in sent
    assert len(sent.encode("utf-16-le")) <= PROMPT_LIMIT * 2


def test_single_image_fixture_runs_through_worker_with_rules(db, settings, sheet, monkeypatch):
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda *args: pytest.fail("No paid calls"))
    job = job_for(db, settings, sheet)
    for _ in range(3):
        process_jobs(db, settings)
    assert job.state == "complete", job.error
    assert "fixed stationary louvers" in job.brief["generation_prompt"]
    assert job.brief["visual_guidance_version"] == sheet.document["visual_guidance"]["version"]


def test_planner_uses_only_profiled_products_and_gets_full_rules(db, settings, sheet, monkeypatch):
    catalog = catalog_for(db, sheet.id, {"text": "Motorized roof example; Ceiling Systems"}, "company")
    assert len(catalog["product_names"]) == 7
    assert "Ceiling Systems" not in catalog["product_names"]
    seen = []

    def respond(self, instructions, context):
        seen.append(context)
        return {"title": "Terrace", "shots": [{"system": "Pergolas", "setting": "terrace"}]}

    monkeypatch.setattr(Claude, "call", respond)
    result = plan_idea(
        db, settings.model_copy(update={"ai_mode": "live"}), idea="A pergola image", facts_id=sheet.id
    )
    assert result.shots[0].system == "Pergolas"
    assert seen[0]["catalog"]["visual_guidance"] == sheet.document["visual_guidance"]


@pytest.mark.parametrize(
    "version,mismatches,passes",
    [
        ("current", [], True),
        ("", [], False),
        ("current", ["pergola.fixed_louvers: blades rotate"], False),
    ],
)
def test_product_review_fails_closed_even_if_model_says_pass(
    db, settings, post, assets, sheet, version, mismatches, passes
):
    post.facts_id = sheet.id
    assets[0].source, assets[0].generator, assets[0].prompt = "ai_generated", "test", "A pergola"
    expected = sheet.document["visual_guidance"]["version"]

    class Reviewer(Providers):
        def vision(self, supplied, context):
            assert context["facts"]["visual_guidance"]["version"] == expected
            return VisionReview(
                passed=True,
                reasons=["Reviewed"],
                garbled_text=False,
                misused_logos=False,
                unexpected_people=False,
                product_rules_version=expected if version == "current" else version,
                product_mismatches=mismatches,
            )

    results, _ = evaluate(db, settings, post, providers=Reviewer(settings))
    check = next(c for c in results if c.name == "vision_review")
    assert check.passed is passes
    assert check.details["product_rules_checked"] is passes


def test_openai_gets_rule_instructions_and_structured_mismatch_fields(settings, assets, sheet, monkeypatch):
    seen = []

    def respond(self, endpoint, payload):
        seen.append(payload)
        result = dict(
            passed=False,
            reasons=["Floating louvers"],
            garbled_text=False,
            misused_logos=False,
            unexpected_people=False,
            visible_claims=[],
            problem_seconds=[],
            product_rules_version=sheet.document["visual_guidance"]["version"],
            product_mismatches=["builder.connections: floating louver ends"],
        )
        return {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(result)}]}],
        }

    monkeypatch.setattr(OpenAI, "call", respond)
    result = OpenAI(settings).vision(assets, {"facts": sheet.document})
    assert result.product_mismatches
    payload = seen[0]
    assert "independently compare every depicted product" in payload["input"][0]["content"][0]["text"]
    assert "product_rules_version" in payload["text"]["format"]["schema"]["required"]


def test_fact_editor_shows_new_version_as_default_and_preserves_guidance(settings, sheet):
    with TestClient(create_app(settings)) as client:
        editor = client.get("/admin/facts")
        assert editor.status_code == 200 and "visual_guidance:" in editor.text
        assert "builder.connections" in editor.text
        create = client.get("/admin/create")
        assert create.status_code == 200
        assert f'value="{sheet.id}" selected' in create.text
