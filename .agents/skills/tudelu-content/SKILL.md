---
name: tudelu-content
description: Prepare factual, platform-specific promotional content and provenance records for the Tudelu Social Studio repository. Use when creating campaigns, captions or assets for this pipeline; excludes public publishing authorization and engagement automation.
---

Read the repository `README.md`, the selected immutable facts version, and `config/platforms.yaml` before preparing content.

Select company-approved assets or use the configured provider. Claude generates caption selections with adaptive thinking and `xhigh` effort; Runway generates images, videos, video edits and audio; OpenAI reviews visual content. Media Studio uses durable jobs, uploaded references and platform renditions. Recurring campaigns rotate concepts and wait for enabled live accounts. Do not substitute a provider or read credentials from other projects. Demo execution uses fixtures without external AI calls.

Write persuasive, conversational copy with varied tone, content angles, openings, sentence lengths and punctuation. Follow the style brief and avoid repeating recent captions; prior captions are never factual evidence. Return paragraph parts as `[{"kind":"copy","text":"A nonfactual hook or invitation","fact_ids":[]},{"kind":"claim","text":"Faithful phrasing of approved facts","fact_ids":["approved.id"]}]`. Every explicit or implied factual assertion needs the supporting IDs, including assertions embedded in questions or calls to action. Preserve the exact approved wording/variant for prices, offers and testimonials. If the desired claim is absent, report it as missing; do not add it to the source of truth yourself. Use the renderer for canonical URLs, hashtags and required disclaimers; new captions have no fixed lead-in. Do not append automatic AI-disclosure text to captions; preserve AI source flags and embedded provenance for platform labeling. Keep copy distinct across platforms and respect `X_INCLUDE_LINKS`. Every part must pass the independent factual review; fixture mode accepts only deterministic fixture wording.

Record source, model, prompt, product/campaign tags, rights and expiry when ingesting generated/edited media. Preserve original bytes and embedded provenance. AI output is never real customer evidence. Customer quotes require a real-customer tag, permission and its evidence reference.

Use draft/generate/check/schedule operations through the pipeline. A held result needs correction and rechecking. Do not bypass the gate, clear an idempotency checkpoint or relabel a demo asset as production material. Do not enable live configuration or publish publicly without the user's explicit authorization. TikTok remains inbox-only and requires the app completion step.
