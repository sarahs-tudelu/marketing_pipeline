import json
from copy import deepcopy

import pytest
from test_gate import change_facts, checks

from social.facts import render
from social.providers import Claude, OpenAI, PartCheck, Providers, Review, VisionReview, vision_context

CAPTION = (
    "Before you finalize a project brief, clarify site access, current drawings, "
    "installation responsibilities, and the delivery sequence you want."
)
FACT = (
    "Clarify site access, current drawings, installation responsibilities, and the requested "
    "delivery sequence before finalizing a project brief."
)


def sample_context(asset_id):
    return {
        "caption": CAPTION,
        "caption_parts": [{"kind": "claim", "text": CAPTION, "fact_ids": ["E08"]}],
        "fact_ids": ["E08"],
        "facts": {
            "version": "older-version",
            "brand_voice": "caption-only-brand-directions",
            "required_disclaimers": ["caption-only-disclaimer"],
            "facts": [{"id": "E08", "kind": "claim", "text": FACT}],
            "products": [{"id": "canopies", "name": "Architectural Canopies"}],
        },
        "renderer_context": {"hashtags": ["caption-only-hashtag"]},
        "audio_review": {"passed": False, "details": {"reasons": ["audio-only-issue"]}},
        "assets": [
            {
                "asset_id": asset_id,
                "source": "ai_generated",
                "product_family": "canopies",
                "tags": {
                    "end_card": {"seconds": 3},
                    "creative_brief": {"prompt": "Visitors coming and going", "system": "Canopies"},
                    "caption": "caption-tag-must-not-leak",
                },
                "media": {"visual_summary": {"summary": "old-caption-interpretation"}},
                "source_or_creation_record": {"prompt": "Two adults enter beneath the canopy."},
            }
        ],
    }


def response(review):
    return {
        "status": "completed",
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(review.model_dump())}]}
        ],
    }


def test_visual_context_excludes_caption_and_other_check_policies_but_keeps_product_evidence():
    context = sample_context("asset")
    original = deepcopy(context)
    filtered = vision_context(context)
    serialized = json.dumps(filtered)
    assert CAPTION not in serialized
    assert FACT in serialized
    for excluded in ["caption-only", "audio-only", "caption-tag", "old-caption-interpretation"]:
        assert excluded not in serialized
    assert filtered["review_scope"] == "media_only"
    assert filtered["assets"][0]["tags"]["end_card"] == {"seconds": 3}
    assert "Two adults" in filtered["assets"][0]["scene_intent"]["generation_prompt"]
    assert context == original


def test_actual_vision_request_cannot_apply_visible_text_rule_to_external_e08_caption(
    settings, assets, monkeypatch
):
    context = sample_context(assets[0].id)
    sent = []

    def call(self, endpoint, payload):
        sent.append(payload)
        prompt, data = payload["input"][0]["content"][0]["text"].split("Context: ", 1)
        data = json.loads(data)
        assert CAPTION not in json.dumps(data)
        assert "Never evaluate caption" in prompt
        assert "only literal readable on-image wording" in prompt
        assert "Missing guidance alone is not a rejection" in prompt
        assert data["facts"]["facts"][0]["text"] == FACT
        assert len(payload["input"][0]["content"]) == 2
        return response(
            VisionReview(
                passed=True,
                reasons=["No observed visual defects."],
                garbled_text=False,
                misused_logos=False,
                unexpected_people=False,
            )
        )

    monkeypatch.setattr(OpenAI, "call", call)
    assert OpenAI(settings).vision(assets, context).passed
    assert len(sent) == 1


@pytest.mark.parametrize("mismatches,passed", [([], True), (["W01: visible floor track"], False)])
def test_geometry_issue_still_holds_even_with_false_text_logo_and_people_flags(
    db,
    settings,
    post,
    assets,
    mismatches,
    passed,
):
    assets[0].source, assets[0].generator, assets[0].prompt = "ai_generated", "test", "A concept"

    class Reviewer(Providers):
        def vision(self, supplied, context):
            return VisionReview(
                passed=True,
                reasons=["Observed geometry"],
                garbled_text=False,
                misused_logos=False,
                unexpected_people=False,
                product_mismatches=mismatches,
            )

    result = checks(db, settings, post, providers=Reviewer(settings))["vision_review"]
    assert result.passed is passed
    assert result.details["review_scope"] == "media_only"


def test_valid_e08_paraphrase_passes_separate_caption_review(db, settings, post, assets):
    from social.config import profiles

    facts = change_facts(db, post, facts=[{"id": "E08", "kind": "claim", "product": "company", "text": FACT}])
    post.product = "company"
    post.parts = [{"kind": "claim", "text": CAPTION, "fact_ids": ["E08"]}]
    post.fact_ids = ["E08"]
    post.caption = render(post.parts, profiles(settings)[post.platform], facts, post.platform, settings)
    assets[0].source, assets[0].generator, assets[0].prompt = "ai_generated", "test", "A concept"

    class Reviewer(Providers):
        def review(self, context):
            assert CAPTION in context["caption"]
            return Review(
                score=1,
                reasons=["Paraphrase preserves E08 meaning"],
                part_checks=[
                    PartCheck(
                        part_index=0, fact_ids=["E08"], supported=True, reasons=["Same planning guidance"]
                    )
                ],
            )

    result = checks(db, settings, post, providers=Reviewer(settings))
    assert result["grounding"].passed
    assert result["model_review"].passed
    assert result["vision_review"].passed


def test_caption_reviewer_scores_its_own_findings_instead_of_inheriting_visual_failure(settings, monkeypatch):
    seen = []

    def call(self, instructions, context):
        seen.append(instructions)
        assert context["visual_review"]["passed"] is False
        return {"score": 1, "reasons": ["Caption is supported"], "unsupported_claims": [], "part_checks": []}

    monkeypatch.setattr(Claude, "call", call)
    review = Claude(settings).review({"visual_review": {"passed": False}, "caption": CAPTION})
    assert review.score == 1
    assert "do not lower its score solely because another check failed" in seen[0]
    assert "application independently blocks failed" in seen[0]
