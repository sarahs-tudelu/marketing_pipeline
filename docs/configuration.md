# Configuration reference

Generated from `social/config.py` and `.env.example`. All variables are optional in offline development; blank values use defaults. Production/live startup requires authenticated administration, a session secret and an encryption key. Values are loaded from environment variables first, then the ignored local `.env`.

Actual client IDs, API keys, tokens, passwords and origin domains are deliberately unset. Version/model settings are pinned defaults, not credentials. `LIVE_PLATFORMS` is an explicit additional switch; missing connections still remain dry. Restart web and worker after changes.

For DigitalOcean, the ignored `.env` is generated from `deploy/droplet.env.example`. With `SECRET_BACKEND=google`, sensitive settings and OAuth client IDs accept `gsm://projects/PROJECT/secrets/NAME/versions/VERSION` references. ADC authenticates reads at startup; failed reads stop startup. `DATABASE_PASSWORD` is injected into a SQLAlchemy URL object, separate from the password-free `DATABASE_URL`. See [Droplet deployment](digitalocean.md) for the extra Compose variables and bootstrap credential file.

`TUDELU_ENV_FILE` is a process environment selector for the settings file (default `.env`); it is not a setting to place inside that file. Select `.env.local` for the offline demo. Compose additionally uses `LOCAL_ENV_FILE` to choose its development env file. The Droplet template uses `GCP_PROJECT_ID`, `GCP_ADC_HOST_PATH`, `TLS_EMAIL`, `ADMIN_ALLOWED_CIDRS` and `DATABASE_CA_HOST_DIR` for reference interpolation, credential/certificate mounts, HTTPS and source restrictions. Its database URL targets the existing DigitalOcean Managed PostgreSQL cluster with `sslmode=verify-full`.

