"""Natural-language denials must not route inspiration ideas into real-media editing."""

import pytest
from fastapi.testclient import TestClient
from test_idea_plans import facts, logo  # noqa: F401
from test_real_ideas import plan_form

from social.app import create_app
from social.idea_plan import read_signals


ILLUSTRATION = (
    "Create a tasteful watercolor-style illustration of a white pergola shading a small garden seating "
    "area. Keep the actual pergola structure believable. Lots of greenery, quiet afternoon mood, no "
    "lettering. This is design inspiration, not a finished project."
)


@pytest.mark.parametrize("idea", [
    ILLUSTRATION,
    "Generate a concept image, not a photo of our completed project.",
    "Don't use our real project photos. Create an inspiration image.",
    "Make an animation rather than installation footage.",
    "Create a concept without using our actual photos.",
    "We do not have real project photos; generate an illustration.",
])
def test_negated_mentions_do_not_request_real_media(idea):
    assert not read_signals(idea)["real"]


@pytest.mark.parametrize("idea", [
    "Show our finished projects.",
    "Installation process of partition walls.",
    "I don't want AI. Use our real project photos.",
    "Use our photos, not our installation footage.",
    "A photo of our finished installation without people.",
    "Not only our real photos, but also our installation footage.",
])
def test_positive_requests_still_use_real_media(idea):
    assert read_signals(idea)["real"]


def test_inspiration_idea_stays_on_the_ai_image_plan(db, settings, facts, logo):  # noqa: F811
    with TestClient(create_app(settings)) as client:
        response = client.post("/forms/ideas/plan", data={
            "csrf": client.get("/api/session").json()["csrf"],
            "prompt": ILLUSTRATION, "format": "auto", "kind": "auto",
            "platforms": ["x"], "rights": "public", "facts_id": facts.id,
        })
        assert response.status_code == 200, response.text
        form = plan_form(response.text)
        assert form["operation"] == ["image"]
        assert form["plan_kind"] == ["non_promotional"]
        assert "Adjust anything before generating" in response.text
