# Build verification

Verified on 2026-11-02 in the supplied Windows workspace using Python 3.14.6. The project targets Python 3.12+ and the Dockerfile selects 3.12. No real platform or AI credentials were used; no public posts or external notifications were sent.

## Executed checks

- `pytest`: **108 passed**. One nonblocking Starlette warning recommends its newer optional TestClient HTTP transport; the requested httpx implementation remains in use.
- `ruff check social tests migrations scripts` and `ruff format`: passed.
- `alembic check`: no model/schema drift.
- Isolated SQLite migration `upgrade head → downgrade base → upgrade head → check`: passed, including closing connections and cleaning the temporary database.
- `pip check`: no dependency conflicts. `requirements.lock` records the resolved versions with a Windows-only marker for pywin32.
- `docker compose config --no-env-resolution --no-interpolate --quiet`: passed.
- Both `.agents/skills/*/SKILL.md` files passed the skill validator.
- Configuration generator verified that `.env.example` covers all 82 application settings and documents all 27 platform profile fields, plus the Compose database password.
- Local Uvicorn startup, dashboard, connections, queues, analytics and health returned HTTP 200. All five connections display Not connected. A separate APScheduler worker started and completed its first tick.

## End-to-end demo

`tudelu init` and `tudelu demo` exercised ingestion, immutable provenance, fact selection, distinct captions, automated gate checks, rolling-cap scheduling, campaign staggering and dry-run adapters. The E2E test forbids HTTP requests and confirms the recorded lifecycle for all five platforms.

| Platform | Simulated result | Recorded request stages |
|---|---|---:|
| LinkedIn | published | 4 |
| Instagram | published | 5 |
| Facebook | published | 1 |
| X | published | 4 |
| TikTok | awaiting_manual | 3 |

The runtime report is `data/demo-report.json`. Seed source and facts are committed project inputs; generated databases/media/reports are ignored runtime files. Repeating the demo does not create duplicate posts. A virtual clock advances the demo through real reserved slots without waiting overnight. In ordinary worker operation the actual clock controls delivery.

## Test scope

Gate tests cover grounding, fact-ID laundering, dates/prices, URLs and public media reachability, caption/hashtag/mention rules, banned claims, testimonials and permission, AI disclosure, rights, media compatibility, original-byte integrity, semantic/cross-platform duplicates, independent model review, vision rejection and live-readiness failures. Recent publication time is used for duplicate history even if a draft is old.

Scheduler tests cover rolling windows and their boundaries, future reservations, time windows/DST, spacing, promotional mix, staggering and retries delayed by another recent delivery. Kill-switch and automatic-pause behavior are tested, including unknown failures.

Adapter tests use mocked official API exchanges for all five platforms, image/video paths, native disclosure fields, 429, expired auth, policy rejection and transient errors. Durable acknowledgements recover without repeating network calls after a worker interruption. Uncertain irreversible writes are never blindly replayed. Model tests verify separate Claude review calls, extra effort, incomplete-output rejection, OpenAI request shapes and byte-preserving image storage. Watch-folder tests verify UTC normalization of rights expiry.

Post-operation concurrency tests use separate SQLite sessions and threads to exercise simultaneous generation, proving one provider call and one successful transition. They cover stale generation/review requests, rollback of pending stale captions, repeated form/API submissions and worker conflict handling. PostgreSQL `FOR UPDATE NOWAIT` SQL and lock-contention error handling are verified with mocked database responses; these tests do not connect to the managed production database.

OAuth/token tests exercise state/PKCE, binding, replay prevention, token encryption, refresh rotation and expiry alerts. Admin tests exercise authentication and CSRF. Phase 2 tests exercise read-only metrics requests, simulated-data marking, daily deduplication and suggestions that leave caps unchanged.

## Remaining external validation

Docker is installed, but its daemon did not respond. The Compose file was validated without running containers. PostgreSQL integration, Linux container startup, production S3/CDN serving, SMTP/Slack delivery and real platform/AI API calls have **not** been tested. These require the configured services or credentials and, for public publishing, the user's authorization.

Platform app review, approved scopes, account entitlements, domain verification and actual media retrieval cannot be established by mocks. Complete the connection and activation steps in `README.md` before a permitted live smoke test. Video generation has no selected commercial provider; existing videos and the offline fixture are supported. AI video review samples frames and does not certify every frame.

Official API evidence, conflicts with the brief and supported conservative limits are documented in [platform verification](platform-verification.md). Current documented flows are implemented; future incompatible platform changes may require adapter updates.

## DigitalOcean configuration update — 2026-11-03

Added a standalone Droplet Compose deployment for the existing DigitalOcean Managed PostgreSQL cluster, Caddy routing/TLS configuration, Google Secret Manager loader, reference-only `.env`, offline `.env.local` and a deployment preflight. The managed connection uses `sslmode=verify-full`, an explicit trust root, a bounded connect timeout and a password loaded separately from Secret Manager. No database container or database volume is included in the Droplet configuration. The loader uses official Google ADC/REST access with CRC32C verification, sanitized startup errors and no secret-value disk cache. App imports no longer fetch deployment secrets; they load during startup.

The expanded suite passes **153 tests** when `CADDY_BINARY` points to the official binary (151 tests plus two proxy integration checks). Managed database tests verify TLS and secret-password parameters reach psycopg without opening a connection, reject insecure/ambiguous URLs, and check CA-file/system-trust prerequisites. Caddy 2.11.7 was checksum-verified, its configuration validated, and its actual HTTP routing tested on loopback. Ruff and Compose configuration validation against an isolated copy of the deployment template pass. The currently edited root `.env` has a standalone IP/CIDR line that Compose rejects; this user configuration issue was reported without changing the file. The supplied template's placeholders fail offline preflight as intended. No real Google authentication, secret reads, managed database connections, DNS changes or Droplet deployment occurred. Container startup remains unverified in this local environment.

The Spaces update adds private-bucket media delivery through the Droplet. Tests use botocore Stubber with fake credentials to verify unchanged upload/download bytes, GET/HEAD, partial content, invalid ranges, unknown objects, permission/service failures, response sanitization and stream closure. Local media delivery still passes, and the real Caddy test checks the media rewrite, credential-header stripping, method restrictions and admin isolation. The deployment preflight accepts configured Spaces settings and rejects incomplete credentials or bucket/CDN endpoints. A malformed database URL now reports that certificate validation could not start. No real Spaces bucket was accessed, and production Spaces connectivity remains unverified. The user's edited `.env` was not changed by this update.
