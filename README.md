# Tudelu Social Studio

An internal, official-API publishing pipeline for LinkedIn company pages, Instagram professional accounts, Facebook Pages, X and TikTok inbox uploads. It runs completely offline in dry-run mode with synthetic seed media and a demo-only facts sheet. Live publication requires explicit configuration and validated account connections.

The implementation uses FastAPI, SQLAlchemy/Alembic, PostgreSQL (SQLite locally), APScheduler, httpx and local/S3 media storage. PyAV inspects media without rewriting it. There are no browsers, scrapers, private APIs or engagement automation in the application.

DigitalOcean deployment is configured in [compose.droplet.yaml](compose.droplet.yaml) for your existing Managed PostgreSQL cluster, with [verified TLS, Google Secret Manager setup and operations](docs/digitalocean.md). The current ignored `.env` contains deployment placeholders and `gsm://` references; `.env.local` preserves the offline demo. The managed database user's existing password is loaded from Secret Manager. For local development in this checkout, set `TUDELU_ENV_FILE=.env.local`.

## Run locally

Use Python 3.12 or newer; the Docker image uses Python 3.12. This minimum accommodates the current PyAV and NumPy wheels. The supplied dependency lock reflects the verified development environment. On Windows:

```powershell
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.lock
.venv/Scripts/python -m pip install -e '.[dev]'
Copy-Item .env.example .env.local
$env:TUDELU_ENV_FILE = '.env.local'
.venv/Scripts/tudelu init
.venv/Scripts/tudelu demo
.venv/Scripts/uvicorn social.app:app --host 127.0.0.1 --port 8000 --no-access-log
```

On Linux/macOS use `.venv/bin/` instead of `.venv/Scripts/` and export `TUDELU_ENV_FILE=.env.local`. Open <http://localhost:8000>. In a second terminal, set the same environment-file selector and run `tudelu worker` from the virtual environment. The worker advances drafts, runs checks, reserves slots, publishes due posts, refreshes tokens and delivers queued alerts. Set `AUTO_ADVANCE=false` to step through generation/check/schedule in the UI for a screen recording.

`tudelu demo` uses a virtual clock to process the real scheduled slots immediately, so no waiting for tomorrow's slot is needed. It is idempotent for the named demo campaign. Four platforms finish as simulated `published`; TikTok finishes as `awaiting_manual`. Fake post IDs have no public links. The JSON result is saved at `data/demo-report.json`; inspect the per-post Request audit in the app for payloads. The seed card and four-second H.264 video are generated locally by `social/seed.py`, depict no product, and are permanently marked demo-only.

No credentials are needed for these commands. `AI_MODE=fixture` calls neither Anthropic nor OpenAI; model and vision checks explicitly report simulated judgments. Local development login accepts loopback clients only. Never expose that mode through a public reverse proxy.

## Docker Compose

