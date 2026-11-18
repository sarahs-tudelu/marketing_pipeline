# Usage and costs

**Settings → Usage & costs** (`/admin/usage`) shows what the app's AI requests cost and how posts performed for that spend, over the last 7, 30 or 90 days. It's for admins; CRM editors can't open it.

## What the page shows

The broad picture comes first:

1. **Headline figures:** AI spend for the period (and the change on the period before), cost per published post (all AI spend divided by the live posts published, so media that wasn't posted counts too), views with the cost per 1,000 views, and the engagement rate (interactions per view).
2. **Costs:** spend by provider (each marked *Billed* or *Estimated*), by stage (media generation, planning and editing, captions, checks) and by platform, then spend per day. Hovering over or tabbing to a day shows its amount, and **Daily spend as a table** lists every day.
3. **Performance:** for live posts published in the period, by platform and by real project media against AI inspiration: posts, views, engagement, AI spend, cost per post and cost per 1,000 views. Views and interactions are each post's latest metrics from the platform (see *Phase 2 analytics* in the README); simulated posts are left out.

**Details** expand underneath: cost per post, with each post's requests by step and model; spend that didn't become a published post (held, failed or deleted posts, media jobs that never made a post, and work not tied to a post such as idea planning, library search and media descriptions); every step and model with its requests, tokens or credits and cost; billed totals against the app's records; provider connections; and the price table.

## Where the numbers come from

**Billed costs** come from each provider's own cost API, by day and model, and lead the totals:

| Provider | Endpoint | Credential |
|---|---|---|
| Runway | `POST /v1/organization/usage` (credits per model per day) and `GET /v1/organization` (credit balance) | `RUNWAY_API_KEY`, already configured |
| Anthropic | `GET /v1/organizations/analytics/cost` (Usage and Cost Analytics API, beta `analytics-usage-cost-2026-08-24`), grouped by model | `ANTHROPIC_API_KEY` when it's an organization admin's or owner's personal key that isn't scoped to a workspace; otherwise `ANTHROPIC_ADMIN_KEY`, an Admin API key (`sk-ant-admin…`) |
| OpenAI | `GET /v1/organization/costs`, grouped by line item | `OPENAI_ADMIN_KEY`, an Admin key (`sk-admin-…`) that an organization owner creates in the OpenAI platform under Settings → Organization → Admin keys |

`OPENAI_COST_PROJECT_ID` and `ANTHROPIC_COST_API_KEY_ID` limit those totals to this app's project or key; without them the whole organization's spend is shown. Runway's usage is the API project's own. The worker reads all three every `USAGE_SYNC_HOURS` hours (default 6) in live mode, and **Refresh billed costs** reads them at once. A provider that can't be read keeps its last totals and shows why under **Provider connections**. Anthropic's data can lag by a few hours and is revised until about a week after a month ends.

**The app's records** say what each request was for and which media job, post and platform it served, because the providers' reports can't be split that way. Every Runway task is recorded with the credits Runway reports charging for it (its estimate, when a finished task doesn't report one), and every Claude and OpenAI request with its token counts, priced from `social/resources/pricing.yaml`. Requests are written after the step that made them has saved its own work, so recording never changes what the pipeline saves. For a provider that isn't connected, the dashboard uses these records as the provider's total, marked *Estimated* (Runway's are its own reported charges). Runway tasks from before this release are filled in from Runway in the background, a few at a time.

Billed totals include everything the provider charged to the account, project or key, such as benchmark runs and other tools, so they can be higher than the app's records; **Billed costs and the app's records** compares them.

## Prices

`social/resources/pricing.yaml` holds the prices used for the app's records, checked on 2026-11-18 against each provider's pricing pages: Runway credits at $0.01; Claude Opus 5.5 at $4 input, $20 output, $0.20 cache reads and $5 cache writes per million tokens; GPT-6 Astra at $10 input, $1 cached input and $50 output; gpt-audio-1.5 at $2.50 text input, $10 text output, $32 audio input and $64 audio output; text-embedding-3-small at $0.02. A model missing from the table is recorded without a cost and counted under **Billed costs and the app's records**. Update the file when prices or models change, and rebuild web and worker.

## Deployment

Migration `f9c2a1e7d340` creates `usage_records`, `provider_costs` and `provider_syncs`. Apply it before starting the updated web and worker; the worker's new usage job reads billed costs and backfills Runway task costs.
