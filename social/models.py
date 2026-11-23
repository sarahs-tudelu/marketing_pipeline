from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base, now, uid


class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    source: Mapped[str] = mapped_column(String(20))
    storage_key: Mapped[str] = mapped_column(String(255))
    original_name: Mapped[str] = mapped_column(String(255))
    checksum: Mapped[str] = mapped_column(String(64), index=True)
    generator: Mapped[str | None] = mapped_column(String(200))
    prompt: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[dict] = mapped_column(JSON, default=dict)
    rights: Mapped[str] = mapped_column(Text)
    owner: Mapped[str] = mapped_column(String(200), default="", server_default="")
    rights_expire_at: Mapped[datetime | None] = mapped_column(DateTime)
    customer_permission: Mapped[bool] = mapped_column(Boolean, default=False)
    permission_reference: Mapped[str] = mapped_column(Text, default="")
    media: Mapped[dict] = mapped_column(JSON)
    demo_only: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    # Deleted from the library but kept for the posts and media jobs that use it (social/library.py).
    removed_at: Mapped[datetime | None] = mapped_column(DateTime)
    removed_by: Mapped[str] = mapped_column(String(200), default="", server_default="")


class AssetEdit(Base):
    __tablename__ = "asset_edits"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"), index=True)
    actor: Mapped[str] = mapped_column(String(200))
    before: Mapped[dict] = mapped_column(JSON)
    after: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class DriveSource(Base):
    __tablename__ = "drive_sources"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    folder_id: Mapped[str] = mapped_column(String(200))
    folder_name: Mapped[str] = mapped_column(String(255), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    recursive: Mapped[bool] = mapped_column(Boolean, default=True)
    defaults: Mapped[dict] = mapped_column(JSON, default=dict)
    scan_state: Mapped[dict] = mapped_column(JSON, default=dict)
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str] = mapped_column(Text, default="")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    __mapper_args__ = {"version_id_col": revision}


class DriveImport(Base):
    __tablename__ = "drive_imports"
    file_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    folder_id: Mapped[str] = mapped_column(String(200))
    name: Mapped[str] = mapped_column(String(255))
    fingerprint: Mapped[str] = mapped_column(String(64))
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id"), index=True)
    status: Mapped[str] = mapped_column(String(20))
    error: Mapped[str] = mapped_column(Text, default="")
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime)


class FactSheet(Base):
    __tablename__ = "fact_sheets"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(100), unique=True)
    document: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Account(Base):
    __tablename__ = "accounts"
    platform: Mapped[str] = mapped_column(String(20), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(200), default="")
    credentials: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    refresh_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime)
    paused: Mapped[bool] = mapped_column(Boolean, default=False)
    live_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    auth_error: Mapped[str] = mapped_column(Text, default="")


class Post(Base):
    __tablename__ = "posts"
    prompted_by: Mapped[str | None] = mapped_column(String(200), index=True)
    prompted_by_label: Mapped[str | None] = mapped_column(String(320))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    platform: Mapped[str] = mapped_column(ForeignKey("accounts.platform"), index=True)
    account_id: Mapped[str] = mapped_column(String(200), default="")
    campaign: Mapped[str] = mapped_column(String(200))
    product: Mapped[str] = mapped_column(String(200), default="company")
    kind: Mapped[str] = mapped_column(String(30), default="promotional")
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    asset_ids: Mapped[list] = mapped_column(JSON)
    facts_id: Mapped[str] = mapped_column(ForeignKey("fact_sheets.id"))
    fact_ids: Mapped[list] = mapped_column(JSON, default=list)
    caption: Mapped[str] = mapped_column(Text, default="")
    parts: Mapped[list] = mapped_column(JSON, default=list)
    ai_disclosure: Mapped[bool] = mapped_column(Boolean, default=False)
    embedding: Mapped[list] = mapped_column(JSON, default=list)
    embedding_model: Mapped[str] = mapped_column(String(100), default="")
    approved_digest: Mapped[str] = mapped_column(String(64), default="")
    human_review_required: Mapped[bool] = mapped_column(Boolean, default=True)
    human_approved_digest: Mapped[str] = mapped_column(String(64), default="")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(36), unique=True, default=uid)
    remote_id: Mapped[str] = mapped_column(String(300), default="")
    live_url: Mapped[str] = mapped_column(Text, default="")
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    simulation_only: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class PostRetry(Base):
    __tablename__ = "post_retries"
    original_post_id: Mapped[str] = mapped_column(ForeignKey("posts.id"), primary_key=True)
    retry_post_id: Mapped[str] = mapped_column(ForeignKey("posts.id"), unique=True)
    actor: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class PostAvailability(Base):
    __tablename__ = "post_availability"
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id"), primary_key=True)
    state: Mapped[str] = mapped_column(String(30), default="unchecked")
    reason: Mapped[str] = mapped_column(Text, default="")
    checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    unavailable_since: Mapped[datetime | None] = mapped_column(DateTime)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    public_ids: Mapped[list] = mapped_column(JSON, default=list)


