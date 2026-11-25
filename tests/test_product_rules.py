import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from social.app import create_app
from social.facts import Facts, active, load_facts, save_facts
from social.gate import evaluate
from social.idea_plan import catalog_for, plan_idea
from social.media_studio import CreativeBrief, process_jobs, queue_jobs
from social.models import FactSheet
from social.product_rules import (
    CANOPY_FORM_GUIDANCE,
    CANOPY_FORM_RULE,
    FRAMING_RULE,
    HANGER_ROD_CUE,
    HANGER_ROD_RULE,
    PANEL_SURFACE_CUE,
    PANEL_SURFACE_RULE,
    PRODUCT_CONSTRAINTS_VERSION,
    SCOPE_REASONING,
    SCOPE_RULE,
    SUPPORT_GUIDANCE,
    SUPPORT_RULE,
    WEATHER_GUIDANCE,
    WEATHER_RULE,
    VisualGuidance,
    constrain_prompt,
    context_for,
    current_constraints,
    enforce_job_prompt,
    profile_for,
)
from social.prompting import PROMPT_LIMIT, gather_sources, write_prompt
from social.providers import Claude, OpenAI, Providers, VisionReview, vision_context
from social.rendering_guidelines import DOORWAY_GUIDANCE, MOTION_GUIDANCE
from social.security import digest
from social.video_segments import segment_brief

GUIDE_PATH = Path("social/resources/visual-guidance.builder.yaml")
SHEET_PATH = Path("config/facts.tudelu-2026-11-16-builder-v1.yaml")
RENEWED_PATH = Path("config/facts.tudelu-2026-11-17-builder-v2.yaml")


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
    assert prompt.endswith(profile.generation_cue)
    assert constrain_prompt(prompt, profile) == prompt


@pytest.mark.parametrize(("family", "design"), [
    ("pergolas", "A hexagonal pergola with three branching aluminum posts."),
    ("canopies", "A long curved canopy with angled supports along the walkway."),
    ("pergolas", "A rectangular pergola with four straight posts."),
])
def test_saved_builder_cue_preserves_the_requested_design_on_retry(guide, family, design):
    profile = profile_for(guide, family)
    saved = f"{design} {profile.prompt_cue}"
    if family == "canopies":
        saved += " " + HANGER_ROD_CUE
    submitted = constrain_prompt(saved, profile)
    assert submitted.startswith(design)
    assert profile.prompt_cue not in submitted
    assert submitted.count(profile.generation_cue) == 1
    assert constrain_prompt(submitted, profile) == submitted


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
    # The Builder rules are read as Tudelu's offerings, customized the way a product brief is.
    assert SCOPE_REASONING in instructions and WEATHER_GUIDANCE in instructions
    assert "Return problem only for a hard limit" in instructions
    # Video prompts say what happens in the clip and send people only through real openings.
    assert MOTION_GUIDANCE in instructions and DOORWAY_GUIDANCE in instructions
    assert "completely still" not in instructions and "locked-off" not in instructions
    assert context["rules"]["visual_guidance"] == context_for(sheet.document["visual_guidance"])
    assert "Use real panel seams" not in json.dumps(context["rules"]["visual_guidance"])
    assert context["rules"]["product_constraints"] == current_constraints()
    assert result["generation_prompt"].endswith(
        profile_for(Facts.model_validate(sheet.document).visual_guidance, "Pergolas").generation_cue
    )
    assert result["prompt_writer"]["sources"]["visual_guidance"] == sheet.document["visual_guidance"]
    assert result["prompt_writer"]["sources"]["product_constraints"] == current_constraints()


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
    assert catalog["product_constraints"] == current_constraints()
    seen = []

    def respond(self, instructions, context):
        assert "four posts" not in instructions.casefold()
        seen.append(context)
        return {"title": "Terrace", "shots": [{"system": "Pergolas", "setting": "terrace"}]}

    monkeypatch.setattr(Claude, "call", respond)
    result = plan_idea(
        db, settings.model_copy(update={"ai_mode": "live"}), idea="A pergola image", facts_id=sheet.id
    )
    assert result.shots[0].system == "Pergolas"
    assert seen[0]["catalog"]["visual_guidance"] == context_for(sheet.document["visual_guidance"])


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
                product_constraints_version=PRODUCT_CONSTRAINTS_VERSION,
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
            product_constraints_version=PRODUCT_CONSTRAINTS_VERSION,
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
    assert "product_constraints_version" in payload["text"]["format"]["schema"]["required"]
    instructions, supplied = payload["input"][0]["content"][0]["text"].split("Context: ", 1)
    assert "Rods or rod-like braces underneath" in instructions
    assert "posts stay plumb" not in instructions
    assert json.loads(supplied)["product_constraints"] == current_constraints()
    assert "Use real panel seams" not in supplied


