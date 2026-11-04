# DigitalOcean Droplet deployment with Google Secret Manager

The project-root `.env` is now a **deployment configuration containing placeholders and secret references**, copied from [`deploy/droplet.env.example`](../deploy/droplet.env.example). It contains no API keys, passwords or private keys. Use [`compose.droplet.yaml`](../compose.droplet.yaml) as a standalone Compose file, not as an override for the local `compose.yaml`.

The configuration targets your existing **DigitalOcean Managed PostgreSQL** cluster. It has been prepared and tested locally; no Droplet, database, DNS record, Google project, IAM grant, secret or public deployment was created or changed. Replace the placeholders before deployment. Publishing and AI calls remain in fixture/dry-run mode.

## Routes and containers

| Setting | Supplied value | Deployment behavior |
|---|---|---|
| `APP_BASE_URL` | `https://social.example.invalid` | Replace with the admin hostname; Caddy terminates TLS and proxies to `web:8000`. |
| `MEDIA_BASE_URL` | `https://media.example.invalid` | Public media hostname on the Droplet; Caddy forwards checksum-named GET/HEAD requests to the app's local/Spaces media reader. |
| `DATABASE_URL` | Managed PostgreSQL URL with `REPLACE_DB_*` fields | Copy the existing cluster's host, port, username and database; use `sslmode=verify-full`, a trust root and `connect_timeout=10`. |
| `DATABASE_PASSWORD` | `gsm://projects/${GCP_PROJECT_ID}/secrets/tudelu-database-password/versions/1` | The existing managed database user's password, loaded by the app. |
| `DATABASE_CA_HOST_DIR` | `/etc/tudelu/database-certs` | Host directory mounted read-only at `/run/database-certs` in web, worker and migration containers. |

Caddy is the only service exposing host ports (TCP 80 and 443). The app origin requires both a permitted source network and application Basic authentication. OAuth callbacks use the same app origin and are reached by the authorizing user's browser. The media origin is public, read-only, and has no admin/API routes or directory listing. Caddy access logging and Uvicorn access logging are disabled so OAuth callback codes are not recorded in access logs. Application event logs remain JSON.

The Droplet deployment runs no PostgreSQL container and creates no database volume. Web, worker and the migration job connect directly to the managed cluster. A migration job must succeed before web and worker start; an unreachable database or invalid TLS/authentication stops that startup sequence. The web and single worker share the runtime volume for local media, watched-folder ingestion and demo reports. With Spaces enabled, media is stored in the bucket. The local development `compose.yaml` still provides its separate local PostgreSQL service.

## DigitalOcean Spaces media storage

The Droplet supports `STORAGE_BACKEND=s3` with a private Spaces Standard Storage bucket. Both `social` and `media` DNS records still point to the Droplet. The app signs requests to the regional Spaces API with its own credentials and streams ingested media through `MEDIA_BASE_URL`. The Spaces key never appears in public media URLs. This configuration does not use the Spaces CDN; media delivery passes through the Droplet and consumes its bandwidth.

