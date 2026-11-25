# CRM access to Social Studio

The CRM workspace chooser links to Social Studio. Active, approved CRM accounts with the Admin or Root role can use the entire Studio. This includes restricted Admin accounts. Social Editor grants access to **New post from an idea** and the editor's own submissions, previews, and finished posts. Approval, publication, connections, and automation are admin actions.

## Assign an editor

In the CRM team permissions editor, assign the **Social Editor** permission role to the person's **non-admin** account. The role grants `social.edit_own`. When creating a new user, choose a non-admin workspace role explicitly. The CRM uses restricted Admin as the default when the workspace role is blank, which gives full Studio access. Removing the editor grant takes effect at the next protected request once the CRM reports the change.

The Studio records the authenticated CRM account ID and its display label at submission. A subsequent email change does not transfer ownership. Queued jobs, automatic campaign batches, replacements, retries, and reposts retain the original attribution. Older posts with no recorded account are visible to admins. Media ownership and the account that publishes on a social network are separate fields.

## Connect an environment

1. Release the matching CRM API and frontend changes. The CRM migration installs the Social Editor role bundle. Set `SOCIAL_STUDIO_URL` on the CRM frontend to the Studio's HTTPS origin. The link is available in **Switch workspace**.
2. Use a Studio hostname within the CRM session cookie's existing domain. For staging, use a hostname inside the staging cookie domain and the staging cookie name. Keep production and staging cookies separate.
3. Set `AUTH_MODE=crm`, `CRM_ACCESS_URL` to the full HTTPS CRM API URL ending in `/auth/social-studio/access`, and `CRM_SESSION_COOKIE` to the CRM's configured cookie name (production default `TUDELU_TOKEN`). Set `APP_BASE_URL` to the Studio origin. With CRM sign-in, approved admins and assigned Social Editors can open Studio from any network. The CRM verifies membership and role on each protected request.
4. Apply the Studio migration with the normal release procedure before starting the updated web and worker. It adds nullable attribution columns to existing posts. Studio sends only the named cookie to the configured endpoint and checks the CRM decision on each protected request. An unavailable CRM returns a temporary sign-in error.

For the production Tudelu hostnames, set `SOCIAL_STUDIO_URL=https://social.tudelu.com` in the CRM frontend environment. In `/opt/tudelu/.env` on the Studio Droplet, set:

```dotenv
AUTH_MODE=crm
APP_BASE_URL=https://social.tudelu.com
CRM_ACCESS_URL=https://api.tudelu.com/api/auth/social-studio/access
CRM_SESSION_COOKIE=TUDELU_TOKEN
MEDIA_BASE_URL=https://media.tudelu.com
```

The production CRM session cookie must cover `social.tudelu.com`. Follow the [Droplet release procedure](digitalocean.md#deploy-after-configuration) after editing the environment. To confirm the running web container picked up the change, run `docker compose -f compose.droplet.yaml exec -T web printenv AUTH_MODE CRM_ACCESS_URL CRM_SESSION_COOKIE` from `/opt/tudelu`.

For an existing installation already using CRM sign-in, pull the routing update and recreate the proxy with `docker compose -f compose.droplet.yaml --env-file .env up -d --no-deps --force-recreate proxy`. Compose passes the same `AUTH_MODE` to the proxy and web. When changing login modes, recreate both services together.

After deployment, verify one admin can open Connections, an editor can submit an idea and open its result, and another editor gets a missing-post response for the same post URL. Confirm that removing the editor role blocks a previously open tab on refresh.