| Environment variable | Default | Purpose / group note |
|---|---|---|
| `ENVIRONMENT` | `"development"` | Copy to .env locally. Never commit .env or put real secrets in this file. Every Settings variable is listed here; blank values use the documented default. Runtime: development or production. Production rejects missing login/encryption config. |
| `DATABASE_URL` | `"sqlite:///./data/social.db"` | Local SQLite. Compose overrides with PostgreSQL. URL-encode special password characters. |
| `DATABASE_PASSWORD` | `(unset secret)` | Optional PostgreSQL password kept separate from DATABASE_URL. Supports gsm:// references. |
| `SECRET_BACKEND` | `"none"` | none for local development; google resolves gsm:// references in sensitive settings. |
| `SECRET_TIMEOUT_SECONDS` | `15` | Secret Manager reads are bounded; failed/missing references stop startup. |
| `GOOGLE_APPLICATION_CREDENTIALS` | `(unset)` | Optional ADC JSON / Workload Identity Federation configuration path. Docker mounts it read-only. |
| `APP_BASE_URL` | `(unset)` | Create later: https://social.[company-domain] (no trailing slash). |
| `MEDIA_BASE_URL` | `(unset)` | Create later: https://media.[company-domain] (public, read-only media origin). |
| `LIVE_PLATFORMS` | `(unset)` | Explicit live opt-in: comma-separated linkedin,instagram,facebook,x,tiktok; empty = all dry. |
| `PROFILES_PATH` | `config/platforms.yaml` | Versioned platform profiles and initial facts (admin saves immutable DB versions). |
| `FACTS_PATH` | `config/facts.seed.yaml` | Related configuration in the group above. |
| `STORAGE_BACKEND` | `"local"` | local or s3. Original bytes, including C2PA/IPTC, are kept unchanged. |
| `MEDIA_DIR` | `data/media` | Related configuration in the group above. |
| `WATCH_DIR` | `data/inbox` | Drop media then a JSON manifest here; see README for the manifest format. |
| `S3_BUCKET` | `(unset)` | S3-compatible bucket name. Droplet deployment can serve a private Spaces bucket through MEDIA_BASE_URL. |
| `S3_ENDPOINT_URL` | `(unset)` | Regional S3 API origin, e.g. https://nyc3.digitaloceanspaces.com (not a bucket/CDN URL). |
| `S3_REGION` | `"us-east-1"` | Bucket region, e.g. nyc3 for Spaces. |
| `AWS_ACCESS_KEY_ID` | `(unset secret)` | Spaces access key ID; accepts a gsm:// reference with SECRET_BACKEND=google. |
| `AWS_SECRET_ACCESS_KEY` | `(unset secret)` | Spaces secret key; accepts a gsm:// reference with SECRET_BACKEND=google. |
| `TOKEN_ENCRYPTION_KEY` | `(unset secret)` | Required for storing tokens: Fernet key, or comma-separated new,old keys during rotation. |
| `SESSION_SECRET` | `(unset secret)` | Required for production/live: random 32+ byte session signing secret. |
| `AUTH_MODE` | `"dev"` | dev = loopback only. basic = internal/VPN. proxy = trusted SSO proxy with shared secret. |
| `ADMIN_USERNAME` | `(unset)` | Related configuration in the group above. |
| `ADMIN_PASSWORD` | `(unset secret)` | Related configuration in the group above. |
| `PROXY_AUTH_SECRET` | `(unset secret)` | Proxy must strip incoming auth headers and set X-Authenticated-User and X-Tudelu-Proxy-Secret. |
| `META_API_VERSION` | `"v26.0"` | Verified/pinned platform API versions; review docs before updating. |
| `META_APP_ID` | `(unset)` | Optional Meta App ID. Not used for publishing: Facebook videos upload as Reels with the Page token. |
| `LINKEDIN_API_VERSION` | `"202609"` | Related configuration in the group above. |
| `X_API_VERSION` | `"2"` | Related configuration in the group above. |
| `TIKTOK_API_VERSION` | `"v2"` | Related configuration in the group above. |
| `META_SYSTEM_USER_TOKEN` | `(unset secret)` | Automatically validated/imported at web and worker startup when all three values are set. |
| `META_PAGE_ID` | `(unset)` | Related configuration in the group above. |
| `INSTAGRAM_USER_ID` | `(unset)` | Related configuration in the group above. |
| `LINKEDIN_CLIENT_ID` | `(unset)` | Three-legged LinkedIn OAuth. Organization numeric ID, not a member ID. OAuth client IDs also accept gsm:// references with SECRET_BACKEND=google. |
| `LINKEDIN_CLIENT_SECRET` | `(unset secret)` | Related configuration in the group above. |
| `LINKEDIN_ORGANIZATION_ID` | `(unset)` | Related configuration in the group above. |
| `X_CLIENT_ID` | `(unset)` | X confidential web app; OAuth 2.0 PKCE. offline.access is requested. Client ID accepts a literal value or a gsm:// reference with SECRET_BACKEND=google. |
| `X_CLIENT_SECRET` | `(unset secret)` | Related configuration in the group above. |
| `X_INCLUDE_LINKS` | `false` | Keep links off unless desired; current billing must be checked in the X Console. |
| `TIKTOK_CLIENT_ID` | `(unset)` | TikTok calls the client ID a client_key. Only inbox upload is implemented. Client ID accepts a literal value or a gsm:// reference with SECRET_BACKEND=google. |
| `TIKTOK_CLIENT_SECRET` | `(unset secret)` | Related configuration in the group above. |
| `TIKTOK_VERIFIED_URL_PREFIX` | `(unset)` | Optional verified HTTPS domain/prefix; required when transfer=url. |
| `TIKTOK_TRANSFER` | `"file"` | file or url. File upload works without verified public media prefix. |
| `TIKTOK_MANUAL_ASSIGNEE` | `"Tudelu social owner"` | Named internal owner mentioned in the completion alert. |
| `AI_MODE` | `"fixture"` | fixture = offline deterministic demo; live = actual AI calls with configured keys. |
| `CAPTION_PROVIDER` | `"anthropic"` | Provider choices requested by Tudelu. These interfaces can be replaced independently. |
| `REVIEW_PROVIDER` | `"anthropic"` | Related configuration in the group above. |
| `VISION_PROVIDER` | `"openai"` | Related configuration in the group above. |
| `IMAGE_PROVIDER` | `"runway"` | Related configuration in the group above. |
| `VIDEO_PROVIDER` | `"runway"` | Runway is the default video provider. http retains the legacy neutral gateway contract. |
| `RUNWAY_API_KEY` | `(unset secret)` | Runway media generation; paste a Runway API key here (or a gsm:// reference). |
| `RUNWAY_API_VERSION` | `"2024-11-06"` | Official Runway API version and supported model identifiers. |
| `RUNWAY_IMAGE_MODEL` | `"gen4_image"` | Related configuration in the group above. |
| `RUNWAY_VIDEO_MODEL` | `"gen4.5"` | Related configuration in the group above. |
| `RUNWAY_EDIT_MODEL` | `"aleph2"` | Related configuration in the group above. |
| `RUNWAY_AUDIO_MODEL` | `"seed_audio"` | Related configuration in the group above. |
| `RUNWAY_TASK_TIMEOUT_SECONDS` | `3600` | Maximum saved-task wait and number of background jobs advanced per worker tick. |
| `MEDIA_JOBS_PER_TICK` | `4` | Related configuration in the group above. |
| `VIDEO_PROVIDER_URL` | `(unset)` | Related configuration in the group above. |
| `VIDEO_PROVIDER_KEY` | `(unset secret)` | Related configuration in the group above. |
| `ANTHROPIC_API_KEY` | `(unset secret)` | Claude captions + independent fact/brand review. xhigh maps the requested extra effort. |
| `ANTHROPIC_MODEL` | `"claude-opus-5-5"` | Related configuration in the group above. |
| `ANTHROPIC_API_VERSION` | `"2026-10-21"` | Related configuration in the group above. |
| `ANTHROPIC_EFFORT` | `"xhigh"` | Related configuration in the group above. |
| `ANTHROPIC_MAX_TOKENS` | `16000` | Includes thinking and answer. Incomplete results are held, never accepted silently. |
| `OPENAI_API_KEY` | `(unset secret)` | OpenAI API key (a ChatGPT subscription alone is not an API credential). |
| `OPENAI_VISION_MODEL` | `"gpt-6-astra"` | Related configuration in the group above. |
| `OPENAI_IMAGE_MODEL` | `"gpt-image-2.5-sunburst-2026-09-08"` | Related configuration in the group above. |
| `OPENAI_EMBEDDING_MODEL` | `"text-embedding-3-small"` | Related configuration in the group above. |
| `EMBEDDING_PROVIDER` | `"hash"` | hash = offline similarity fixture; openai required for live publishing. |
| `REVIEW_MIN_SCORE` | `0.9` | Independent reviewer acceptance and duplicate similarity thresholds (0..1). |
| `DUPLICATE_THRESHOLD` | `0.94` | Related configuration in the group above. |
| `DUPLICATE_DAYS` | `30` | Related configuration in the group above. |
| `RETRY_MAX_ATTEMPTS` | `5` | Durable delayed retries. Policy/validation/ambiguous public-write errors are terminal. |
| `RETRY_BASE_SECONDS` | `60` | Related configuration in the group above. |
| `WORKER_INTERVAL_SECONDS` | `15` | Single APScheduler worker, durable DB state. Polling does not create per-post memory jobs. |
| `CAMPAIGN_STAGGER_MINUTES` | `20` | Related configuration in the group above. |
| `FAILURE_PAUSE_THRESHOLD` | `3` | Related configuration in the group above. |
| `HELD_ALERT_THRESHOLD` | `10` | Related configuration in the group above. |
| `EXPIRY_ALERT_DAYS` | `14` | Related configuration in the group above. |
| `ALERT_SLACK_WEBHOOK` | `(unset secret)` | Optional alert channels; empty means local admin/log outbox only. |
| `ALERT_EMAIL_TO` | `(unset)` | Related configuration in the group above. |
| `SMTP_HOST` | `(unset)` | Related configuration in the group above. |
| `SMTP_PORT` | `587` | Related configuration in the group above. |
| `SMTP_USERNAME` | `(unset)` | Related configuration in the group above. |
| `SMTP_PASSWORD` | `(unset secret)` | Related configuration in the group above. |
| `SMTP_FROM` | `(unset)` | Related configuration in the group above. |
| `SMTP_STARTTLS` | `true` | Related configuration in the group above. |
| `HTTP_TIMEOUT_SECONDS` | `30` | Network/AI timeouts and maximum ingestion size (bytes). |
| `AI_TIMEOUT_SECONDS` | `300` | Related configuration in the group above. |
| `MAX_UPLOAD_BYTES` | `100000000` | Related configuration in the group above. |
| `ANALYTICS_ENABLED` | `false` | Phase 2 read-only metrics. TikTok needs reconnect with video.list; Meta insights scopes required. |
| `AUTO_ADVANCE` | `true` | Advance drafts through generation, gate and scheduling on worker ticks. |
| `POSTGRES_PASSWORD` | (unset secret) | Compose-only PostgreSQL password; use a random alphanumeric value. |

## Platform profile fields

`PROFILES_PATH` is YAML keyed by `linkedin`, `instagram`, `facebook`, `x`, `tiktok`. The following defaults apply unless a platform overrides them. Required fields have no default. Caps, caption length, asset counts and supported media envelopes are validated against conservative code ceilings; lower limits are configurable. Expanding the supported format envelope requires another official-documentation review.

| Field | Default |
|---|---|
| `max_length` | `required` |
| `hashtag_limit` | `required` |
| `allow_links` | `true` |
| `allow_mentions` | `false` |
| `tone` | `required` |
| `lead_in` | `required` |
| `target_per_day` | `1` |
| `hard_cap` | `3` |
| `documented_daily_limit` | `null` |
| `operational_ceiling` | `10` |
| `timezone` | `"America/New_York"` |
| `windows` | `[["09:00", "17:00"]]` |
| `minimum_spacing_minutes` | `180` |
| `promotional_ratio` | `0.7` |
| `image_formats` | `["image/jpeg", "image/png"]` |
| `image_max_bytes` | `5000000` |
| `video_max_bytes` | `100000000` |
| `min_width` | `320` |
| `min_height` | `320` |
| `max_width` | `1920` |
| `image_min_ratio` | `0.8` |
| `image_max_ratio` | `1.91` |
| `video_min_ratio` | `0.5625` |
| `video_max_ratio` | `1.777778` |
| `min_duration` | `3` |
| `max_duration` | `60` |
| `max_assets` | `1` |
| `image_width` | `1080` |
| `image_height` | `1080` |
| `video_width` | `1280` |
| `video_height` | `720` |

Windows are same-day local clock pairs such as `[[09:00, 12:00], [14:00, 17:00]]`; quote time values in YAML. Overnight windows must be split into same-day ranges. Use IANA time zones. Targets are calendar-day preferences; hard caps use rolling 24-hour windows. Promotional mix uses a seven-day trailing history rounded up for small samples.

Default hard caps: LinkedIn 3, Instagram 5, Facebook 3, X 3, TikTok 3. All targets are 1/day. Platform YAML overrides include Instagram JPEG-only images, TikTok 360-pixel minimum width/height, image-only carousel counts, and distinct platform lead-ins. Native account-specific limits may be lower and always take precedence.

## Provider gateway and credentials

Claude uses official Messages with adaptive thinking and `output_config.effort=xhigh`. Runway uses official asynchronous image/video/audio tasks. OpenAI provides Responses vision and embeddings; legacy image generation remains optional. `AI_MODE=fixture` is offline; live publication additionally requires live AI checks and semantic embeddings. Media Studio workflows and job recovery are in `docs/runway.md`; connection steps are in `README.md`.

Meta credentials are automatically validated/imported from the three configured environment values at web/worker startup, or entered through Connections; all persisted tokens and live response checkpoints use Fernet encryption. X, LinkedIn and TikTok tokens come from OAuth callbacks. Do not place access/refresh tokens in the YAML profiles or facts files. Secret rotation accepts comma-separated Fernet keys, newest first; run `tudelu rotate-tokens` before removing an old key.
