import base64
import io
import json
import wave
from datetime import timedelta

import httpx
import numpy as np
import pytest
from pydantic import SecretStr

from social.audio_review import extract_tracks, observe_audio, review_audio
from social.db import now
from social.facts import Facts
from social.gate import evaluate, run_gate
from social.models import FactSheet
from social.providers import AudioObservation, AudioReview, OpenAI, PartCheck, Providers
from social.storage import ingest, storage


def audio_bytes(seconds=2, channels=2):
    samples = np.zeros((round(24000 * seconds), channels), dtype="<i2")
    samples[-2400:] = 12345  # An audible tail verifies that extraction reaches the end.
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setparams((channels, 2, 24000, 0, "NONE", "not compressed"))
        wav.writeframes(samples.tobytes())
    return output.getvalue()


def observation(**changes):
    return AudioObservation.model_validate(dict(summary="Instrumental music only.", has_speech=False,
        has_lyrics=False, has_music=True, uncertain=False, segments=[], limitations=[]) | changes)


def test_audio_extraction_preserves_complete_90_second_track_and_stereo():
    tracks = extract_tracks(audio_bytes(90))
    assert len(tracks) == 1 and tracks[0]["duration_seconds"] == 90
    with wave.open(io.BytesIO(tracks[0]["wav"])) as wav:
        assert wav.getnchannels() == 2 and wav.getframerate() == 24000
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
        assert samples[-1] == 12345 and samples[0] == 0
    with pytest.raises(ValueError, match="90-second"):
        extract_tracks(audio_bytes(91))


def test_observations_cache_exact_bytes_model_mode_and_preserve_original(db, settings):
    asset = ingest(db, settings, audio_bytes(), "track.wav", rights="public")
    provider = Providers(settings)
    calls = []
    provider.listen = lambda wav: (calls.append(wav) or observation())
    settings.ai_mode = "live"
    first = observe_audio(db, settings, asset, provider)
    assert first["status"] == "observed" and len(calls) == 1
    assert observe_audio(db, settings, asset, provider)["tracks"] == first["tracks"] and len(calls) == 1
    settings.openai_audio_review_model = "other-model"
    observe_audio(db, settings, asset, provider)
    assert len(calls) == 2
    settings.ai_mode = "fixture"
    assert observe_audio(db, settings, asset, provider)["simulated"]
    assert len(calls) == 3
    assert storage(settings).read(asset.storage_key) == audio_bytes()
    storage(settings).path(asset.storage_key).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="integrity"):
        observe_audio(db, settings, asset, provider)
    assert len(calls) == 3


@pytest.mark.parametrize("finish,content", [
    ("length", "{}"), ("stop", "{}"), ("stop", "not json"),
    ("stop", json.dumps(observation().model_dump() | {"has_speech": True})),
])
def test_incomplete_or_inconsistent_listening_never_passes(settings, finish, content):
    settings.openai_api_key = SecretStr("test-secret")
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
        "choices": [{"finish_reason": finish, "message": {"content": content}}],
    })))
    with pytest.raises(ValueError):
        OpenAI(settings, client).listen(audio_bytes())


def test_audio_api_uses_real_audio_input_and_text_output_without_storage(settings):
    settings.openai_api_key = SecretStr("test-secret")
    original = audio_bytes()

    def handle(request):
        payload = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert payload["model"] == settings.openai_audio_review_model
        assert payload["store"] is False and payload["modalities"] == ["text"]
        audio = payload["messages"][1]["content"][1]["input_audio"]
        assert audio["format"] == "wav" and base64.b64decode(audio["data"]) == original
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(observation().model_dump()),
        }}]})

    assert OpenAI(settings, httpx.Client(transport=httpx.MockTransport(handle))).listen(original).has_music


def test_audio_gate_checks_company_media_and_supplies_evidence_to_final_reviewer(db, settings, post):
    asset = ingest(db, settings, audio_bytes(), "company.wav", rights="public")
    post.asset_ids = [asset.id]  # Media format is separately held; audio must still be reviewed.
    seen = []

    class Capture(Providers):
        def listen(self, wav):
            return observation()

        def review(self, context):
            seen.append(context["audio_review"])
            return super().review(context)

    results = {c.name: c for c in evaluate(db, settings, post, providers=Capture(settings))[0]}
    assert results["audio_review"].passed
    assert seen[0]["passed"] and seen[0]["details"]["assets"][0]["tracks"][0]["observation"]["has_music"]
    assert not results["media"].passed


@pytest.mark.parametrize("kind", ["speech", "lyrics"])
def test_each_transcribed_part_needs_supported_current_citations(db, settings, post, kind):
    asset = ingest(db, settings, audio_bytes(), "track.wav", rights="public", tags={"product": post.product})
    facts = Facts.model_validate(db.get(FactSheet, post.facts_id).document)
    fact = next(f for f in facts.facts if f.id in post.fact_ids)
    providers = Providers(settings)
    providers.listen = lambda _: observation(**{f"has_{kind if kind == 'speech' else 'lyrics'}": True},
                                            segments=[{"kind": kind, "text": fact.text}])
    context = {"product": post.product}
    assert review_audio(db, settings, [asset], context, facts, now(), providers)[0]
    facts.facts = [f.model_copy(update={"valid_until": now() - timedelta(days=1)}) for f in facts.facts]
    assert not review_audio(db, settings, [asset], context, facts, now(), providers)[0]
    providers.review_audio = lambda _: AudioReview(score=1, reasons=["ok"], unsupported_claims=[], part_checks=[])
    assert not review_audio(db, settings, [asset], context, facts, now(), providers)[0]
    providers.review_audio = lambda _: AudioReview(score=1, reasons=["ok"], unsupported_claims=[], part_checks=[
        PartCheck(part_index=0, supported=True, fact_ids=["invented"], reasons=["ok"]),
    ])
    assert not review_audio(db, settings, [asset], context, facts, now(), providers)[0]


@pytest.mark.parametrize("failure", ["uncertain", "unavailable", "unsupported"])
def test_audio_holds_cannot_be_overridden_by_passing_caption_review(db, settings, post, failure):
    asset = ingest(db, settings, audio_bytes(), "track.wav", rights="public")
    post.asset_ids = [asset.id]

    class Failing(Providers):
        def listen(self, wav):
            if failure == "unavailable":
                raise RuntimeError("private-provider-body")
            return observation(uncertain=failure == "uncertain")

        def review_audio(self, context):
            return AudioReview(score=0, reasons=["Unsupported claim"], unsupported_claims=["Claim"], part_checks=[])

    checks = run_gate(db, settings, post, providers=Failing(settings))
    check = next(c for c in checks if c.name == "audio_review")
    assert post.status == "held" and not check.passed
    assert "private-provider-body" not in json.dumps(check.details)


def test_no_audio_stream_is_verified_without_provider_call(db, settings, assets, post):
    from social.seed import seed_media
    asset = ingest(db, settings, seed_media()[1], "silent.mp4", rights="public")
    provider = Providers(settings)
    provider.listen = lambda _: pytest.fail("No audio needs no listening call")
    provider.review_audio = lambda _: pytest.fail("No audio needs no factual review")
    facts = Facts.model_validate(db.get(FactSheet, post.facts_id).document)
    passed, details = review_audio(db, settings, [asset, *assets], {"product": post.product}, facts, now(), provider)
    assert passed and all(a["status"] == "no_audio" for a in details["assets"])
