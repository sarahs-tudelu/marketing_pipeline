import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLATFORMS = ("linkedin", "instagram", "facebook", "x", "tiktok")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", env_ignore_empty=True, hide_input_in_errors=True
    )
    environment: Literal["development", "production"] = "development"
    database_url: str = "sqlite:///./data/social.db"
    database_password: SecretStr = SecretStr("")
    secret_backend: Literal["none", "google"] = "none"
    secret_timeout_seconds: float = Field(default=15, ge=1, le=60)
    google_application_credentials: str = ""
    app_base_url: str = ""
    media_base_url: str = ""
    live_platforms: str = ""  # Comma separated; explicit live opt-in in addition to credentials.
    profiles_path: Path = Path("config/platforms.yaml")
    facts_path: Path = Path("config/facts.seed.yaml")
    storage_backend: Literal["local", "s3"] = "local"
    media_dir: Path = Path("data/media")
    watch_dir: Path = Path("data/inbox")
    s3_bucket: str = ""
    s3_endpoint_url: str = ""
    s3_region: str = "us-east-1"
    aws_access_key_id: SecretStr = SecretStr("")
    aws_secret_access_key: SecretStr = SecretStr("")
    token_encryption_key: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("")
    auth_mode: Literal["basic", "proxy", "dev"] = "dev"
    admin_username: str = ""
    admin_password: SecretStr = SecretStr("")
    proxy_auth_secret: SecretStr = SecretStr("")
    meta_api_version: str = "v26.0"
    meta_app_id: str = ""
    linkedin_api_version: str = "202609"
    x_api_version: Literal["2"] = "2"
    tiktok_api_version: Literal["v2"] = "v2"
    meta_system_user_token: SecretStr = SecretStr("")
    meta_page_id: str = ""
    instagram_user_id: str = ""
    linkedin_client_id: str = ""
    linkedin_client_secret: SecretStr = SecretStr("")
    linkedin_organization_id: str = ""
    x_client_id: str = ""
    x_client_secret: SecretStr = SecretStr("")
    x_include_links: bool = False
    tiktok_client_id: str = ""  # TikTok calls this client_key.
    tiktok_client_secret: SecretStr = SecretStr("")
    tiktok_verified_url_prefix: str = ""
    tiktok_transfer: Literal["file", "url"] = "file"
    tiktok_manual_assignee: str = "Tudelu social owner"
    ai_mode: Literal["fixture", "live"] = "fixture"
    caption_provider: Literal["anthropic"] = "anthropic"
    review_provider: Literal["anthropic"] = "anthropic"
    vision_provider: Literal["openai"] = "openai"
    image_provider: Literal["openai"] = "openai"
    video_provider: Literal["disabled", "http"] = "disabled"
    video_provider_url: str = ""
    video_provider_key: SecretStr = SecretStr("")
    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_model: str = "claude-opus-5-5"
    anthropic_api_version: str = "2026-10-21"
    anthropic_effort: Literal["high", "xhigh", "max"] = "xhigh"
    anthropic_max_tokens: int = Field(default=16000, ge=1024)
    openai_api_key: SecretStr = SecretStr("")
    openai_vision_model: str = "gpt-6-astra"
    openai_image_model: str = "gpt-image-2.5-sunburst-2026-09-08"
    openai_embedding_model: str = "text-embedding-3-small"
    embedding_provider: Literal["hash", "openai"] = "hash"
    disclosure_text: str = "AI-generated or AI-edited media."
    review_min_score: float = Field(default=0.9, ge=0, le=1)
    duplicate_threshold: float = Field(default=0.94, gt=0, le=1)
    duplicate_days: int = Field(default=30, ge=1)
    retry_max_attempts: int = Field(default=5, ge=1)
    retry_base_seconds: int = Field(default=60, ge=1)
    worker_interval_seconds: int = Field(default=15, ge=5)
    campaign_stagger_minutes: int = Field(default=20, ge=1)
    failure_pause_threshold: int = Field(default=3, ge=1)
    held_alert_threshold: int = Field(default=10, ge=1)
    expiry_alert_days: int = Field(default=14, ge=1)
    alert_slack_webhook: SecretStr = SecretStr("")
    alert_email_to: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = ""
    smtp_starttls: bool = True
    http_timeout_seconds: float = 30
    ai_timeout_seconds: float = 300
    max_upload_bytes: int = 100_000_000
    analytics_enabled: bool = False
    auto_advance: bool = True

    def __init__(self, **values):
        if "_env_file" not in values and os.environ.get("TUDELU_ENV_FILE"):
            values["_env_file"] = os.environ["TUDELU_ENV_FILE"]
        super().__init__(**values)

    @model_validator(mode="before")
    @classmethod
    def load_google_secrets(cls, values):
        from .cloud_secrets import resolve_references

        data = dict(values)
        sensitive = {
            name: data[name]
            for name, field in cls.model_fields.items()
            if field.annotation is SecretStr and name in data
        }
        resolved = resolve_references(
            sensitive,
            backend=data.get("secret_backend", "none"),
            timeout=float(data.get("secret_timeout_seconds", 15)),
            credentials_file=data.get("google_application_credentials") or None,
        )
        return data | resolved

    @property
    def database_dsn(self):
        from sqlalchemy.engine import make_url

        url = make_url(self.database_url)
        if self.database_password.get_secret_value():
            if url.get_backend_name() != "postgresql":
                raise ValueError("DATABASE_PASSWORD requires a PostgreSQL DATABASE_URL")
            url = url.set(password=self.database_password.get_secret_value())
        return url

    @model_validator(mode="after")
    def secure_production(self):
        if set(self.live_platforms.split(",")) - set(PLATFORMS) - {""}:
            raise ValueError("Unknown LIVE_PLATFORMS entry")
        if self.environment == "production" or self.live_platforms:
            if self.auth_mode == "dev" or not self.session_secret.get_secret_value():
                raise ValueError("Production/live requires authentication and SESSION_SECRET")
            if not self.token_encryption_key.get_secret_value():
                raise ValueError("Production/live requires TOKEN_ENCRYPTION_KEY")
        if self.auth_mode == "basic" and (
            not self.admin_username or not self.admin_password.get_secret_value()
        ):
            raise ValueError("Basic auth requires ADMIN_USERNAME and ADMIN_PASSWORD")
        if self.auth_mode == "proxy" and not self.proxy_auth_secret.get_secret_value():
            raise ValueError("Proxy authentication requires PROXY_AUTH_SECRET")
        return self

    def wants_live(self, platform: str) -> bool:
        return platform in self.live_platforms.split(",")


