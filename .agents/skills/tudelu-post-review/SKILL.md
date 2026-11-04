---
name: tudelu-post-review
description: Review Tudelu Social Studio posts against their versioned facts, media provenance, platform profile and stored checks. Use for held-post correction, disclosure audits and publishing readiness; excludes bypassing checks or authorizing public publication.
---

Read the post's stored facts version, fact parts, asset records, platform profile, check history and lifecycle. Use `README.md` and `docs/platform-verification.md` for operational boundaries and official references.

Audit each claim against its exact approved fact/variant and product, including implied price, offer dates, URLs and disclaimers. A valid fact ID alone does not justify arbitrary wording. Never invent product facts to make a post pass. Claude's independent factual review must use a separate call from generation with the configured extra effort.

Verify usage rights at the intended publication time, checksum integrity and AI source flags. AI-generated or significantly AI-edited media requires disclosure. Instagram uses `is_ai_generated` on the parent container; X uses `made_with_ai`; automatic caption disclosure is disabled. Facebook and LinkedIn retain embedded provenance, but native label detection is not guaranteed. Live Facebook AI-video posts must remain held until the adapter supports the required native label. TikTok's inbox API cannot set the label, so the completion alert must instruct the owner. OpenAI vision review must evaluate text, logos, people and visible claims; acknowledge sampled-video limitations.

Inspect URL resolution, caption/hashtag rules, duplicate embeddings, model score and media compatibility. Any failed or unavailable check holds the post. Approval means rerunning the gate, never overriding failure. Record correction reasons and preserve prior check/transition records.

For an uncertain publish result, inspect the request checkpoint and platform status before any replacement. Never reset or retry an ambiguous public write. Policy/auth errors require account correction and explicit resume of its pause. Do not perform public publishing, credential access or engagement actions as part of a review unless separately authorized by the user.
