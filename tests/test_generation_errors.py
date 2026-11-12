import base64
import io
import json
from types import SimpleNamespace

import httpx
import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import SecretStr
from sqlalchemy import select

from social.app import create_app
from social.facts import load_facts
from social.models import Asset, Transition
from social.pipeline import draft, generate
from social.provider_errors import ProviderError
from social.providers import Claude, OpenAI
from social.storage import LocalStore


@pytest.mark.parametrize("provider_class", [OpenAI, Claude])
@pytest.mark.parametrize(
    "status,code,category",
    [
        (401, "invalid_api_key", "authentication"),
        (403, "permission_denied", "permission"),
        (404, "model_not_found", "model"),
        (400, "content_policy_violation", "policy"),
        (429, "insufficient_quota", "limit"),
        (503, "server_error", "service"),
    ],
)
def test_provider_failures_are_safe_and_not_retried(settings, provider_class, status, code, category):
    settings = settings.model_copy(
        update={"openai_api_key": SecretStr("fake-key"), "anthropic_api_key": SecretStr("fake-key")}
    )
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            status,
            headers={"x-request-id": "req_test-123"},
            json={"error": {"code": code, "message": "sensitive upstream details fake-key"}},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as error:
            provider_class(settings, client).generate({} if provider_class is Claude else "test image")
    assert error.value.category == category
    assert error.value.upstream_status == status
    assert error.value.request_id == "req_test-123"
    assert "fake-key" not in str(error.value)
    assert "sensitive upstream" not in str(error.value)
    assert len(requests) == 1


@pytest.mark.parametrize("timeout", [False, True])
def test_provider_network_errors(settings, timeout):
    settings = settings.model_copy(update={"openai_api_key": SecretStr("mock")})

    def handler(request):
        error = httpx.ReadTimeout if timeout else httpx.ConnectError
        raise error("sensitive connection details", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as exc:
            OpenAI(settings, client).generate("test")
    assert exc.value.status == (504 if timeout else 502)
    assert "sensitive" not in str(exc.value)


@pytest.mark.parametrize("payload", [{}, {"data": []}, {"data": [{"b64_json": "broken"}]}])
def test_unusable_image_response_is_safe(settings, payload):
    settings = settings.model_copy(update={"openai_api_key": SecretStr("mock")})
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
        with pytest.raises(ProviderError, match="usable image"):
            OpenAI(settings, client).generate("test")


def image_response():
    stream = io.BytesIO()
    Image.new("RGB", (64, 64), "green").save(stream, format="JPEG", comment=b"original provenance")
    return stream.getvalue()


@pytest.mark.parametrize("failure", ["NoSuchBucket", "AccessDenied", "quota", "missing_key"])
def test_image_form_shows_error_preserves_input_and_can_recover(db, settings, monkeypatch, caplog, failure):
    import social.providers as providers

    caplog.set_level("INFO", logger="social")
    settings = settings.model_copy(update={"ai_mode": "live", "image_provider": "openai", "openai_api_key": SecretStr("fake-key")})
    if failure == "missing_key":
        settings.openai_api_key = SecretStr("")
    data = image_response()
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "quota":
            return httpx.Response(429, json={"error": {"message": "fake-key private content"}})
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(data).decode()}]})

    def failed_put(*args, **kwargs):
        raise ClientError({"Error": {"Code": failure, "Message": "fake-key private content"}}, "PutObject")

    with httpx.Client(transport=httpx.MockTransport(handler)) as mocked:
        monkeypatch.setattr(providers, "OpenAI", lambda s: OpenAI(s, mocked))
        if failure in {"NoSuchBucket", "AccessDenied"}:
            monkeypatch.setattr(LocalStore, "put", failed_put)
        with TestClient(create_app(settings)) as client:
            form = {
                "csrf": client.get("/api/session").json()["csrf"],
                "prompt": '</textarea><script>alert("unsafe")</script>',
                "kind": "image",
                "rights": "public",
                "owner": "Tudelu marketing",
                "tags": '{"campaign":"test"}',
            }
            response = client.post("/forms/generate-asset", data=form)
            assert response.status_code == (400 if failure == "missing_key" else 502)
            expected = {
                "NoSuchBucket": "S3_BUCKET",
                "AccessDenied": "bucket write permissions",
                "quota": "quota or rate limit",
                "missing_key": "OPENAI_API_KEY is missing",
            }
            assert expected[failure] in response.text
            assert 'role="alert"' in response.text
            assert "&lt;/textarea&gt;&lt;script&gt;" in response.text
            assert '<script>alert("unsafe")</script>' not in response.text
            assert '<option value="public" selected>' in response.text
            assert 'value="Tudelu marketing"' in response.text
            assert "fake-key" not in response.text + caplog.text
            assert "private content" not in response.text + caplog.text
            assert len(calls) == (0 if failure == "missing_key" else 1)
            assert db.scalars(select(Asset)).all() == []
            # A failed attempt leaves the database usable and can be corrected.
            assert client.get("/admin/assets").status_code == 200
    events = [json.loads(r.getMessage()) for r in caplog.records if r.name == "social"]
    assert any(e.get("event") == "asset_generation_failed" for e in events)


