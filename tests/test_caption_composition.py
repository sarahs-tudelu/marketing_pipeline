import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from pydantic import SecretStr

from social.config import profiles
from social.facts import Facts, caption_fact_ids, render, validate_parts
from social.gate import run_gate
from social.models import FactSheet
from social.pipeline import draft, generate
from social.providers import Claude, Providers, Review


def render_parts(db, settings, post, parts):
    facts = Facts.model_validate(db.get(FactSheet, post.facts_id).document)
    post.caption = render(parts, profiles(settings)[post.platform], facts, post.platform, settings)
    post.parts, post.fact_ids = parts, caption_fact_ids(parts)


def composed_parts():
    return [
        {"kind": "copy", "text": "Curious? Take a closer look!", "fact_ids": []},
        {"kind": "claim", "text": "Meet Tudelu.", "fact_ids": ["company.name"]},
    ]


def complete_review(parts):
    return {
        "score": 1,
        "reasons": ["Supported by approved facts"],
        "unsupported_claims": [],
        "part_checks": [
            {"part_index": i, "fact_ids": part["fact_ids"], "supported": True, "reasons": ["Supported"]}
            for i, part in enumerate(parts)
        ],
    }


def test_composed_caption_and_independent_review_with_mocked_claude(db, settings, post):
    calls = []
    parts = composed_parts()

    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        response = {"parts": parts} if len(calls) == 1 else complete_review(parts)
        return httpx.Response(
            200, json={"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(response)}]}
        )

    claude = Claude(
        settings.model_copy(update={"anthropic_api_key": SecretStr("fixture")}),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    render_parts(db, settings, post, claude.generate({"platform": "linkedin"}))

    class Independent(Providers):
        def review(self, context):
            return claude.review(context)

    results = run_gate(db, settings, post, providers=Independent(settings))
    assert post.status == "approved"
    assert all(check.passed for check in results)
    assert post.caption.startswith("Curious? Take a closer look!\n\nMeet Tudelu.")
    assert "From the Tudelu team:" not in post.caption
    assert post.caption.endswith("#Tudelu")
    assert post.fact_ids == ["company.name"]
    assert len(calls) == 2
    assert all(len(call["messages"]) == 1 for call in calls)
    review_context = json.loads(calls[1]["messages"][0]["content"])
    assert review_context["caption_parts"] == parts


@pytest.mark.parametrize("failure", ["missing", "duplicate", "wrong_citations", "unsupported", "no_reason"])
def test_high_score_cannot_bypass_incomplete_or_failed_part_review(db, settings, post, failure):
    parts = composed_parts()
    render_parts(db, settings, post, parts)
    result = complete_review(parts)
    if failure == "missing":
        result["part_checks"] = []
    elif failure == "duplicate":
        result["part_checks"][1] = result["part_checks"][0]
    elif failure == "wrong_citations":
        result["part_checks"][1]["fact_ids"] = ["demo.context"]
    elif failure == "unsupported":
        result["part_checks"][0]["supported"] = False
    else:
        result["part_checks"][0]["reasons"] = []
    provider = Providers(settings)
    provider.review = Mock(return_value=Review.model_validate(result))
    checks = run_gate(db, settings, post, providers=provider)
    assert post.status == "held"
    assert not next(c for c in checks if c.name == "model_review").passed


@pytest.mark.parametrize(
    "part",
    [
        {"kind": "copy", "text": "Designed for effortless everyday comfort!", "fact_ids": []},
        {"kind": "claim", "text": "Tudelu makes your home more comfortable.", "fact_ids": ["company.name"]},
    ],
)
def test_fixture_review_cannot_approve_invented_claims_or_uncited_copy(db, settings, post, part):
    render_parts(db, settings, post, [*post.parts, part])
    results = run_gate(db, settings, post)
    assert post.status == "held"
    assert not next(c for c in results if c.name == "model_review").passed


@pytest.mark.parametrize(
    "bad_part",
    [
        {"kind": "claim", "text": "Tudelu saves 50%.", "fact_ids": ["company.name"]},
        {"kind": "copy", "text": "Tudelu", "fact_ids": ["company.name"]},
        {"kind": "claim", "text": "Tudelu", "fact_ids": []},
        {"kind": "claim", "text": "Tudelu", "fact_ids": ["unknown"]},
        {"kind": "claim", "text": "Tudelu", "fact_ids": "company.name"},
    ],
)
def test_composed_caption_rejects_invalid_citations_and_new_quantities(db, settings, post, bad_part):
    facts = Facts.model_validate(db.get(FactSheet, post.facts_id).document)
    with pytest.raises(ValueError):
        validate_parts([bad_part], facts)


def test_prices_keep_exact_approved_wording(db, settings, post):
    facts = Facts.model_validate(db.get(FactSheet, post.facts_id).document)
    facts = Facts.model_validate(
        facts.model_dump()
        | {
            "facts": [
                {
                    "id": "price",
                    "product": "demo",
                    "kind": "price",
                    "text": "Price: USD 100.",
                    "amount": "100",
                    "currency": "USD",
                    "valid_from": "2026-01-01",
                    "valid_until": "2027-01-01",
                }
            ]
        }
    )
    part = {"kind": "claim", "fact_ids": ["price"], "text": "Only USD 100 for a limited time!"}
    with pytest.raises(ValueError, match="exact approved wording"):
        validate_parts([part], facts)
    part["text"] = "Price: USD 100."
    validate_parts([part], facts)


def test_legacy_saved_caption_rendering_remains_unchanged(db, settings, post):
    render_parts(db, settings, post, [{"fact_id": "company.name", "text": "Tudelu."}])
    assert post.caption == "From the Tudelu team:\n\nTudelu.\n\nInternal demo.\n\n#Tudelu"
    run_gate(db, settings, post)
    assert post.status == "approved"


def test_generation_uses_different_style_briefs_and_recent_captions(db, settings, post, assets, monkeypatch):
    contexts = []

    class Capture(Providers):
        def caption(self, context):
            contexts.append(context)
            return super().caption(context)

    provider = Capture(settings)
    for index in range(5):
        selected = draft(
            db, "linkedin", f"Variation {index}", [a.id for a in assets], post.facts_id, product="demo"
        )
        monkeypatch.setattr(
            "social.pipeline.hashlib",
            SimpleNamespace(sha256=Mock(return_value=SimpleNamespace(hexdigest=lambda: f"{index * 31:08x}"))),
        )
        generate(db, settings, selected, providers=provider)
        assert selected.status == "generated"
    assert all(post.caption in context["recent_captions"] for context in contexts)
    for key in ("opening", "tone", "angle", "punctuation", "rhythm"):
        assert len({context["style_brief"][key] for context in contexts}) > 1
