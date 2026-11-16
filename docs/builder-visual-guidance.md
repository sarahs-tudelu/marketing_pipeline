# Builder product rules in social media generation

The production fact sheet `tudelu-2026-11-16-builder-v1` adds `visual_guidance` to the
existing `tudelu-2026-11-04-v2` document. Existing caption facts, their validity dates,
and product provenance are preserved. The new section is for product appearance and
motion; it does not grant new caption claims or engineering approval.

## Sources found

The website repository is `tudelu-system/tudelu-frontend`. Its current fetched staging
commit was `f9b43c223fdb11afe5e95573a346dabdce8ba861`. The source files consulted matched
that commit, including in the checkout with unrelated pending development. No website
source or credentials were changed or copied into the social application.

- `apps/canopy-designer/server/assistant/rules.js`: central assistant facts, owner-confirmed
  product knowledge, louver connections, canopy orientation and roof-panel rules.
- `apps/canopy-designer/PRODUCT-RULES.md`: actual frame geometry, constant cross-sections,
  measured connections and drawing acceptance boundaries.
- `apps/canopy-designer/skill-sources/tudelu-project-planning/references/builder-products.md`:
  product purpose, roofs, supports, lighting and fascia-less sunshade restrictions.
- `.../references/product-product-families.md`: fixed louvers, MUTE appearance and motion,
  finishes, product-family differences and unreleased families.
- `.../references/product-scope-and-boundaries.md`: source precedence and evidence limits.

The website's `projectDirectorMainAgent.js`, `projectDirectorPlannerPrompt.js` and
`projectDirectorDirectPrompt.js` import the shared rules. The planning skill links the
product references. This verifies the source wiring; it does not certify the website's
deployed server revision or any runtime prompt overrides.

The reviewed public pages were https://tudelu.com/ and https://tudelu.com/design.
The design page still includes a motorized-pergola-roof example. The explicit fixed-louver
rule in the Builder's product guidance takes precedence. No pricing, stock, lead-time,
acoustic-rating or structural claims were imported into the caption facts.

## Configuration and behavior

`config/visual-guidance.builder.yaml` contains the curated rules and immutable source
paths, commit IDs and SHA-256 hashes. The same section is appended to
`config/facts.tudelu-2026-11-16-builder-v1.yaml`. Every rule cites its source IDs.

There are seven configured visual families: MUTE retractable partition walls,
architectural canopies, fixed-louver pergolas, sunshades, covered walkways,
drive-through systems and carports. Coming-soon families are not approved visual
profiles. Builder experimental/concept exceptions are not treated as released products.

- The idea planner receives the rules and limits suggested products to those profiles.
- The prompt writer receives the full guidance, with precedence over conflicting site
  examples or references. Each image or video clip retains its product's concise physical
  cues within Runway's UTF-16 prompt limit.
- Edited and retried prompts receive the same product cues before submission. The exact
  prompt and source guidance version are recorded with the job.
- The independent visual reviewer checks product appearance and motion against the same
  version. Reported mismatches, or a missing acknowledgement of the selected rules version,
  hold the post even if the model's overall verdict says pass.
- Old fact versions retain their existing hashes and remain readable. Existing posts and
  already queued jobs retain their selected version; this update does not regenerate paid
  media. The newest non-demo sheet is the default for new posts.

Visual review remains a model judgment, and video review samples frames. These controls
improve product consistency but do not replace the deterministic Builder's parts-fit
validation or guarantee perfect pixels. Exact project depictions should use approved
company photos or accepted Builder renders as references.

## Updating

### Owner correction: hanger-rod canopies

On 2026-11-16, the owner reported that generated video showed bottom-supported
hanger-rod canopies and confirmed that Tudelu does not offer them. Rule
`canopy.hanger_rods_above`, recorded in `social/product_rules.py` under
`tudelu-owner-visual-2026-11-16-v1`, requires hanger rods above the roof, rising
from the outer fascia to higher wall anchors. Rods or rod-like diagonal braces
below the roof, from a lower wall anchor to the underside or outer edge, are
unsupported. Existing rules for vertical posts and separately approved
cantilever arrangements still apply; a lower diagonal rod is not reclassified
as an approved cantilever.

This is a current owner correction, separate from the immutable Builder source
snapshot and caption facts. The planner, prompt writer and visual reviewer
receive its source and version even when a job uses older facts without Builder
guidance. Each relevant image, video or video-edit clip retains a positive
above-roof support cue within Runway's prompt limit, including manually edited
and retried prompts immediately before submission. Job records preserve the
applied correction. Corrections override conflicting references and earlier
generated media; they do not authorize additional caption claims.

Visual review checks the actual rod geometry in sampled frames, records affected
timestamps, and holds reported violations even when the model's overall verdict
says pass. The review must acknowledge the current correction version. The
publication gate reruns this check for scheduled posts before a new public write.

Existing output files are not changed by updating the code. After deploying,
recheck the affected post and use **Regenerate clip** for the flagged clip;
the replacement receives the current constraints and must pass checks and human
review again. Already-running Runway tasks finish with their submitted prompts,
and their outputs still require the corrected visual review. No paid regeneration
or public publishing is triggered by this code change.

### Updating the Builder snapshot

Review changed Builder sources before changing the curated rules. Preserve the original
source revision and file hashes for every imported version. Save a new facts version;
do not overwrite an existing document. `save_facts` validates source references and
product IDs. Deploy code support before importing a sheet with `visual_guidance`.

Validate with `pytest tests/test_product_rules.py tests/test_runway_prompts.py
tests/test_gate.py tests/test_idea_plans.py`. Tests use fixtures and mocked providers;
they do not purchase media generation or publish posts.