def test_image_form_success_preserves_bytes_and_provenance(db, settings, monkeypatch):
    import social.providers as providers

    settings = settings.model_copy(update={"ai_mode": "live", "image_provider": "openai", "openai_api_key": SecretStr("mock")})
    data = image_response()
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(data).decode()}]})
        )
    ) as mocked:
        monkeypatch.setattr(providers, "OpenAI", lambda s: OpenAI(s, mocked))
        with TestClient(create_app(settings)) as client:
            response = client.post(
                "/forms/generate-asset",
                data={
                    "csrf": client.get("/api/session").json()["csrf"],
                    "prompt": "Abstract background",
                    "kind": "image",
                    "rights": "public",
                    "owner": "Tudelu",
                    "tags": '{"campaign":"test"}',
                },
                follow_redirects=False,
            )
    assert response.status_code == 303
    asset = db.scalar(select(Asset))
    assert asset.source == "ai_generated" and asset.prompt == "Abstract background"
    assert asset.generator == settings.openai_image_model
    assert asset.tags == {"campaign": "test", "asset_type": "ai_concept"}
    assert (asset.rights, asset.owner) == ("public", "Tudelu")
    assert LocalStore(settings).read(asset.storage_key) == data


@pytest.mark.parametrize(
    "tags,rights",
    [("not json", "public"), ("[]", "public"), ("{}", " "), ("{}", "Company permitted")],
)
def test_invalid_form_rejected_before_paid_generation(db, settings, monkeypatch, tags, rights):
    settings = settings.model_copy(update={"ai_mode": "live", "image_provider": "openai"})

    def must_not_generate(*args):
        raise AssertionError("Invalid forms must not call a paid provider")

    monkeypatch.setattr(OpenAI, "generate", must_not_generate)
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/forms/generate-asset",
            data={
                "csrf": client.get("/api/session").json()["csrf"],
                "prompt": "test",
                "kind": "image",
                "rights": rights,
                "tags": tags,
            },
        )
    assert response.status_code == 400
    assert 'role="alert"' in response.text


def test_caption_failure_is_held_with_actionable_reason(db, settings, assets):
    facts = load_facts(db, settings.facts_path)
    post = draft(db, "linkedin", "test", [assets[0].id], facts.id, product="demo")

    def unavailable(context):
        raise ProviderError(
            "Claude denied access. Check model permissions.", provider="Claude", category="permission"
        )

    generate(db, settings, post, providers=SimpleNamespace(caption=unavailable))
    assert post.status == "held"
    history = db.scalars(
        select(Transition).where(Transition.post_id == post.id).order_by(Transition.id)
    ).all()
    assert "Check model permissions" in history[-1].reason
