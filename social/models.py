from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
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
    rights_expire_at: Mapped[datetime | None] = mapped_column(DateTime)
    customer_permission: Mapped[bool] = mapped_column(Boolean, default=False)
    permission_reference: Mapped[str] = mapped_column(Text, default="")
    media: Mapped[dict] = mapped_column(JSON)
    demo_only: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


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
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    auth_error: Mapped[str] = mapped_column(Text, default="")


class Post(Base):
    __tablename__ = "posts"
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
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(36), unique=True, default=uid)
    remote_id: Mapped[str] = mapped_column(String(300), default="")
    live_url: Mapped[str] = mapped_column(Text, default="")
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str] = mapped_column(Text, default="")
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
