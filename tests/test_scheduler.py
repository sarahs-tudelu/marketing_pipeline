from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from social.config import Profile, profiles
from social.gate import run_gate
from social.scheduler import in_window, permitted, schedule


def record(at, platform="linkedin", campaign="other", status="published", kind="non_promotional"):
    return SimpleNamespace(
        platform=platform,
        campaign=campaign,
        status=status,
        account_id="account",
        kind=kind,
        scheduled_at=at,
        published_at=at if status == "published" else None,
    )


@pytest.fixture
def profile():
    return Profile(
        max_length=3000,
        hashtag_limit=3,
        tone="clear",
        lead_in="Hi",
        timezone="UTC",
        windows=[["00:00", "23:59"]],
        hard_cap=3,
        target_per_day=3,
        minimum_spacing_minutes=15,
        promotional_ratio=1,
    )


def test_rolling_cap_crosses_midnight_and_excludes_exact_boundary(settings, profile):
    at = datetime(2026, 11, 2, 10)
    p = record(at)
    others = [record(at - timedelta(hours=h)) for h in (1, 12, 23)]
    assert not permitted(at, p, others, profile, settings)
    others[2] = record(at - timedelta(hours=24))
    assert permitted(at, p, others, profile, settings)


def test_future_reservations_prevent_overbooking(settings, profile):
    at = datetime(2026, 11, 2, 10)
    others = [record(at + timedelta(hours=h), status="scheduled") for h in (1, 2, 3)]
    assert not permitted(at, record(at), others, profile, settings)


def test_spacing_per_account_and_campaign_stagger(settings, profile):
    at = datetime(2026, 11, 2, 10)
    assert not permitted(at, record(at), [record(at - timedelta(minutes=10))], profile, settings)
    assert not permitted(at, record(at), [record(at, platform="x")], profile, settings)
    assert permitted(at, record(at), [record(at - timedelta(minutes=30), platform="x")], profile, settings)


def test_time_windows_dst(settings):
    profile = profiles(settings)["linkedin"]
    assert in_window(datetime(2026, 10, 30, 13), profile)  # 9 EDT
    assert not in_window(datetime(2026, 11, 2, 13), profile)  # 8 EST
    assert in_window(datetime(2026, 11, 2, 14), profile)


def test_content_mix(settings, profile):
    profile.promotional_ratio = 0.5
    at = datetime(2026, 11, 2, 10)
    assert not permitted(
        at,
        record(at, kind="promotional"),
        [record(at - timedelta(hours=2), kind="promotional")],
        profile,
        settings,
    )


def test_tiktok_pending_limit(settings, profile):
    at = datetime(2026, 11, 2, 10)
    previous = [
        record(at - timedelta(days=i + 1), platform="tiktok", status="awaiting_manual") for i in range(4)
    ]
    assert not permitted(at, record(at, platform="tiktok"), previous, profile, settings)


def test_invalid_cap_rejected():
    with pytest.raises(ValueError):
        Profile(
            max_length=3000, hashtag_limit=3, tone="clear", lead_in="Hi", hard_cap=5, documented_daily_limit=5
        )


def test_schedule_reserves_once(db, settings, post):
    run_gate(db, settings, post)
    at = schedule(db, settings, post)
    assert post.status == "scheduled" and post.scheduled_at == at
    with pytest.raises(ValueError):
        schedule(db, settings, post)