For local Docker, copy `.env.example` to `.env.local`. Generate a random alphanumeric `POSTGRES_PASSWORD` and set `AUTH_MODE=basic`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`, and `SESSION_SECRET` in that file. Docker requests arrive from the container network, so the loopback-only development login is intentionally unsuitable. Keep the Droplet `.env` separate. Then:

```sh
docker compose --env-file .env.local up --build -d
docker compose --env-file .env.local exec web tudelu seed
docker compose --env-file .env.local exec web tudelu demo
docker compose --env-file .env.local logs -f worker
```

Compose runs migrations before the web/worker, binds the UI to host loopback, and shares a runtime media volume. PostgreSQL is not exposed on a host port. For production, run behind VPN/TLS or company SSO, use a secrets manager, and use S3/CDN media serving. Set `ENVIRONMENT=production`. Docker daemon execution was unavailable on the build workstation; local SQLite execution and mocked API tests were verified. See [verification](docs/verification.md).

## Content and facts

Upload original company or AI media through Asset library. Supply usage rights, product/campaign tags and expiry. AI media requires the model and prompt. Real customer content additionally needs `{"real_customer_content": true}` in tags, a permission checkbox and an evidence reference. Embedded C2PA/IPTC remains in the original bytes; SHA-256 integrity is checked before publishing. The app does not claim to cryptographically verify C2PA signatures. It never transcodes the stored original. Incompatible media is held; supply a new compliant rendition with its own provenance record.

For watched ingestion, write the media to `WATCH_DIR` first, then atomically place a JSON manifest last:

```json
{
  "filename": "approved-photo.jpg",
  "source": "company",
  "rights": "Tudelu-owned; promotional social usage approved",
  "tags": {"product": "product-id", "campaign": "campaign-id"},
  "customer_permission": false
}
```

Other supported manifest fields are `generator`, `prompt`, `rights_expire_at` (ISO UTC), `permission_reference`, and `demo_only`. Completed manifests are renamed `.done.json`; invalid ones become `.error.json` with an admin alert. Local-folder watching and S3 storage are implemented; a bucket event can download media/manifests into this watched folder. The service does not scan an arbitrary remote bucket for new files.

Maintain the facts sheet under Approved facts or import from `FACTS_PATH`. Each version is immutable and stored with a content hash. Existing posts retain their source version. Use a new `version` when updating. Replace the seed sheet with real, approved facts and `demo_only: false` before live use. Never change the seed's marker to pretend its content is production approved.

The schema supports products, approved fact IDs, exact copy variants, `claim|price|offer|testimonial` kinds, product tags, validity dates, price amount/currency, canonical URLs, platform hashtags, banned phrases, brand voice and required disclaimers. Prices and offers require start and end dates. Dates are rechecked at delivery; an expired offer is held even if it previously passed.

Claude chooses approved sentences/variants for the selected product and platform. The renderer adds the platform lead-in, approved links/hashtags, disclaimers and AI disclosure. Free-form factual paraphrases are rejected even if they cite a valid ID. Add approved variants to the facts sheet to expand copy variety. All platforms receive distinct text. A separate Claude request reviews grounding, combinations of claims and brand fit. For AI media, OpenAI vision checks text, logos, people and visible claims. Video vision uses one frame per second plus the last frame; it is a sampled review and cannot certify every frame. AI-generated depictions must not be represented as real customer content.

## AI configuration

The requested providers are implemented with httpx against official APIs:

| Task | Provider and default | Configuration |
|---|---|---|
| Caption selection | Claude Opus 5.5, adaptive thinking, `xhigh` effort | `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `ANTHROPIC_EFFORT` |
| Independent fact/brand review | Separate Claude request, same configured model/effort | `REVIEW_MIN_SCORE` |
| Image generation | OpenAI GPT Image 2.5 Sunburst, dated model ID | `OPENAI_API_KEY`, `OPENAI_IMAGE_MODEL` |
| Vision review | OpenAI GPT-6 Astra through Responses | `OPENAI_VISION_MODEL` |
| Semantic duplicate embeddings | OpenAI embeddings | `EMBEDDING_PROVIDER=openai`, `OPENAI_EMBEDDING_MODEL` |
| Video generation | Unselected/disabled | `VIDEO_PROVIDER=disabled` |

Set `AI_MODE=live` to use the configured providers. A paid call is made only when the relevant operation runs. Keys are never read from other projects or harvested from the machine. Incomplete/refused/malformed model output fails closed. `ANTHROPIC_MAX_TOKENS` includes thinking and the answer. No chain-of-thought is stored. Prompt/model provenance is stored for generated media.

The `ImageGenerator`, `VideoGenerator` and `CaptionGenerator` interfaces are in `social/providers.py`. A provider-neutral video gateway can be configured later with `VIDEO_PROVIDER=http`, `VIDEO_PROVIDER_URL`, and its secret key. Its contract is `POST {"prompt":"..."}` → `{"media_base64":"...","model":"provider/model-version"}` for an MP4. No commercial video provider was chosen. Existing company videos can publish today after configuration.

## Connect accounts later

Create `social.[company-domain]` for the application and `media.[company-domain]` for public immutable media. Set the actual HTTPS URLs in `APP_BASE_URL` and `MEDIA_BASE_URL`. Media URLs must work without cookies, access tokens, VPN or short-lived signed URLs; restrict the media origin to read-only serving and disable listings. Only the media origin should be publicly accessible without internal login.

Generate `TOKEN_ENCRYPTION_KEY` using `Fernet.generate_key()` and `SESSION_SECRET` using `secrets.token_urlsafe(48)`, saving directly to a secret manager or local ignored `.env`. Credentials, refresh tokens, OAuth PKCE verifiers and live API response checkpoints are encrypted at rest. Authorization headers and temporary upload secrets are redacted from request audits. Do not enable request-body logging or proxy access logs containing OAuth callback query strings.

