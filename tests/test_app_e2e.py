import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from social.app import create_app
from social.models import Post, RequestRecord, Transition
from social.seed import demo


def test_end_to_end_five_platforms_offline(db, settings, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Dry-run demo must never use the network")

    monkeypatch.setattr(httpx.Client, "request", no_network)
    result = demo(db, settings)
    assert {r["platform"] for r in result} == {"linkedin", "instagram", "facebook", "x", "tiktok"}
    assert all(
        r["status"] == ("awaiting_manual" if r["platform"] == "tiktok" else "published") for r in result
    )
    assert all(r["request_count"] and r["dry_run"] for r in result)
    assert len({r["scheduled_at"] for r in result}) == 5
    posts = db.scalars(select(Post)).all()
    assert len({p.caption for p in posts}) == 5
    for p in posts:
        statuses = [
            t.to_status
            for t in db.scalars(select(Transition).where(Transition.post_id == p.id).order_by(Transition.id))
        ]
        assert statuses == ["draft", "generated", "checked", "approved", "scheduled", "publishing", p.status]
    before = len(db.scalars(select(RequestRecord)).all())
    assert demo(db, settings) == result
    assert len(db.scalars(select(RequestRecord)).all()) == before


def test_admin_pages_login_and_csrf(db, settings):
    app = create_app(settings)
    with TestClient(app) as client:
        for path in [
            "/",
            "/admin/connections",
            "/admin/assets",
            "/admin/facts",
            "/admin/compose",
            "/admin/queues",
            "/admin/analytics",
        ]:
            response = client.get(path)
            assert response.status_code == 200, response.text
            assert "Tudelu" in response.text
        assert "Not connected" in client.get("/admin/connections").text
        assert client.post("/forms/control", data={"enabled": "true"}).status_code == 403
        token = client.get("/api/session").json()["csrf"]
        assert client.post("/forms/control", data={"enabled": "true", "csrf": token}).status_code == 200
        assert "Publishing is stopped" in client.get("/").text


def test_basic_auth_protects_admin(db, settings):
    from pydantic import SecretStr

    secured = settings.model_copy(
        update={
            "auth_mode": "basic",
            "admin_username": "internal",
            "admin_password": SecretStr("test-password"),
        }
    )
    with TestClient(create_app(secured)) as client:
        assert client.get("/").status_code == 401
        assert client.get("/", auth=("internal", "test-password")).status_code == 200
        assert client.get("/health").status_code == 200


def test_providers_are_mocked_no_live_keys(settings):
    from pydantic import SecretStr

    from social.providers import Claude

    settings = settings.model_copy(
        update={"anthropic_api_key": SecretStr("test"), "openai_api_key": SecretStr("test")}
    )

    def handler(request):
        import json

        payload = json.loads(request.content)
        assert payload["output_config"]["effort"] == "xhigh"
        assert payload["thinking"] == {"type": "adaptive"}
        return httpx.Response(
            200,
            json={
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": '{"parts":[{"fact_id":"name","text":"Tudelu."}]}'}],
            },
        )

    assert (
        Claude(settings, httpx.Client(transport=httpx.MockTransport(handler))).generate({})[0]["fact_id"]
        == "name"
    )
