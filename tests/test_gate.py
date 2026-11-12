from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from social.db import now
from social.facts import Facts, content_digest, render, save_facts
from social.gate import check_urls, evaluate, media_errors, run_gate, weighted_length
from social.models import CheckResult, FactSheet, Post
from social.providers import Providers, Review, VisionReview
from social.storage import storage


def checks(db, settings, post, **kwargs):
    return {c.name: c for c in evaluate(db, settings, post, **kwargs)[0]}


def change_facts(db, post, **updates):
    document = dict(db.get(FactSheet, post.facts_id).document)
    document.update(updates)
    document["version"] += "-changed"
    post.facts_id = save_facts(db, document).id
    return Facts.model_validate(document)


def test_complete_gate_audits_all_checks(db, settings, post):
    result = run_gate(db, settings, post)
    assert post.status == "approved"
    assert len(result) == 18
    assert all(c.passed for c in result)
    assert post.approved_digest
    assert len(db.scalars(select(CheckResult)).all()) == 18


@pytest.mark.parametrize("live", [False, True])
def test_review_context_reports_execution_mode_and_actual_asset_provenance(db, settings, post, assets, live):
    asset = assets[0]
    asset.source, asset.generator, asset.prompt = "ai_generated", "fixture-model", "An illustrative concept"
    asset.tags = {"product": "demo", "asset_type": "ai_concept", "rights_status": "internal_testing_only"}
    asset.rights = "Internal testing only; public posting not approved."
    post.ai_disclosure = True
    post.dry_run = True  # The requested execution, not a stale post flag, controls review scope.
    observed = []

    class Capture(Providers):
        def review(self, context):
            observed.append(context)
            return super().review(context)

    checks(db, settings, post, providers=Capture(settings), live=live)
    context = observed[0]
    assert context["execution_mode"] == ("live" if live else "dry_run")
    assert context["public_publication"] is live
    assert context["product"] == post.product
    assert context["content_kind"] == post.kind
    record = context["assets"][0]
    assert record["asset_id"] == asset.id and record["asset_type"] == "ai_concept"
    assert record["source_or_creation_record"] == {
        "generator": asset.generator,
        "prompt": asset.prompt,
        "checksum": asset.checksum,
    }
    assert record["rights"] == asset.rights and record["rights_status"] == "internal_testing_only"
    assert record["media"] == asset.media
    assert context["renderer_context"]["lead_in"] == ""
    assert context["caption_parts"] == post.parts
    assert context["renderer_context"]["automatic_caption_disclosure"] is False
    assert context["visual_review"]["passed"] is True


def test_failed_visual_review_is_available_to_independent_review_and_still_blocks(db, settings, post, assets):
    assets[0].source = "ai_generated"
    post.ai_disclosure = True
    observed = []

    class BadVision(Providers):
        def vision(self, assets, context):
            return VisionReview(
                passed=False,
                reasons=["Unsupported feature"],
                garbled_text=False,
                misused_logos=False,
                unexpected_people=False,
            )

        def review(self, context):
            observed.append(context["visual_review"])
            return Review(score=1, reasons=["Text matches"])

    results = run_gate(db, settings, post, providers=BadVision(settings))
    assert observed[0]["passed"] is False
    assert post.status == "held"
    assert not next(c for c in results if c.name == "vision_review").passed


@pytest.mark.parametrize("flag", [False, True])
def test_legacy_company_asset_with_ai_creation_record_requires_vision_and_source_correction(
    db, settings, post, assets, flag
):
    asset = assets[0]
    asset.generator, asset.prompt = "Seedance 2.5", "Illustrative canopy"
    post.ai_disclosure = flag
    observed = []

    class Capture(Providers):
        def vision(self, assets, context):
            observed.append(context)
            return super().vision(assets, context)

    result = checks(db, settings, post, providers=Capture(settings))
    assert observed and result["vision_review"].details["reasons"]
    disclosure = result["disclosure"]
    assert not disclosure.passed and disclosure.details["required"]
    assert any("AI creation details require Source" in r for r in disclosure.details["reasons"])
    asset.source = "ai_generated"
    post.ai_disclosure = True
    assert checks(db, settings, post)["disclosure"].passed


def test_legacy_ai_source_supplies_known_metadata_without_inventing_product_family(db, settings, post, assets):
    asset = assets[0]
    asset.source, asset.generator, asset.prompt = "ai_generated", "Seedance 2.5", "Concept"
    asset.tags = {}
    asset.rights = "public"
    post.ai_disclosure = True
    observed = []

    class Capture(Providers):
        def review(self, context):
            observed.append(context["assets"][0])
            return super().review(context)

    checks(db, settings, post, providers=Capture(settings))
    assert observed[0]["asset_type"] == "ai_concept"
    assert observed[0]["rights_status"] == "public"
    assert observed[0]["product_family"] is None


