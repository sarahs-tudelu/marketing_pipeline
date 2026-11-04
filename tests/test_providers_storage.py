import base64
import json
from datetime import datetime

import httpx
import pytest
from pydantic import SecretStr

from social.providers import Claude, OpenAI
from social.storage import ingest, storage, watch_folder


def test_openai_generation_and_vision_preserve_original_bytes(db, settings, assets):
    data = storage(settings).read(assets[0].storage_key)
    settings = settings.model_copy(update={"openai_api_key": SecretStr("mock-only")})
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        seen.append(request.url.path)
        if request.url.path.endswith("images/generations"):
            assert payload["model"] == settings.openai_image_model
            assert payload["output_format"] == "jpeg"
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(data).decode()}]})
        if request.url.path.endswith("responses"):
            assert payload["store"] is False
            assert payload["model"] == settings.openai_vision_model
            output_format = payload["text"]["format"]
            assert output_format["type"] == "json_schema" and output_format["strict"] is True
            schema = output_format["schema"]
            assert schema["additionalProperties"] is False
            assert set(schema["required"]) == set(schema["properties"])
            for field in ("passed", "garbled_text", "misused_logos", "unexpected_people"):
                assert schema["properties"][field]["type"] == "boolean"
            image = payload["input"][0]["content"][1]["image_url"]
            assert base64.b64decode(image.split(",", 1)[1]) == data
            result = {
                "passed": True,
                "reasons": ["Approved"],
                "garbled_text": False,
                "misused_logos": False,
                "unexpected_people": False,
                "visible_claims": [],
            }
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [
                        {"type": "message", "content": [{"type": "output_text", "text": json.dumps(result)}]}
                    ],
                },
            )
        assert request.url.path.endswith("embeddings")
        return httpx.Response(200, json={"data": [{"embedding": [0.5, 0.8]}]})

    provider = OpenAI(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    original, name, model = provider.generate("Abstract internal fixture")
    generated = ingest(
        db,
        settings,
        original,
        name,
        source="ai_generated",
        generator=model,
        prompt="Abstract internal fixture",
        rights="Fixture only",
    )
    assert storage(settings).read(generated.storage_key) == data
    assert generated.generator == settings.openai_image_model
    assert provider.vision([generated], {}).passed
    assert provider.embedding("fixture") == [0.5, 0.8]
    assert len(seen) == 3


def test_claude_review_independent_and_incomplete_output_rejected(settings):
    settings = settings.model_copy(update={"anthropic_api_key": SecretStr("mock-only")})
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(
            200,
            json={
                "stop_reason": "end_turn",
                "content": [
                    {
                        "type": "text",
                        "text": '{"score":0.5,"reasons":["Unsupported"],"unsupported_claims":["unapproved price"]}',
                    }
                ],
            },
        )

    provider = Claude(settings, httpx.Client(transport=httpx.MockTransport(handler)))
    review = provider.review({"caption": "unapproved price"})
    assert review.score == 0.5 and review.unsupported_claims
    assert len(seen[0]["messages"]) == 1
    assert seen[0]["output_config"]["effort"] == "xhigh"
    provider.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"stop_reason": "max_tokens", "content": []})
        )
    )
    with pytest.raises(ValueError, match="incomplete"):
        provider.generate({})


def test_watched_manifest_preserves_expiry_offset(db, settings, assets):
    data = storage(settings).read(assets[0].storage_key)
    settings.watch_dir.mkdir()
    (settings.watch_dir / "fixture.jpg").write_bytes(data)
    (settings.watch_dir / "fixture.json").write_text(
        json.dumps(
            {
                "filename": "fixture.jpg",
                "rights": "Company owned",
                "rights_expire_at": "2030-01-01T10:00:00-05:00",
            }
        )
    )
    watch_folder(db, settings)
    from sqlalchemy import select

    from social.models import Asset

    added = db.scalar(select(Asset).where(Asset.original_name == "fixture.jpg"))
    assert added.rights_expire_at == datetime(2030, 1, 1, 15)
    assert (settings.watch_dir / "fixture.done.json").exists()