Keep the bucket and its file listing private, without a public-read bucket policy. Create a dedicated limited-access Spaces key for that bucket with Read/Write/Delete permission (the available write-capable key permission). Store its two parts as `tudelu-spaces-access-key-id` and `tudelu-spaces-secret-access-key` in Google Secret Manager and grant the runtime identity access. The app only needs object reads/writes; it does not create buckets or modify permissions. [Spaces access keys](https://docs.digitalocean.com/products/spaces/how-to/manage-access/)

In the existing root `.env`, set these fields; use the actual bucket name and region instead of the example `nyc3`:

```env
STORAGE_BACKEND=s3
S3_BUCKET=YOUR_BUCKET_NAME
S3_ENDPOINT_URL=https://nyc3.digitaloceanspaces.com
S3_REGION=nyc3
AWS_ACCESS_KEY_ID=gsm://projects/${GCP_PROJECT_ID}/secrets/tudelu-spaces-access-key-id/versions/1
AWS_SECRET_ACCESS_KEY=gsm://projects/${GCP_PROJECT_ID}/secrets/tudelu-spaces-secret-access-key/versions/1
MEDIA_BASE_URL=https://media.tudelu.com
```

Use your selected media subdomain if different. `S3_ENDPOINT_URL` is the HTTPS regional API origin, without a bucket name, CDN hostname, path or trailing slash. `MEDIA_BASE_URL` remains a distinct HTTPS origin on the Droplet with no path or trailing slash. Keep the existing `MEDIA_DIR` and `WATCH_DIR` settings. [Official Python SDK configuration](https://docs.digitalocean.com/products/spaces/reference/aws-sdks/)

The public media route accepts GET/HEAD for supported checksum filenames recorded in the asset library, supports single byte ranges for video retrieval, and has no bucket-listing or write operation. Files are returned unchanged, including embedded C2PA/IPTC metadata. Anyone with an ingested asset's media URL can retrieve it, including while a post is held; this matches the pipeline's public media-hosting model. Do not ingest confidential files. Missing objects return 404; access or service failures return a generic, noncached 503.

Deploy this updated code and Caddy configuration before switching an older installation to `s3`. Rebuild the image, run the preflight, and recreate services with the Compose commands below. The preflight validates configuration without accessing the bucket. Existing local assets are not automatically migrated: copy the checksum-named files unchanged to the bucket root, or re-upload their original files through the app after switching. Do not delete the local copies until migration is verified.

If generating or uploading media fails with `NoSuchBucket`, compare the exact bucket name and region in DigitalOcean with `S3_BUCKET`, `S3_REGION` and the regional `S3_ENDPOINT_URL` in the Droplet's `/opt/tudelu/.env`. A successful configuration preflight does not prove that the bucket exists. The bucket name must not include a URL or folder. Check the running container's nonsecret settings:

```bash
sudo docker compose -f compose.droplet.yaml --env-file .env exec web python -c "import os; print({k: os.getenv(k) for k in ['STORAGE_BACKEND','S3_BUCKET','S3_REGION','S3_ENDPOINT_URL']})"
```

After correcting `.env`, recreate the application services so they receive the updated values; `docker compose restart` alone does not reload environment variables:

```bash
sudo docker compose -f compose.droplet.yaml --env-file .env up -d --no-deps --force-recreate web worker
```

Verify storage with a company-owned image upload before generating another image. A generation request may already have been charged if saving its result failed. The generation form displays safe provider/storage errors, preserves entered values, and does not automatically repeat paid generation requests.

## Get the managed database connection details

In DigitalOcean, open **Databases → your PostgreSQL cluster → Overview → Connection Details**. Select the intended database and user, choose the network, enable `verify-full`, and use **Connection parameters** to obtain the nonsecret host, port, username and database name. Use VPC network details only when the Droplet and cluster share that VPC; otherwise use the public endpoint with a trusted-source rule. [Official connection guide](https://docs.digitalocean.com/products/databases/postgresql/how-to/connect/)

Use the direct database endpoint, not a transaction-pooled connection: the worker holds a session advisory lock. Select a database intended for this app and a user allowed to run its schema migrations. The application does not automatically create a database or database user on the managed cluster.

For Standard Edition, download the cluster's CA certificate from the same Connection Details section. Put it on the Droplet under `DATABASE_CA_HOST_DIR`, for example `/etc/tudelu/database-certs/ca-certificate.crt`, readable by the application container. Configure:

```env
DATABASE_URL=postgresql+psycopg://YOUR_USER@YOUR_HOST:YOUR_PORT/YOUR_DATABASE?sslmode=verify-full&sslrootcert=/run/database-certs/ca-certificate.crt&connect_timeout=10
DATABASE_CA_HOST_DIR=/etc/tudelu/database-certs
```

Replace every `YOUR_*` field with the actual connection parameter. The existing `.env` intentionally uses `REPLACE_*` placeholders instead. Do not include the password in this URL. Store the **existing password of the selected database user** in the referenced Google secret; generating an unrelated new password will not work.

Advanced Edition uses the system trust store. Use `sslrootcert=system` instead of a file path, with `sslmode=verify-full`. Its libpq client must be version 16 or newer; the offline certificate preflight checks this. Keep `DATABASE_CA_HOST_DIR` present as an empty directory so the shared read-only mount exists. [Standard versus Advanced TLS settings](https://docs.digitalocean.com/products/databases/postgresql/how-to/connect/#connect-with-verify-full)

Compose sets `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt` so OpenSSL uses the Debian image's CA bundle for system trust, including when the database driver bundles its own OpenSSL. A downloaded Standard Edition CA still uses its explicit `sslrootcert` path. [PostgreSQL trust-root configuration](https://www.postgresql.org/docs/current/libpq-connect.html#LIBPQ-CONNECT-SSLROOTCERT)

On the cluster's **Network Access** tab, add the deployment Droplet as a trusted source. Allow the Droplet's outbound connection to the cluster's supplied port. No inbound database port needs to be exposed on the Droplet. [Managed database trusted sources](https://docs.digitalocean.com/products/databases/postgresql/how-to/secure/)

## Replace deployment placeholders

Set these in `.env`:

- `GCP_PROJECT_ID`: the actual project ID. Keep this definition above all references so dotenv interpolation resolves it.
- `APP_BASE_URL` / `MEDIA_BASE_URL`: distinct HTTPS origins with no path, port or trailing slash.
- `TLS_EMAIL`: the certificate contact address.
- `ADMIN_ALLOWED_CIDRS`: space-separated company VPN/office public egress CIDRs. The loopback defaults intentionally deny remote administration. Do not use `0.0.0.0/0` or `::/0`.
- `GCP_ADC_HOST_PATH`: an absolute path outside the repository on the Droplet, default `/etc/tudelu/google-adc.json`.
- `GOOGLE_APPLICATION_CREDENTIALS`: keep `/run/secrets/google_adc`, the read-only container mount.
- `DATABASE_URL`: the managed database's connection parameters with verified TLS, no password, and a bounded connect timeout.
- `DATABASE_CA_HOST_DIR`: the host certificate directory; for Standard Edition, replace the CA filename placeholder in `DATABASE_URL` with the downloaded certificate's actual filename.

Create A records for both hostnames pointing to the Droplet. Add AAAA records only if the Droplet actually serves IPv6. Use a supported Ubuntu LTS Droplet with Docker Engine and the Compose plugin installed from [Docker's official Ubuntu instructions](https://docs.docker.com/engine/install/ubuntu/).

Configure the [DigitalOcean Cloud Firewall](https://docs.digitalocean.com/products/networking/firewalls/how-to/configure-rules/) to allow SSH only from administrators, and public TCP 80/443 for media delivery and certificate issuance. Do not expose database or application-server ports. Permit outbound HTTPS for Google, platforms and AI APIs, outbound TCP to the managed database's port, plus DNS/time service as needed. Docker-published ports can bypass UFW rules, so use the cloud firewall and the Caddy app-origin source restriction together. [Docker firewall guidance](https://docs.docker.com/engine/install/ubuntu/#firewall-limitations)

This Caddy configuration expects direct client connections. If adding Cloudflare or another reverse proxy, explicitly configure and validate trusted proxy handling before changing the source-network policy; client-supplied `X-Forwarded-For` does not grant admin access.

## Google Secret Manager setup

Enable the Secret Manager API in the selected project. Provision a dedicated runtime identity and grant `roles/secretmanager.secretAccessor` on the individual secrets it needs. The application only calls the official v1 `access` endpoint and validates the returned CRC32C checksum. Missing secrets, denied access, authentication errors, invalid payloads and checksum failures stop startup with a sanitized error. [Access permissions and versions](https://cloud.google.com/secret-manager/docs/access-secret-version), [checksum verification](https://cloud.google.com/secret-manager/docs/samples/secretmanager-access-secret-version)

Create these secret names (or change the references to your naming scheme), with enabled version `1`:

| Secret name | Value |
|---|---|
| `tudelu-database-password` | The existing password of the selected DigitalOcean Managed PostgreSQL user; no trailing newline. |
| `tudelu-token-encryption-key` | A Fernet key, generated with `cryptography.fernet.Fernet.generate_key()`; preserve it for database recovery. |
| `tudelu-session-secret` | A strong random session-signing secret. |
| `tudelu-admin-password` | The internal administrator password. |
| `tudelu-anthropic-api-key` | The Claude API key. |
| `tudelu-openai-api-key` | The OpenAI API key. |
| `tudelu-spaces-access-key-id` | The Spaces access key ID; required when using Spaces. |
| `tudelu-spaces-secret-access-key` | The Spaces secret key; required when using Spaces. |

If only deploying the fixture demo, the two AI key settings can be blank until their secrets exist. Every nonempty `gsm://` reference is fetched at startup, even if its corresponding feature is not yet active. The core database/authentication/encryption secrets are required for production.

The template also shows optional references for LinkedIn/X/TikTok client secrets, the Meta System User token, Slack and SMTP. Leave those fields blank until configured. Client IDs and account IDs remain ordinary nonsecret settings. Access and refresh tokens obtained through Connections remain encrypted in PostgreSQL; they are not rewritten into `.env` or uploaded to Secret Manager.

Use numeric versions for controlled deployments, especially the DB password and Fernet key. The loader also supports `latest`/version aliases, but the Droplet preflight requires numeric versions for those two critical settings. Loaded values are held in process memory and are never written back to `.env`, a generated configuration file or logs. Reads are deduplicated within a settings load; there is no persistent secret cache. Restart processes to pick up changed references.

## Authenticate from DigitalOcean

Google does not automatically recognize a DigitalOcean Droplet as a Google workload. Supply Application Default Credentials (ADC). Google recommends Workload Identity Federation when an existing external identity provider can issue suitable credentials. If federation is unavailable, use an appropriately restricted service-account key. The implementation supports either ADC JSON format. It does not invent a DigitalOcean-native identity federation service. [Google guidance for other clouds](https://cloud.google.com/docs/authentication/set-up-adc-on-premises)

Place the ADC bootstrap file outside the repository at `/etc/tudelu/google-adc.json`. Do not put its contents in `.env` or commit it. On the Droplet, provision permissions equivalent to:

```sh
sudo install -d -m 0750 -o root -g 10001 /etc/tudelu
sudo install -m 0440 -o root -g 10001 /secure/source/google-adc.json /etc/tudelu/google-adc.json
sudo install -d -m 0755 /etc/tudelu/database-certs
```

For Standard Edition, also install the downloaded CA file into that directory, for example with `sudo install -m 0644 /secure/source/ca-certificate.crt /etc/tudelu/database-certs/ca-certificate.crt`. A CA certificate is public trust material, not a database password.

Use your actual secured source path. The app container runs as UID/GID 10001. Compose file secrets are bind mounts, so the original host file permissions matter. A WIF configuration that refers to a separate subject-token file also needs that file mounted read-only at the path named in its `credential_source`; arrange token renewal using the existing identity provider. [Compose secret mounts](https://docs.docker.com/compose/how-tos/use-secrets/)

The ADC file is the authentication bootstrap; storing it only inside Secret Manager would leave the Droplet unable to authenticate to retrieve it. Runtime provider keys, application secrets and the DB password are stored in Secret Manager.

## Deploy after configuration

Place the project in `/opt/tudelu`, transfer its reference-only `.env`, provision the ADC/trust files, and configure the managed cluster's trusted source. Keep `.env` excluded from version control. Review the startup commands on the target Droplet before running them:

```sh
cd /opt/tudelu
sudo chmod 600 .env
sudo docker compose -f compose.droplet.yaml --env-file .env config --quiet
sudo docker compose -f compose.droplet.yaml --env-file .env build
sudo docker compose -f compose.droplet.yaml --env-file .env run --rm --no-deps web python scripts/check_droplet_config.py --environment --check-adc-file --check-ca-files
sudo docker compose -f compose.droplet.yaml --env-file .env up -d
sudo docker compose -f compose.droplet.yaml --env-file .env ps
sudo docker compose -f compose.droplet.yaml --env-file .env logs --tail=100 web worker
```

The preflight checks references, routes, TLS parameters, source networks and file permissions without reading secret contents or making network calls. With `--check-ca-files`, it also validates the public CA file or the system-trust client support. Its failure on the supplied placeholders is intentional. With local Python dependencies installed, `python scripts/check_droplet_config.py --env-file .env` runs the same offline settings check before transfer.

For the seeded demo after startup:

```sh
sudo docker compose -f compose.droplet.yaml exec web tudelu seed
sudo docker compose -f compose.droplet.yaml exec web tudelu demo
```

The `restart: unless-stopped` services return when Docker restarts. Enable Docker at boot. A new application release still needs the migration job: run the Compose `up -d --build` workflow and confirm successful migrations. Do not scale the worker beyond one process. Maintain managed-database backups/recovery separately from the Droplet's runtime media volume and Fernet key; none of these are source-controlled files.

## Rotation and live activation

Changing a Secret Manager version does not change the managed PostgreSQL user's password. Coordinate rotation with that user's password change in DigitalOcean, store the new value, then update the version reference and restart affected services during maintenance. Likewise, preserve the old Fernet key during token rotation; the existing `new,old` key-ring procedure in the README still applies, with that key-ring stored as a Secret Manager value.

Keep `LIVE_PLATFORMS=` and `AI_MODE=fixture` until authorized to make real calls. Later, configure the real account apps, redirect URLs and platform approvals; then enable AI/live settings as documented in the README. Creating this deployment configuration does not authorize public publishing.

## Continue local development

`.env.local` contains the offline development defaults. The deployment `.env` deliberately cannot start until its placeholders and authentication are provided. Select the local file explicitly:

```powershell
$env:TUDELU_ENV_FILE = '.env.local'
.venv/Scripts/tudelu init
.venv/Scripts/uvicorn social.app:app --host 127.0.0.1 --port 8000 --no-access-log
```

Set the same variable in the worker terminal. On Linux/macOS use `TUDELU_ENV_FILE=.env.local` before each command. The local Docker Compose file defaults to `.env.local`; use `docker compose --env-file .env.local ...`. `LOCAL_ENV_FILE` can select a different local file. Do not merge the local and Droplet Compose files.

## Verification

Secret Manager tests mock authentication and HTTP. They cover reference parsing, placeholders, permissions/errors, checksum failures, deduplication, input precedence, secret redaction and DB password separation. Managed PostgreSQL tests verify TLS/timeout/password parameters reach psycopg, reject insecure/ambiguous URLs, check trust configuration, and verify the deployment has no bundled DB service. Caddy configuration and its real local routing are tested with the official Caddy binary: media GET/HEAD succeeds, admin/API access on the media host fails, source restrictions hold, and spoofed forwarded/identity headers do not grant access.

The local Docker daemon remains unavailable, so the Droplet images, managed PostgreSQL connection, real Google IAM/ADC, TLS certificate issuance and remote startup have not been exercised. No cloud changes, database connections or secret reads were made during configuration work.
