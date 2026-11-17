# Runway time-to-acceptable benchmark

Run locally with python -m social.runway_benchmark. This does not change production
settings, schedule posts, import publishing credentials, or deploy anything. Each trial
gets a fresh SQLite database and media directory. The normal prompt writer, reference
selection, scene review/retries, soundtrack, assembly, caption, final gate, and automatic
visual replacements still run.

## Measurement

Start: the brief is queued against prepared source inputs. Stop: every check returned by
run_gate(..., live=True) passes for a finished video and caption. Human approval remains
outstanding. This is time to automated acceptance, not time to publication.
Initial input snapshot/setup are excluded. Provider queues, reference uploads, all attempts,
reviews, retries, music, assembly and final checks are included. All arms poll every five seconds.

Failed or timed-out trials have a null time_to_acceptable_seconds. Never average failure
durations into successful completion times. Report successes/trials and failure reasons
beside timing; small pilots cannot establish reliable averages or percentiles. Record concurrency.

The actual platform gate runs with live-readiness checks. For local output use a platform
such as Facebook video that uploads bytes. Instagram's live public-media URL check must
fail for local-only outputs; never mock it to pass. Delivery authorization, scheduling,
fresh production duplicate history and human approval remain publication prerequisites.

## Arms

| Name | Scene | Animation | Difference |
|---|---|---|---|
| baseline | gen4_image | gen4.5 | Current models and one-product-reference selection |
| gemini_pro | gemini_image3_pro | gen4.5 | No seed; native resolution |
| nano_banana | nano_banana_2.1 | gen4.5 | No seed; high thinking |
| gpt_sunburst | gpt_image_2_6_sunburst | gen4.5 | No seed; high reasoning/quality; ordinal references |
| veo | gen4_image | veo3.1 | Audio off; approved scene as first and last frame |
| veo_fast | gen4_image | veo3.1_fast | Same endpoints; faster model |
| three_references | gen4_image | gen4.5 | Three distinct, visually checked construction references |
| text_repair | gen4_image + conditional ideogram_4_5 | gen4.5 | Masked lettering repair followed by unchanged scene review |

Request a locked camera when using the same scene at both ends. Veo clips must be 4, 6
or 8 seconds. Include the pipeline's three-second logo card in finished duration.
Endpoint conditioning does not guarantee correct intermediate geometry.

Gen-4 accepts three images total, including style. The three-product-reference arm rejects
a fourth style input; use a one-scene trial. The current selector sends one construction
image, optionally plus a style scene, not two independent product photos. Larger input
limits do not prove higher structural fidelity.

Every arm receives the same brief/candidate pool. Native portrait sizes differ:
Gen-4 720:1280, Gemini Pro 768:1344, Nano Banana 768:1376, Sunburst 1088:1920.
Normal platform rendering follows. This compares usable configurations, not equal pixel counts.

Ideogram repairs only when independent diagnosis says ALL defects are background lettering,
and tight boxes exclude structural members/joints. Black edits, white preserves; masks cover
at most 15% of the scene. If this condition never occurs, report "repair not exercised".
Keep the original rejection, scene, diagnosis and repair task. The edit must pass the normal
scene check before animation. Interrupted repair tasks block automatic new purchases.

## Inputs and execution

The JSON manifest contains:

- facts_path: approved, non-demo facts YAML.
- assets: records with id, absolute file, SHA-256 checksum, original_name, source, rights,
  and optional owner/expiry/permissions/tags/source_url.
- logo_id: the supplied company logo.
- extra_reference_ids: compatible product views.
- brief: normal video brief fields including one platform, product, duration and reference_ids.
- history: read-only /api/posts snapshot for duplicate checks.

That API omits creation/publication timestamps. Snapshot posts are conservatively dated
at import, potentially over-blocking old duplicates. Disclose this scope. Empty history
does not constitute a production duplicate audit.

Use normal Settings for credentials; never include keys in the manifest. An in-memory caller
can pass resolved settings to main(settings).

~~~powershell
python -m social.runway_benchmark --manifest data/benchmark/manifest.json --output data/benchmark/baseline-1 --variant baseline --budget data/benchmark/budget.json --max-credits 1800 --timeout 2400
~~~

Parallel trials share one budget path and maximum. Reservations are conservative, NOT
actual charges; reviewer costs are separate. Reservations remain after ambiguous submissions.
Do not attribute account-balance changes to the test when production shares the account.

Existing trial databases cannot be overwritten. Inspect task IDs and saved outputs after
interruption before recovery. Starting another directory is a new trial, not continuation.
events.jsonl, result.json and SQLite retain timings, checks, prompts, tasks and outputs.

Contracts checked 2026-11-17:
[OpenAPI](https://docs.dev.runwayml.com/openapi.json),
[API reference](https://docs.dev.runwayml.com/api/),
[pricing](https://docs.dev.runwayml.com/guides/pricing/).

## Matched branches

The pilot also uses branch_from, branch_approved, source_seed and shared_prefix_seconds
in its private manifest. A branch is a copy of an existing isolated benchmark database;
credentials and old paid-task IDs are excluded from execution. Rejected-scene branches
replay the exact recorded rejection for the edit-vs-regenerate comparison. Approved-scene
branches reuse one accepted scene for animation comparisons, resetting footage duration
and regenerating soundtrack, caption and final checks. Report branch elapsed time and the
shared measured prefix separately. This avoids presenting reused scene generation as a new
independent success. nano_veo and nano_veo_fast keep Nano Banana for any later scene retries.