@pytest.mark.parametrize("system", ["Canopy", "Sunshades", "Walkways", "Carports", "Drive-Through Systems"])
@pytest.mark.parametrize("legacy", [True, False])
def test_hanger_rod_orientation_survives_prompt_limits_and_retries(guide, system, legacy):
    profile = None if legacy else profile_for(guide, system)
    prompt = constrain_prompt("An entrance in daylight. " * 100 + "\U0001f305" * 80, profile, system)
    assert len(prompt.encode("utf-16-le")) <= PROMPT_LIMIT * 2
    assert prompt.endswith(HANGER_ROD_CUE)
    assert prompt.count(HANGER_ROD_CUE) == 1
    assert constrain_prompt(prompt, profile, system) == prompt
    if profile:
        assert profile.generation_cue in prompt


@pytest.mark.parametrize("operation", ["image", "video", "video_edit"])
def test_old_saved_prompts_receive_owner_correction_before_submission(db, settings, operation):
    sheet = load_facts(db, settings.facts_path)
    original = json.dumps(sheet.document, sort_keys=True)
    job = job_for(db, settings, sheet, product="canopies")
    job.brief = job.brief | {"operation": operation, "generation_prompt": "A close view of the canopy."}
    if operation != "image":
        job.brief = job.brief | {
            "video_segments": [{"index": 0, "duration": 5, "seed": 1, "prompt": "A new camera angle."}]
        }
    enforce_job_prompt(db, job)
    sent = segment_brief(job)["generation_prompt"]
    assert sent.endswith(HANGER_ROD_CUE)
    assert job.brief["product_constraints"] == current_constraints()
    enforce_job_prompt(db, job)
    assert segment_brief(job)["generation_prompt"] == sent
    assert json.dumps(db.get(FactSheet, sheet.id).document, sort_keys=True) == original


def test_each_canopy_clip_retains_correction_after_manual_replacement(db, settings, sheet, house_logo, monkeypatch):
    job = job_for(db, settings, sheet, operation="video", duration=12, product="company", shots=[
        {"setting": "entry", "system": "Canopies"},
        {"setting": "garden", "system": "Sunshades"},
    ])
    job.brief = job.brief | {"execution_mode": "live"}
    monkeypatch.setattr(Claude, "call", lambda *args: {
        "system": "Canopies", "prompt": "An entrance.",
        "shots": [{"system": "Canopies", "prompt": "An entrance. " * 120},
                  {"system": "Sunshades", "prompt": "A garden. " * 120}],
    })
    job.brief = job.brief | write_prompt(db, settings, job)
    first, second = job.brief["video_segments"]
    assert all(s["prompt"].endswith(HANGER_ROD_CUE) for s in (first, second))
    job.brief = job.brief | {"video_segments": [first | {"output_asset_id": "already-generated"},
                                               second | {"prompt": "A closer view of the supports."}]}
    enforce_job_prompt(db, job)
    assert segment_brief(job)["generation_prompt"].endswith(HANGER_ROD_CUE)
    assert job.brief["video_segments"][0] == first | {"output_asset_id": "already-generated"}


def test_owner_constraint_problem_stops_legacy_writer_even_with_a_named_system(db, settings, monkeypatch):
    sheet = load_facts(db, settings.facts_path)
    job = job_for(db, settings, sheet, product="canopies")
    job.brief = job.brief | {"execution_mode": "live"}

    def respond(self, instructions, context):
        assert context["rules"]["product_constraints"] == current_constraints()
        return {"system": "Canopies", "prompt": "A canopy.",
                "problem": "The supplied reference has bottom-supported rods that cannot be preserved."}

    monkeypatch.setattr(Claude, "call", respond)
    with pytest.raises(ValueError, match="bottom-supported"):
        write_prompt(db, settings, job)


@pytest.mark.parametrize("version,mismatch,passes", [
    (PRODUCT_CONSTRAINTS_VERSION, "", True),
    (PRODUCT_CONSTRAINTS_VERSION, HANGER_ROD_RULE + ": diagonal rods below the roof at 2 seconds", False),
    (PRODUCT_CONSTRAINTS_VERSION, PANEL_SURFACE_RULE + ": joint lines divide the finished canopy underside", False),
    ("", "", False),
    ("older-owner-correction", "", False),
])
def test_old_fact_versions_cannot_bypass_current_canopy_review(db, settings, post, assets, version, mismatch, passes):
    assets[0].source, assets[0].generator, assets[0].prompt = "ai_generated", "test", "A canopy"

    class Reviewer(Providers):
        def vision(self, supplied, context):
            assert context["product_constraints"] == current_constraints()
            return VisionReview(
                passed=True, reasons=["Reviewed pixels"], garbled_text=False, misused_logos=False,
                unexpected_people=False, product_constraints_version=version,
                product_mismatches=[mismatch] if mismatch else [], problem_seconds=[2] if mismatch else [],
            )

    results, _ = evaluate(db, settings, post, providers=Reviewer(settings))
    check = next(c for c in results if c.name == "vision_review")
    assert check.passed is passes
    assert check.details["product_constraints"] == current_constraints()
    if mismatch:
        assert mismatch in check.details["reasons"]
        assert check.details["problem_seconds"] == [2]