class AvailabilityCheck(Base):
    __tablename__ = "availability_checks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id"), index=True)
    state: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    actor: Mapped[str] = mapped_column(String(100))
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class PostRepost(Base):
    __tablename__ = "post_reposts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    original_post_id: Mapped[str] = mapped_column(ForeignKey("posts.id"), index=True)
    repost_post_id: Mapped[str] = mapped_column(ForeignKey("posts.id"), unique=True)
    actor: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Transition(Base):
    __tablename__ = "transitions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id"), index=True)
    from_status: Mapped[str] = mapped_column(String(30))
    to_status: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(100), default="pipeline")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class CheckResult(Base):
    __tablename__ = "checks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id"), index=True)
    run_id: Mapped[str] = mapped_column(String(36))
    name: Mapped[str] = mapped_column(String(60))
    passed: Mapped[bool] = mapped_column(Boolean)
    details: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class RequestRecord(Base):
    __tablename__ = "requests"
    __table_args__ = (UniqueConstraint("post_id", "step"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id"), index=True)
    step: Mapped[str] = mapped_column(String(100))
    request: Mapped[dict] = mapped_column(JSON)
    response: Mapped[dict] = mapped_column(JSON, default=dict)
    response_headers: Mapped[dict] = mapped_column(JSON, default=dict)
    state: Mapped[str] = mapped_column(String(30), default="pending")
    irreversible: Mapped[bool] = mapped_column(Boolean, default=False)
    dry_run: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class OAuthState(Base):
    __tablename__ = "oauth_states"
    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    platform: Mapped[str] = mapped_column(String(20))
    browser_hash: Mapped[str] = mapped_column(String(64))
    verifier: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)


class Control(Base):
    __tablename__ = "controls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    killed: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    automation_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(80))
    message: Mapped[str] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(String(200), unique=True)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Engagement(Base):
    __tablename__ = "engagement"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id"), index=True)
    metrics: Mapped[dict] = mapped_column(JSON)
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class MediaCampaign(Base):
    __tablename__ = "media_campaigns"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(200))
    brief: Mapped[dict] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    sequence: Mapped[int] = mapped_column(Integer, default=0)
    next_run_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class MediaJob(Base):
    __tablename__ = "media_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    campaign_id: Mapped[str | None] = mapped_column(ForeignKey("media_campaigns.id"))
    batch_key: Mapped[str] = mapped_column(String(200), unique=True)
    platform: Mapped[str] = mapped_column(String(20))
    brief: Mapped[dict] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(20), default="visual")
    task_id: Mapped[str] = mapped_column(String(100), default="")
    task_history: Mapped[list] = mapped_column(JSON, default=list)
    source_asset_ids: Mapped[list] = mapped_column(JSON, default=list)
    output_asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id"))
    post_id: Mapped[str | None] = mapped_column(ForeignKey("posts.id"), index=True)
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str] = mapped_column(Text, default="")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class UsageRecord(Base):
    """One paid AI request the app made, with what it was for and, when known, the post it served."""

    __tablename__ = "usage_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(20), index=True)
    purpose: Mapped[str] = mapped_column(String(60))
    model: Mapped[str] = mapped_column(String(120), default="")
    units: Mapped[dict] = mapped_column(JSON, default=dict)
    # Runway reports the credits it charged; token costs come from the price table, so they're estimates.
    cost_usd: Mapped[float | None] = mapped_column(Float)
    estimated: Mapped[bool] = mapped_column(Boolean, default=True)
    request_id: Mapped[str] = mapped_column(String(120), default="", index=True)
    post_id: Mapped[str | None] = mapped_column(String(36), index=True)
    media_job_id: Mapped[str | None] = mapped_column(String(36), index=True)
    platform: Mapped[str] = mapped_column(String(20), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class ProviderCost(Base):
    """A provider's billed cost for one day and model or line item, read from its cost API."""

    __tablename__ = "provider_costs"
    __table_args__ = (UniqueConstraint("provider", "day", "item"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(20))
    day: Mapped[date] = mapped_column(Date, index=True)
    item: Mapped[str] = mapped_column(String(200), default="")
    amount_usd: Mapped[float] = mapped_column(Float, default=0)
    units: Mapped[dict] = mapped_column(JSON, default=dict)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ProviderSync(Base):
    """When each provider's costs were last read, and why they couldn't be."""

    __tablename__ = "provider_syncs"
    provider: Mapped[str] = mapped_column(String(20), primary_key=True)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
