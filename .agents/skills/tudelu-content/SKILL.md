---
name: tudelu-content
description: Prepare factual, platform-specific promotional content and provenance records for the Tudelu Social Studio repository. Use when creating campaigns, captions or assets for this pipeline; excludes public publishing authorization and engagement automation.
---

Read the repository `README.md`, the selected immutable facts version, and `config/platforms.yaml` before preparing content.

Select company-approved assets or use the configured provider. Claude generates caption selections with adaptive thinking and `xhigh` effort; OpenAI generates images and reviews visual content. Video generation stays disabled unless the user selects a provider. Do not substitute a provider or read credentials from other projects. Demo execution uses fixtures without external AI calls.

Return caption parts as `[{"fact_id":"approved.id","text":"exact approved text or variant"}]`. Every factual sentence must match the cited fact or one of its approved variants. If the desired claim is absent, report it as missing; do not add it to the source of truth yourself. Use the renderer for platform lead-ins, canonical URLs, hashtags and required disclaimers. Do not append automatic AI-disclosure text to captions; preserve AI source flags and embedded provenance for platform labeling. Keep copy distinct across platforms and respect `X_INCLUDE_LINKS`.

Record source, model, prompt, product/campaign tags, rights and expiry when ingesting generated/edited media. Preserve original bytes and embedded provenance. AI output is never real customer evidence. Customer quotes require a real-customer tag, permission and its evidence reference.

Use draft/generate/check/schedule operations through the pipeline. A held result needs correction and rechecking. Do not bypass the gate, clear an idempotency checkpoint or relabel a demo asset as production material. Do not enable live configuration or publish publicly without the user's explicit authorization. TikTok remains inbox-only and requires the app completion step.