def test_fact_editor_shows_new_version_as_default_and_preserves_guidance(settings, sheet):
    with TestClient(create_app(settings)) as client:
        editor = client.get("/admin/facts")
        assert editor.status_code == 200 and "visual_guidance:" in editor.text
        assert "builder.connections" in editor.text
        create = client.get("/admin/create")
        assert create.status_code == 200
        assert f'value="{sheet.id}" selected' in create.text


def test_owner_scope_allows_customization_and_keeps_hard_limits():
    rules = {r["id"]: r["instruction"] for r in current_constraints()["rules"]}
    assert list(rules) == [SCOPE_RULE, WEATHER_RULE, FRAMING_RULE, CANOPY_FORM_RULE, SUPPORT_RULE,
                           PANEL_SURFACE_RULE, HANGER_ROD_RULE]
    assert "hanger rods below a canopy" in rules[SCOPE_RULE]
    assert "'only when selected' describe a starting point" in rules[SCOPE_RULE]
    assert "A pergola can take an insulated solid panel or glass roof" in rules[WEATHER_RULE]
    assert "Partial overlap is not a defect" in rules[FRAMING_RULE]
    assert rules[CANOPY_FORM_RULE] == CANOPY_FORM_GUIDANCE
    assert [s["recorded_on"] for s in current_constraints()["sources"]] == [
        "2026-11-16", "2026-11-17", "2026-11-24", "2026-11-24", "2026-11-25"
    ]


def test_supports_are_judged_by_realism_not_an_exact_post_count():
    rules = {r["id"]: r["instruction"] for r in current_constraints()["rules"]}
    assert rules[SUPPORT_RULE] == SUPPORT_GUIDANCE
    assert "Never reject or regenerate a scene only because a count or spacing differs" in SUPPORT_GUIDANCE
    # Realism and Tudelu's designs still decide; only an admin's own words make a count binding.
    assert "a floating or ungrounded post" in SUPPORT_GUIDANCE and "supports Tudelu doesn't make" in SUPPORT_GUIDANCE
    assert "a count that changes between frames of one clip" in SUPPORT_GUIDANCE
    assert "the idea or an operator edit" in SUPPORT_GUIDANCE
    # Every planner and reviewer reads the scope paragraph, which points to the rule.
    assert SUPPORT_RULE in SCOPE_REASONING and SUPPORT_RULE in CANOPY_FORM_GUIDANCE
    assert "post count, spacing and shape" not in CANOPY_FORM_GUIDANCE


@pytest.mark.parametrize(("system", "scene", "panels"), [
    ("Canopies", "A hexagonal canopy with branching columns.", True),
    ("Pergolas", "An insulated panel roof over the terrace.", True),
    ("Pergolas", "An open roof of spaced fixed louvers.", False),
    ("Pergolas", "Open fixed louvers without roof panels.", False),
    ("Partition Walls", "A fabric partition in the meeting room.", False),
])
def test_panel_finish_reaches_generation_for_the_selected_roof(guide, system, scene, panels):
    for profile in (profile_for(guide, system), None):
        prompt = constrain_prompt(scene, profile, system)
        assert (PANEL_SURFACE_CUE in prompt) is panels
        assert constrain_prompt(prompt, profile, system) == prompt


def test_image_side_models_get_product_names_not_caption_limits(db, settings, sheet):
    limits = ["rainproof seating", "claim_provenance", "policy_metadata", "AI-generated concept"]
    catalog = catalog_for(db, sheet.id, {}, "company")
    writer = gather_sources(db, settings, job_for(db, settings, sheet))[0]
    reviewer = vision_context({"facts": sheet.document})
    for products in (catalog["products"], writer["rules"]["products"], reviewer["facts"]["products"]):
        assert {"id": "pergolas", "name": "Pergolas", "status": "presented_on_site"} in products
        assert not any(text in json.dumps(products) for text in limits)
    # The approved fact itself stays available, and the caption review still gets every limit.
    assert any("do not form a waterproof roof" in f["text"] for f in reviewer["facts"]["facts"])
    assert "rainproof seating" in json.dumps(sheet.document["products"])


def test_renewed_sheet_keeps_the_guidance_and_extends_only_verified_facts(db, settings):
    original = yaml.safe_load(SHEET_PATH.read_text(encoding="utf-8"))
    renewed = yaml.safe_load(RENEWED_PATH.read_text(encoding="utf-8"))
    assert renewed["visual_guidance"] == original["visual_guidance"]
    facts = Facts.model_validate(renewed)
    assert sorted(f.id for f in facts.facts if not active(f, datetime(2026, 12, 10))) == [
        "R06", "R07", *sorted(f.id for f in facts.facts if f.id.startswith("availability."))
    ]
    assert all(active(f, datetime(2026, 11, 20)) for f in facts.facts)
    assert "AI-generated concept" not in RENEWED_PATH.read_text(encoding="utf-8")
    load_facts(db, settings.facts_path)
    assert load_facts(db, RENEWED_PATH).version == "tudelu-2026-11-17-builder-v2"
