# Tudelu Social Studio

An internal, official-API publishing pipeline for LinkedIn company pages, Instagram professional accounts, Facebook Pages, X and TikTok inbox uploads. It runs completely offline in dry-run mode with synthetic seed media and a demo-only facts sheet. Live publication requires explicit configuration and validated account connections.

The implementation uses FastAPI, SQLAlchemy/Alembic, PostgreSQL (SQLite locally), APScheduler, httpx and local/S3 media storage. PyAV inspects media without rewriting it. There are no browsers, private APIs or engagement automation in the application. Media Studio reads the Tudelu homepage for a bounded brand-context snapshot when requested.

DigitalOcean deployment is configured in [compose.droplet.yaml](compose.droplet.yaml) for your existing Managed PostgreSQL cluster, with [verified TLS, Google Secret Manager setup and operations](docs/digitalocean.md). The current ignored `.env` contains deployment placeholders and `gsm://` references; `.env.local` preserves the offline demo. The managed database user's existing password is loaded from Secret Manager. For local development in this checkout, set `TUDELU_ENV_FILE=.env.local`.

For production media, [enable private DigitalOcean Spaces storage](docs/digitalocean.md#digitalocean-spaces-media-storage) with `STORAGE_BACKEND=s3`. Keys are loaded from Secret Manager; the app streams ingested assets through the existing media hostname with GET/HEAD and video byte ranges. The bucket can stay private, and both app/media DNS records continue to point to the Droplet. Existing local assets must be copied unchanged or re-uploaded when switching storage.

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

No credentials are needed for these commands. `AI_MODE=fixture` calls neither Runway, Anthropic nor OpenAI; model and vision checks explicitly report simulated judgments. Local development login accepts loopback clients only. Never expose that mode through a public reverse proxy.

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

Other supported manifest fields are `generator`, `prompt`, `rights_expire_at` (ISO UTC), `permission_reference`, and `demo_only`. Completed manifests are renamed `.done.json`; invalid ones become `.error.json` with an admin alert. Local-folder watching and S3 storage are implemented; a bucket event can download media/manifests into this watched folder. Files uploaded directly into the configured bucket can also be registered through Asset library ? Import bucket asset, with their rights and provenance. The service does not scan arbitrary remote objects automatically.

Maintain the facts sheet under Approved facts or import from `FACTS_PATH`. Each version is immutable and stored with a content hash. Existing posts retain their source version. Use a new `version` when updating. Replace the seed sheet with real, approved facts and `demo_only: false` before live use. Never change the seed's marker to pretend its content is production approved.

The schema supports products, approved fact IDs, exact copy variants, `claim|price|offer|testimonial` kinds, product tags, validity dates, price amount/currency, canonical URLs, platform hashtags, banned phrases, brand voice and required disclaimers. Prices and offers require start and end dates. Dates are rechecked at delivery; an expired offer is held even if it previously passed.

Claude writes conversational, persuasive copy with varied tone, angles, openings, sentence lengths and punctuation. Recent captions provide repetition context, never evidence for new claims. New captions use paragraph parts: `{"kind":"claim","text":"Faithful phrasing of an approved fact","fact_ids":["approved.id"]}` or `{"kind":"copy","text":"A nonfactual hook or invitation","fact_ids":[]}`. Every factual implication must be supported by its cited facts; a valid ID alone is insufficient. Prices, offers and testimonials retain exact approved wording/variants. A separate Claude call must review every part, including uncited hooks and calls to action, and return matching citations with support verdicts. Missing or failed verdicts hold the post regardless of its overall score. This semantic review is a model judgment, not a guarantee of factual accuracy. Offline fixtures accept only their deterministic wording; they cannot approve arbitrary paraphrases.

The renderer adds approved links/hashtags and required disclaimers, without automatic AI caption disclosure or a fixed lead-in for new captions. Older exact-text parts retain their original rendering. All platforms receive distinct text. For AI media, OpenAI vision checks text, logos, people and visible claims. Video vision uses one frame per second plus the last frame; it is a sampled review and cannot certify every frame. AI-generated depictions must not be represented as real customer content.

AI provenance flags, Instagram/X native flags, original embedded metadata and TikTok manual-label instructions remain active. Facebook and LinkedIn depend on platform detection of preserved metadata, which is not guaranteed. Live Facebook AI-video publishing is held because this adapter cannot set the required native label. 

The old `DISCLOSURE_TEXT` setting is ignored. Existing stored captions are unchanged until regenerated or saved through the fact-parts editor and rechecked; captions containing the removed automatic disclosure sentence fail exact-grounding checks before publication.

## AI configuration

The requested providers are implemented with httpx against official APIs:

| Task | Provider and default | Configuration |
|---|---|---|
| Caption selection | Claude Opus 5.5, adaptive thinking, `xhigh` effort | `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `ANTHROPIC_EFFORT` |
| Independent fact/brand review | Separate Claude request, same configured model/effort | `REVIEW_MIN_SCORE` |
| Image generation / references | Runway Gen-4 Image | `RUNWAY_API_KEY`, `IMAGE_PROVIDER=runway` |
| Vision review | OpenAI GPT-6 Astra through Responses | `OPENAI_VISION_MODEL` |
| Semantic duplicate embeddings | OpenAI embeddings | `EMBEDDING_PROVIDER=openai`, `OPENAI_EMBEDDING_MODEL` |
| Video generation / editing | Runway Gen-4.5 / Aleph 2 | `RUNWAY_API_KEY`, `VIDEO_PROVIDER=runway` |
| Audio generation / reference editing | Seed Audio through Runway | `RUNWAY_API_KEY`, `RUNWAY_AUDIO_MODEL` |

Set `AI_MODE=live` to use the configured providers. A paid call is made only when the relevant operation runs. Keys are never read from other projects or harvested from the machine. Incomplete/refused/malformed model output fails closed. `ANTHROPIC_MAX_TOKENS` includes thinking and the answer. No chain-of-thought is stored. Prompt/model provenance is stored for generated media.

Open **Media Studio** for a custom prompt, uploaded references, optional video soundtrack, and platform-specific output. Save a recurring campaign to rotate visual concepts, media formats, source references and promotional/editorial content once the selected accounts are live. Generated posts enter the existing caption, fact, media, duplicate and scheduling checks. Audio can be saved to the library or fitted to a video. The original provider output is preserved; JPEG/H.264-AAC publishing renditions are separate assets with linked provenance.

See [Runway setup and workflows](docs/runway.md) for the API-key slot, supported inputs, job recovery, campaign controls and deployment migration. The legacy OpenAI image provider and generic HTTP video gateway remain opt-in compatibility paths; Media Studio uses Runway. Claude captions/reviews and OpenAI visual review/embeddings retain their own keys.

## Connect accounts later

**Overview → Automatic posts** controls recurring campaign batches and automatic caption generation, checks and scheduling. The switch is stored in the database and takes effect without restarting services. Until changed in the UI, `AUTO_ADVANCE` is its default. The worker rereads it between automatic stages: a stage already running can finish, but later routine stages wait when the switch is off. Manual actions, explicit **Create now** workflows, queued media jobs and delivery of already scheduled posts continue. The global **Stop all publishing** and per-platform pause/live controls remain separate. Migration `c4e8b6a92017` adds this preference without changing the current effective setting; run migrations before updated web/worker containers.

**Overview → Give the next post an idea** accepts an admin's prompt, platforms, references, approved facts and usage rights. **Use for next automatic post** saves it until automation is on and the platform's media queue is available. **Create now** starts a prioritized workflow on the next available worker tick, including media, visual summaries, captions, checks and scheduling, even with routine automation off. Both actions create one post per selected platform and use the normal live/simulated delivery settings; they do not publish immediately or bypass holds. Track progress or remove a waiting idea in Media Studio.

Connected platforms have an **Enable live posting / Turn off live posting** switch in Connections and the Overview's Platform controls. The selection persists in the database and overrides `LIVE_PLATFORMS` for that platform; until first changed, the environment remains the default. Enabling requires authenticated app access and a validated connection. Pausing and the global stop control remain separate. Existing simulated reservations stay simulated; switching off blocks further live requests, including the next step of an in-progress delivery. A request already sent cannot be recalled. Deploy migration `b7d2e9a40316` before running this version of web/worker.

Create `social.[company-domain]` for the application and `media.[company-domain]` for public immutable media. Set the actual HTTPS URLs in `APP_BASE_URL` and `MEDIA_BASE_URL`. Media URLs must work without cookies, access tokens, VPN or short-lived signed URLs; restrict the media origin to read-only serving and disable listings. Only the media origin should be publicly accessible without internal login.

Generate `TOKEN_ENCRYPTION_KEY` using `Fernet.generate_key()` and `SESSION_SECRET` using `secrets.token_urlsafe(48)`, saving directly to a secret manager or local ignored `.env`. Credentials, refresh tokens, OAuth PKCE verifiers and live API response checkpoints are encrypted at rest. Authorization headers and temporary upload secrets are redacted from request audits. Do not enable request-body logging or proxy access logs containing OAuth callback query strings.

**LinkedIn:** Register a web app and obtain the necessary Community Management access. Configure `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET`, and numeric `LINKEDIN_ORGANIZATION_ID`. Register exactly `APP_BASE_URL/oauth/linkedin/callback`. Connect from the admin page and authorize `w_organization_social r_organization_social rw_organization_admin`. The validation call requires the organization administrator role. Programmatic refresh tokens are available only to approved Marketing Developer Platform partners; without a returned refresh token, the app alerts for reconnection instead of pretending renewal is possible. `LINKEDIN_API_VERSION=202609` is sent on REST calls.

**X:** Register a confidential web app supporting OAuth 2.0. Set `X_CLIENT_ID` and `X_CLIENT_SECRET`; register exactly `APP_BASE_URL/oauth/x/callback`. Connect with `tweet.read tweet.write users.read media.write offline.access`. State is session-bound and single-use, with S256 PKCE. Access tokens are refreshed before expiry and the newly returned refresh token is persisted. Add prepaid API credits and any required app access in the X Console. `X_INCLUDE_LINKS=false` is the default; actual pricing is console-controlled, and a universal link surcharge was not established by the public billing guide.

OAuth connections submit to a same-site handoff page before navigating to the provider. This keeps the existing `form-action 'self'` policy while avoiding Chrome blocking a cross-site form redirect and leaving the button on Working. The handoff also provides a Continue link when JavaScript is unavailable. With `SECRET_BACKEND=google`, OAuth client IDs accept `gsm://` references as well as literal IDs. Rebuild/recreate web and worker after deploying this change or updating their environment.

**TikTok:** Enable Login Kit and Content Posting API upload scope `video.upload` plus `user.info.basic`. Set `TIKTOK_CLIENT_ID` to the TikTok client key and configure the secret. Register exactly `APP_BASE_URL/oauth/tiktok/callback`. Web Login Kit uses state and confidential-client exchange; PKCE is documented for mobile/desktop, not added as an unsupported web parameter. Use `TIKTOK_TRANSFER=file` or verify the exact public prefix in TikTok and configure `TIKTOK_TRANSFER=url`, `TIKTOK_VERIFIED_URL_PREFIX`. Only `/post/publish/inbox/video/init/` is used: there is no Direct Post implementation. Assign `TIKTOK_MANUAL_ASSIGNEE` and an alert channel.

**Meta:** There is no Meta OAuth redirect in this system. Create a System User in the company Business Portfolio, assign the company Page and Instagram account, and provision the appropriate publishing permissions. Use the Facebook Login API path with linked Instagram professional account. Enter the System User token, Page ID and Instagram user ID in Connections. The app obtains and validates the Page token and verifies the Instagram linkage before saving encrypted credentials. `META_APP_ID` is also needed for Facebook video uploads. Alternatively set `META_SYSTEM_USER_TOKEN`, `META_PAGE_ID` and `INSTAGRAM_USER_ID` in the active `.env`; web and worker automatically import and validate them at startup. Restart/recreate both services after changes. Unchanged validated credentials are reused, token/account changes are revalidated, and failures appear in Connections and operations alerts without exposing secrets. An explicit UI disconnect survives restarts; use **Connect from environment** or `tudelu import-meta` to reconnect. Account pauses and `LIVE_PLATFORMS` are preserved. Publishing needs `pages_manage_posts`, `pages_read_engagement`, `instagram_basic`, `instagram_content_publish` and applicable Page access permissions (`pages_show_list`; Meta's Page guide additionally names `publish_video` for video and task/permission requirements). Business-assigned Instagram roles may require `ads_management`/`ads_read`; see the official verification notes rather than requesting unrelated engagement permissions. System User tokens are not refreshed with an invented OAuth endpoint: replace them if invalidated. App review, business verification, Page publishing authorization and platform entitlement requirements still apply.

Missing config means Not connected and dry run. A successful connect does not itself enable publication.

## Switch a platform to live

After approval to publish publicly, configure production authentication, HTTPS origins, encrypted credentials, actual company assets and approved non-demo facts. Set `AI_MODE=live`, `EMBEDDING_PROVIDER=openai`, and the Runway, Anthropic and OpenAI API keys. Connect/validate the target account. Set `LIVE_PLATFORMS=linkedin` (or a comma-separated subset) and restart web/worker. This is configuration-only; no adapter code changes are needed for supported formats.

Create fresh posts for live publication. Already scheduled dry-run posts remain dry even after credentials are added. Live posts fail closed if their account/configuration disappears or the connected account changes. All gates run again at delivery, including expiry, rights, integrity and approval digest. Fixtures and demo assets/facts cannot pass the live-readiness gate. Platform approvals and real-credential smoke tests are external prerequisites, not something the offline demo can prove.

## Operations

- **Kill switch:** Stop all publishing at the top of every admin page. The worker checks it before each HTTP step. It cannot recall a request already accepted by a platform. Resuming preserves queued work.
- **Platform pause:** Pause/resume one account on Overview. A policy/permission rejection pauses immediately; repeated failures pause at `FAILURE_PAUSE_THRESHOLD`. Authentication refresh failures also pause and alert. Fix the issue before resuming.
- **Held queue:** Open Needs attention, inspect stored check reasons, correct fact selections/assets/facts, and choose Recheck & approve. This runs all gates again; human approval cannot override a failing check. Reject marks the post failed.
- **Drafts:** Delete draft is available on the dashboard and draft detail page. It removes the draft from the workspace while retaining its audit history and asset files. Only posts still in `draft` can be deleted; a concurrent generation or status change blocks deletion. The terminal `deleted` status is never picked up by the worker.
- **Progress:** Generation, upload and other submitted forms show an animated status indicator and disable repeated submission. Returning with the browser's Back button restores the controls. Reduced-motion preferences are respected.
- **Session recovery:** Forms refresh the CSRF token before submission so a page left open past the one-hour session expiry can still submit. A failed refresh preserves entered values and files and leaves the action unsubmitted. On an older deployment, return from a CSRF error to the original page and reload it before retrying.
- **TikTok:** After confirmed inbox delivery, open the TikTok inbox notification, paste the supplied caption, enable the AI-generated label when instructed, and finish in the app. The post remains `awaiting_manual` until Check TikTok completion receives `PUBLISH_COMPLETE`. Simulated inbox entries can never be confirmed public. TikTok may withhold a public post ID until moderation completes.
- **Retries:** Safe transient failures get exponential delayed retries, honoring `Retry-After` and X reset headers. Policy/validation errors do not retry. Successful upload steps are durable checkpoints. An uncertain public POST (timeout, crash after dispatch, or HTTP 5xx) becomes failed and requires reconciliation. These APIs do not share a universal native idempotency mechanism; the system does not promise impossible exactly-once delivery across a lost response. It prevents automatic duplicate publication by refusing to replay an uncertain publish. Inspect the account and audit before creating a replacement post.
- **Caps:** `config/platforms.yaml` defines daily targets, rolling 24-hour caps, time zone/windows, minimum spacing, promotional ratio and media/caption constraints. Reservations, publishing, delivered and TikTok-pending items count. Campaigns are staggered. Rolling counters are per stored account; timestamps are UTC and windows use IANA time zones. Default caps are 3/day except Instagram 5/day; TikTok remains below its five-pending-share limit. Targets are one/day. The seven-day promotional ratio rounds up for small samples. Instagram additionally queries its own limit endpoint. Never increase a cap without checking current official/account-specific limits.
- **Alerts:** Stored in the DB and displayed in admin. Add a Slack webhook and/or SMTP config to deliver. TikTok completion alerts contain the caption and AI-label instruction. Notification failures retry at most five times. Configure the designated channels before live use.
- **Backups/rotation:** Back up PostgreSQL, immutable media and encryption keys separately. For rotation, configure `TOKEN_ENCRYPTION_KEY=new,old`, run `tudelu rotate-tokens`, then remove the old key after validating backup/restore. Stop the worker during rotation.
- **Worker deployment:** One worker only. PostgreSQL advisory locking or a shared local file lock enforces that. Web processes do not run publishers. Durable state survives restart; APScheduler only drives ticks. Failed/ambiguous writes never silently reset to drafts.
- **Concurrent admin actions:** Caption generation and checks claim the post before calling providers. PostgreSQL uses a row lock with `NOWAIT`; SQLite uses a transaction write lock. If the worker or another admin already owns or has advanced the post, the UI shows the latest post with a conflict notice; the API returns HTTP 409. Refresh the post instead of submitting the same action again. The worker skips conflicts without creating a scheduling alert. `AUTO_ADVANCE=false` leaves generation/check/schedule under manual control while scheduled publishing continues.

The lifecycle is `draft → generated → checked → held | approved → scheduled → publishing → published | failed | awaiting_manual`. Audit records also capture corrective transitions (`held → draft/generated/failed`, scheduled/dispatch rechecks to held, and TikTok completion). Every check run remains in `checks`; every request stage remains in `requests`. Volatile status/limit checkpoints retain the latest response, while transition and gate history are append-only.

## Phase 2 analytics

Set `ANALYTICS_ENABLED=true` to collect official read-only per-post metrics at most once/day for recent published posts. Instagram needs `instagram_manage_insights`; Facebook needs `read_insights` and Page read permissions; LinkedIn uses `rw_organization_admin`; TikTok needs `video.list` and reconnection to grant it. X requests `public_metrics`. Missing insights permissions create an alert and do not initiate engagement actions.

Use Performance in admin or `tudelu analytics`. Metrics are platform-specific and not falsely equated. Recommendations compare engagement per view for the latest five versus previous five eligible live posts, mark limited samples, and never change caps or schedules automatically. Simulated metrics are excluded. Facebook video IDs and metric availability may depend on the current Page/video surface; unavailable metrics remain unavailable instead of being filled with invented zeros.

## Configuration and development

[`.env.example`](.env.example) lists every environment variable with comments. [Configuration reference](docs/configuration.md) lists defaults and profile fields. [Platform verification](docs/platform-verification.md) records current official references, conflicts and supported API flows. The source contains no real tokens or product claims.

Run `pytest`, `ruff check social tests migrations`, and `alembic check`. Tests use mocked HTTP, local fixtures and temporary databases. The end-to-end test actively forbids network requests. Live accounts, credentials, scopes, app-review approvals and production S3/SMTP are intentionally untested until provided.

Optional browser checks: install `playwright` in your development environment and set `BROWSER_BINARY` to a local Chrome/Chromium executable before running `pytest tests/test_studio_ui.py`. These tests serve local fixtures through intercepted requests and cover loading states, duplicate submission, reduced motion, error recovery and delete confirmation. No platform account or AI provider is contacted. Set `UI_CAPTURE_DIR` to an ignored directory such as `data/ui-verification` to save screenshots.

Agent workflows are included at [content generation](.agents/skills/tudelu-content/SKILL.md) and [post review](.agents/skills/tudelu-post-review/SKILL.md). The adapter boundary in `social/adapters/base.py` allows a unified official posting provider to replace one adapter without changing generation, gates or scheduling. No automatic publishing permission is granted by an agent skill.