**LinkedIn:** Register a web app and obtain the necessary Community Management access. Configure `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET`, and numeric `LINKEDIN_ORGANIZATION_ID`. Register exactly `APP_BASE_URL/oauth/linkedin/callback`. Connect from the admin page and authorize `w_organization_social r_organization_social rw_organization_admin`. The validation call requires the organization administrator role. Programmatic refresh tokens are available only to approved Marketing Developer Platform partners; without a returned refresh token, the app alerts for reconnection instead of pretending renewal is possible. `LINKEDIN_API_VERSION=202609` is sent on REST calls.

**X:** Register a confidential web app supporting OAuth 2.0. Set `X_CLIENT_ID` and `X_CLIENT_SECRET`; register exactly `APP_BASE_URL/oauth/x/callback`. Connect with `tweet.read tweet.write users.read media.write offline.access`. State is session-bound and single-use, with S256 PKCE. Access tokens are refreshed before expiry and the newly returned refresh token is persisted. Add prepaid API credits and any required app access in the X Console. `X_INCLUDE_LINKS=false` is the default; actual pricing is console-controlled, and a universal link surcharge was not established by the public billing guide.

**TikTok:** Enable Login Kit and Content Posting API upload scope `video.upload` plus `user.info.basic`. Set `TIKTOK_CLIENT_ID` to the TikTok client key and configure the secret. Register exactly `APP_BASE_URL/oauth/tiktok/callback`. Web Login Kit uses state and confidential-client exchange; PKCE is documented for mobile/desktop, not added as an unsupported web parameter. Use `TIKTOK_TRANSFER=file` or verify the exact public prefix in TikTok and configure `TIKTOK_TRANSFER=url`, `TIKTOK_VERIFIED_URL_PREFIX`. Only `/post/publish/inbox/video/init/` is used: there is no Direct Post implementation. Assign `TIKTOK_MANUAL_ASSIGNEE` and an alert channel.