class Profile(BaseModel):
    max_length: int = Field(gt=0)
    hashtag_limit: int = Field(ge=0)
    allow_links: bool = True
    allow_mentions: bool = False
    tone: str
    lead_in: str
    target_per_day: int = Field(default=1, ge=1)
    hard_cap: int = Field(default=3, ge=1)
    documented_daily_limit: int | None = None
    operational_ceiling: int = Field(default=10, ge=1)
    timezone: str = "America/New_York"
    windows: list[list[str]] = [["09:00", "17:00"]]
    minimum_spacing_minutes: int = Field(default=180, ge=15)
    promotional_ratio: float = Field(default=0.7, ge=0, le=1)
    image_formats: list[str] = ["image/jpeg", "image/png"]
    image_max_bytes: int = 5_000_000
    video_max_bytes: int = 100_000_000
    min_width: int = 320
    min_height: int = 320
    max_width: int = 1920
    image_min_ratio: float = 0.8
    image_max_ratio: float = 1.91
    video_min_ratio: float = 0.5625
    video_max_ratio: float = 1.777778
    min_duration: float = 3
    max_duration: float = 60
    max_assets: int = 1

    @model_validator(mode="after")
    def limits(self):
        from datetime import time
        from zoneinfo import ZoneInfo

        ZoneInfo(self.timezone)
        if self.hard_cap > self.operational_ceiling or self.target_per_day > self.hard_cap:
            raise ValueError("Target <= hard cap <= operational ceiling required")
        if self.documented_daily_limit and self.hard_cap >= self.documented_daily_limit:
            raise ValueError("Cap must be strictly below documented limit")
        if not self.windows or any(time.fromisoformat(a) >= time.fromisoformat(b) for a, b in self.windows):
            raise ValueError("Windows must be nonempty same-day start/end pairs")
        return self


def profiles(settings: Settings) -> dict[str, Profile]:
    raw = yaml.safe_load(settings.profiles_path.read_text(encoding="utf-8"))
    result = {p: Profile.model_validate(raw[p]) for p in PLATFORMS}
    # These ceilings cannot be loosened via a YAML typo.
    ceilings = {"instagram": 49, "tiktok": 4, "x": 50, "linkedin": 10, "facebook": 10}
    lengths = {"instagram": 2200, "tiktok": 2200, "x": 280, "linkedin": 3000, "facebook": 5000}
    for name, profile in result.items():
        max_assets = {"instagram": 10, "tiktok": 1, "x": 4, "linkedin": 9, "facebook": 1}
        formats = {"image/jpeg"} if name == "instagram" else {"image/jpeg", "image/png"}
        minimum = 360 if name == "tiktok" else 320
        if (
            profile.hard_cap > ceilings[name]
            or profile.max_length > lengths[name]
            or profile.max_assets < 1
            or profile.max_assets > max_assets[name]
            or not set(profile.image_formats) <= formats
            or not 0 < profile.image_max_bytes <= 5_000_000
            or not 0 < profile.video_max_bytes <= 100_000_000
            or profile.min_width < minimum
            or profile.min_height < minimum
            or not profile.min_width <= profile.max_width <= 1920
            or not 0.8 <= profile.image_min_ratio <= profile.image_max_ratio <= 1.91
            or not 0.5625 <= profile.video_min_ratio <= profile.video_max_ratio <= 1.777778
            or not 3 <= profile.min_duration <= profile.max_duration <= 60
            or profile.hashtag_limit > 30
        ):
            raise ValueError(f"{name}: exceeds verified conservative envelope")
    return result


@lru_cache
def get_settings():
    return Settings()