@pytest.mark.parametrize("field,value", [
    ("generator", "Seedance 2.5"), ("prompt", "A new concept"),
    ("tags", {"product_family": ["canopies", "pergolas"]}),
])
def test_provenance_corrections_invalidate_existing_approval(post, assets, field, value):
    previous = content_digest(post, assets)
    setattr(assets[0], field, value)
    assert content_digest(post, assets) != previous


@pytest.mark.parametrize(
    "mutation,failed",
    [
        (lambda p: setattr(p, "caption", p.caption + "\nGuaranteed 50% savings."), "grounding"),
        (lambda p: setattr(p, "fact_ids", ["made-up"]), "grounding"),
        (lambda p: setattr(p, "caption", "a" * 3001), "length"),
        (lambda p: setattr(p, "caption", "#Tudelu #one #two #three"), "hashtags"),
        (lambda p: setattr(p, "caption", p.caption + " @customer"), "mentions_links"),
        (lambda p: setattr(p, "caption", p.caption + " guaranteed"), "banned_phrases"),
        (lambda p: setattr(p, "caption", 'Customer says "Love it"'), "testimonials"),
        (lambda p: setattr(p, "ai_disclosure", True), "disclosure"),
        (lambda p: setattr(p, "caption", p.caption + " https://evil.example/"), "urls"),
    ],
)
def test_gate_holds_each_rule_failure(db, settings, post, mutation, failed):
    mutation(post)
    result = run_gate(db, settings, post)
    assert post.status == "held"
    assert not next(c for c in result if c.name == failed).passed


def test_prices_offers_expire_at_publication_time(db, settings, post):
    doc = db.get(FactSheet, post.facts_id).document
    rows = list(doc["facts"]) + [
        {
            "id": "price",
            "product": "demo",
            "kind": "price",
            "text": "Price: USD 100.",
            "amount": "100",
            "currency": "USD",
            "valid_from": (now() - timedelta(days=1)).isoformat(),
            "valid_until": (now() + timedelta(minutes=1)).isoformat(),
        }
    ]
    facts = change_facts(db, post, facts=rows)
    post.parts = [{"fact_id": "price", "text": "Price: USD 100."}]
    post.fact_ids = ["price"]
    from social.config import profiles

    post.caption = render(post.parts, profiles(settings)["linkedin"], facts, "linkedin", settings)
    assert checks(db, settings, post)["prices_offers_validity"].passed
    assert not checks(db, settings, post, at=now() + timedelta(minutes=2))["prices_offers_validity"].passed
    post.parts = [{"fact_id": "price", "text": "Price: USD 10."}]
    assert not checks(db, settings, post)["grounding"].passed


def test_unknown_fact_id_cannot_launder_factual_claim(db, settings, post):
    post.parts = [{"fact_id": "company.name", "text": "Tudelu saves 90% energy."}]
    assert not checks(db, settings, post)["grounding"].passed


def test_rights_and_media_checks(db, settings, post, assets):
    assets[0].rights_expire_at = now() - timedelta(seconds=1)
    assets[0].media = assets[0].media | {"width": 100, "bytes": 100_000_000}
    result = checks(db, settings, post)
    assert not result["rights"].passed
    assert not result["media"].passed


def test_metadata_bytes_preserved_and_tampering_held(db, settings, post, assets):
    store = storage(settings)
    original = store.read(assets[0].storage_key)
    assert b"provenance fixture" in original
    store.path(assets[0].storage_key).write_bytes(original + b"tampered")
    assert not checks(db, settings, post)["provenance_integrity"].passed


def test_duplicate_semantic_embedding_and_cross_platform_exact(db, settings, post):
    run_gate(db, settings, post)
    clone = Post(
        platform="linkedin",
        campaign="other",
        product="demo",
        facts_id=post.facts_id,
        asset_ids=post.asset_ids,
        parts=post.parts,
        fact_ids=post.fact_ids,
        caption=post.caption,
        status="generated",
    )
    db.add(clone)
    db.flush()
    post.created_at = now() - timedelta(days=90)
    post.published_at = now()
    post.status = "published"
    assert not checks(db, settings, clone)["duplicates"].passed
    clone.platform = "facebook"
    assert not checks(db, settings, clone)["duplicates"].passed


def test_model_low_score_and_exception_fail_closed(db, settings, post):
    class Low(Providers):
        def review(self, context):
            return Review(score=0.2, reasons=["Misleading claim"])

    assert not checks(db, settings, post, providers=Low(settings))["model_review"].passed

    class Broken(Providers):
        def review(self, context):
            raise TimeoutError

    assert not checks(db, settings, post, providers=Broken(settings))["model_review"].passed