**Meta:** There is no Meta OAuth redirect in this system. Create a System User in the company Business Portfolio, assign the company Page and Instagram account, and provision the appropriate publishing permissions. Use the Facebook Login API path with linked Instagram professional account. Enter the System User token, Page ID and Instagram user ID in Connections. The app obtains and validates the Page token and verifies the Instagram linkage before saving encrypted credentials. `META_APP_ID` is also needed for Facebook video uploads. Alternatively set the corresponding environment values and run `tudelu import-meta`. Publishing needs `pages_manage_posts`, `pages_read_engagement`, `instagram_basic`, `instagram_content_publish` and applicable Page access permissions (`pages_show_list`; Meta's Page guide additionally names `publish_video` for video and task/permission requirements). Business-assigned Instagram roles may require `ads_management`/`ads_read`; see the official verification notes rather than requesting unrelated engagement permissions. System User tokens are not refreshed with an invented OAuth endpoint: replace them if invalidated. App review, business verification, Page publishing authorization and platform entitlement requirements still apply.

Missing config means Not connected and dry run. A successful connect does not itself enable publication.

## Switch a platform to live

After approval to publish publicly, configure production authentication, HTTPS origins, encrypted credentials, actual company assets and approved non-demo facts. Set `AI_MODE=live`, `EMBEDDING_PROVIDER=openai`, and the two AI API keys. Connect/validate the target account. Set `LIVE_PLATFORMS=linkedin` (or a comma-separated subset) and restart web/worker. This is configuration-only; no adapter code changes are needed for supported formats.

Create fresh posts for live publication. Already scheduled dry-run posts remain dry even after credentials are added. Live posts fail closed if their account/configuration disappears or the connected account changes. All gates run again at delivery, including expiry, rights, integrity and approval digest. Fixtures and demo assets/facts cannot pass the live-readiness gate. Platform approvals and real-credential smoke tests are external prerequisites, not something the offline demo can prove.

## Operations

- **Kill switch:** Stop all publishing at the top of every admin page. The worker checks it before each HTTP step. It cannot recall a request already accepted by a platform. Resuming preserves queued work.
- **Platform pause:** Pause/resume one account on Overview. A policy/permission rejection pauses immediately; repeated failures pause at `FAILURE_PAUSE_THRESHOLD`. Authentication refresh failures also pause and alert. Fix the issue before resuming.
- **Held queue:** Open Needs attention, inspect stored check reasons, correct fact selections/assets/facts, and choose Recheck & approve. This runs all gates again; human approval cannot override a failing check. Reject marks the post failed.
- **TikTok:** After confirmed inbox delivery, open the TikTok inbox notification, paste the supplied caption, enable the AI-generated label when instructed, and finish in the app. The post remains `awaiting_manual` until Check TikTok completion receives `PUBLISH_COMPLETE`. Simulated inbox entries can never be confirmed public. TikTok may withhold a public post ID until moderation completes.
- **Retries:** Safe transient failures get exponential delayed retries, honoring `Retry-After` and X reset headers. Policy/validation errors do not retry. Successful upload steps are durable checkpoints. An uncertain public POST (timeout, crash after dispatch, or HTTP 5xx) becomes failed and requires reconciliation. These APIs do not share a universal native idempotency mechanism; the system does not promise impossible exactly-once delivery across a lost response. It prevents automatic duplicate publication by refusing to replay an uncertain publish. Inspect the account and audit before creating a replacement post.
- **Caps:** `config/platforms.yaml` defines daily targets, rolling 24-hour caps, time zone/windows, minimum spacing, promotional ratio and media/caption constraints. Reservations, publishing, delivered and TikTok-pending items count. Campaigns are staggered. Rolling counters are per stored account; timestamps are UTC and windows use IANA time zones. Default caps are 3/day except Instagram 5/day; TikTok remains below its five-pending-share limit. Targets are one/day. The seven-day promotional ratio rounds up for small samples. Instagram additionally queries its own limit endpoint. Never increase a cap without checking current official/account-specific limits.
- **Alerts:** Stored in the DB and displayed in admin. Add a Slack webhook and/or SMTP config to deliver. TikTok completion alerts contain the caption and AI-label instruction. Notification failures retry at most five times. Configure the designated channels before live use.
- **Backups/rotation:** Back up PostgreSQL, immutable media and encryption keys separately. For rotation, configure `TOKEN_ENCRYPTION_KEY=new,old`, run `tudelu rotate-tokens`, then remove the old key after validating backup/restore. Stop the worker during rotation.
- **Worker deployment:** One worker only. PostgreSQL advisory locking or a shared local file lock enforces that. Web processes do not run publishers. Durable state survives restart; APScheduler only drives ticks. Failed/ambiguous writes never silently reset to drafts.

The lifecycle is `draft → generated → checked → held | approved → scheduled → publishing → published | failed | awaiting_manual`. Audit records also capture corrective transitions (`held → draft/generated/failed`, scheduled/dispatch rechecks to held, and TikTok completion). Every check run remains in `checks`; every request stage remains in `requests`. Volatile status/limit checkpoints retain the latest response, while transition and gate history are append-only.

## Phase 2 analytics

Set `ANALYTICS_ENABLED=true` to collect official read-only per-post metrics at most once/day for recent published posts. Instagram needs `instagram_manage_insights`; Facebook needs `read_insights` and Page read permissions; LinkedIn uses `rw_organization_admin`; TikTok needs `video.list` and reconnection to grant it. X requests `public_metrics`. Missing insights permissions create an alert and do not initiate engagement actions.

Use Performance in admin or `tudelu analytics`. Metrics are platform-specific and not falsely equated. Recommendations compare engagement per view for the latest five versus previous five eligible live posts, mark limited samples, and never change caps or schedules automatically. Simulated metrics are excluded. Facebook video IDs and metric availability may depend on the current Page/video surface; unavailable metrics remain unavailable instead of being filled with invented zeros.

## Configuration and development

[`.env.example`](.env.example) lists every environment variable with comments. [Configuration reference](docs/configuration.md) lists defaults and profile fields. [Platform verification](docs/platform-verification.md) records current official references, conflicts and supported API flows. The source contains no real tokens or product claims.

Run `pytest`, `ruff check social tests migrations`, and `alembic check`. Tests use mocked HTTP, local fixtures and temporary databases. The end-to-end test actively forbids network requests. Live accounts, credentials, scopes, app-review approvals and production S3/SMTP are intentionally untested until provided.

Agent workflows are included at [content generation](.agents/skills/tudelu-content/SKILL.md) and [post review](.agents/skills/tudelu-post-review/SKILL.md). The adapter boundary in `social/adapters/base.py` allows a unified official posting provider to replace one adapter without changing generation, gates or scheduling. No automatic publishing permission is granted by an agent skill.