def test_ai_disclosure_and_vision_failures(db, settings, post, assets):
    assets[0].source, assets[0].generator, assets[0].prompt = "ai_edited", "test-model", "test-prompt"
    assert not checks(db, settings, post)["disclosure"].passed

    class BadVision(Providers):
        def vision(self, assets, context):
            return VisionReview(
                passed=False,
                reasons=["Garbled label"],
                garbled_text=True,
                misused_logos=False,
                unexpected_people=False,
            )

    assert not checks(db, settings, post, providers=BadVision(settings))["vision_review"].passed


@pytest.mark.parametrize("platform", ["linkedin", "instagram", "facebook", "x", "tiktok"])
@pytest.mark.parametrize("source", ["ai_generated", "ai_edited"])
def test_ai_captions_pass_without_disclosure_text_and_keep_required_disclaimers(
    db, settings, post, assets, platform, source
):
    from social.pipeline import generate

    change_facts(db, post, required_disclaimers=["Availability varies."])
    assets[0].source, assets[0].generator, assets[0].prompt = source, "fixture", "fixture"
    post.platform, post.status = platform, "draft"
    db.commit()  # Persist the fixture's draft state before the operation claims its row.
    generate(db, settings, post)
    assert post.ai_disclosure
    assert "AI-generated or AI-edited media." not in post.caption
    assert "Availability varies." in post.caption
    results = checks(db, settings, post)
    assert results["grounding"].passed
    assert results["disclosure"].passed
    assert results["disclosure"].details["method"] != "caption"
    assert not results["disclosure"].details["caption_disclosure"]

    # Removing the caption sentence must not weaken the asset provenance check.
    post.ai_disclosure = False
    assert not checks(db, settings, post)["disclosure"].passed


def test_saved_legacy_caption_needs_regeneration_and_recheck(db, settings, post, assets):
    assets[0].source, assets[0].generator, assets[0].prompt = "ai_generated", "fixture", "fixture"
    post.ai_disclosure = True
    post.caption += "\n\nAI-generated or AI-edited media."
    run_gate(db, settings, post)
    assert post.status == "held"
    assert not db.scalars(select(CheckResult).where(CheckResult.name == "grounding")).one().passed


def test_testimonial_requires_real_customer_tag_and_permission(db, settings, post, assets):
    post.caption = 'Customer says "Good"'
    assets[0].customer_permission = True
    assets[0].permission_reference = "release-123"
    assert not checks(db, settings, post)["testimonials"].passed
    assets[0].tags = {"real_customer_content": True}
    assert checks(db, settings, post)["testimonials"].passed


def test_live_cannot_use_demo_or_fixture_reviews(db, settings, post):
    assert not checks(db, settings, post, live=True)["live_readiness"].passed


def test_url_resolution_and_redirect_revalidation(monkeypatch):
    visited = []
    monkeypatch.setattr("social.gate.public_url", lambda u: visited.append(u))

    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://example.org/end"})
        return httpx.Response(200)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert check_urls("https://example.org/start", {"https://example.org/start"}, client)[0]
    assert visited == ["https://example.org/start", "https://example.org/end"]
    assert not check_urls("https://example.org/start", set(), client)[0]
    assert not check_urls(
        "https://example.org/404",
        {"https://example.org/404"},
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404))),
    )[0]


def test_private_url_refused():
    from social.security import public_url

    with pytest.raises(ValueError):
        public_url("https://127.0.0.1/admin")


def test_x_weighted_length():
    assert weighted_length("https://example.com/very-long-url", "x") == 23
    assert weighted_length("漢字", "x") == 4


@pytest.mark.parametrize(
    "field,value",
    [
        ("mime", "video/webm"),
        ("codec", "vp9"),
        ("pixel_format", "yuv444p"),
        ("duration", 120),
        ("fps", 12),
        ("audio_codecs", ["mp3"]),
        ("bit_rate", 26_000_000),
        ("width", 100),
        ("height", 3000),
    ],
)
def test_video_media_envelope(settings, assets, field, value):
    from social.config import profiles

    assets[0].media = {
        "mime": "video/mp4",
        "width": 720,
        "height": 720,
        "bytes": 1000,
        "duration": 4,
        "codec": "h264",
        "pixel_format": "yuv420p",
        "fps": 24,
        "audio_codecs": [],
        "bit_rate": 1_000_000,
    }
    assert not media_errors(assets, profiles(settings)["tiktok"], "tiktok")
    assets[0].media = assets[0].media | {field: value}
    assert media_errors(assets, profiles(settings)["tiktok"], "tiktok")
