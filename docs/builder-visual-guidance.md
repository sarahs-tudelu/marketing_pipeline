# Builder product rules in social media generation

The production fact sheet `tudelu-2026-11-16-builder-v1` adds `visual_guidance` to the
existing `tudelu-2026-11-04-v2` document. Existing caption facts, their validity dates,
and product provenance are preserved. The new section is for product appearance and
motion; it does not grant new caption claims or engineering approval.

`tudelu-2026-11-17-builder-v2` (`config/facts.tudelu-2026-11-17-builder-v2.yaml`) renews
it with the same `visual_guidance`. Each fact was checked against its cited tudelu.com page
on 2026-11-17. The 33 claims the pages still back run to 2026-12-17, and the five
coming-soon availability facts run to 2026-11-24. R06 (AI Design Studio) and R07 (shop)
keep their 2026-12-04 expiry: their "confirm through project assessment" and "a listing
does not confirm stock" cautions are not on the cited pages, so they need a reviewer's
decision. W01 now also cites /partition-walls, which states its ceiling-assessment
caution. The E03 variant typo is fixed ("assess supports"), and the visual policy no
longer asks for "AI-generated concept" labels: labels come from platform-native flags
and embedded metadata. Load it from **Facts** as a new version; the newest version is
the default for new posts.

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

`social/resources/visual-guidance.builder.yaml` contains the curated rules and immutable source
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

### Reference-grounded rendering

The [reference workflow](reference-grounded-generation.md) makes the guide actionable against actual pixels: the selector checks Public library/Drive photos and reviewed website images against the product rules before each live scene. It uses the chosen image for product construction while composing a new scene from the prompt. Each video scene image is checked before animation; a shared visual direction coordinates varied settings and supported finishes across clips. Final review distinguishes those intended differences from product defects. `social/rendering_guidelines.py` adds the versioned scene-consistency, visible-connection and restrained-camera rules shared by planning, generation and review. This supplements the immutable Builder/facts snapshot; it does not rewrite existing facts or authorize new caption claims.

Version `tudelu-reference-rendering-2026-11-17-v3` adds structure rules from the same Builder snapshot's `PRODUCT-RULES.md` (every piece is solid and never passes through or overlaps another piece, a wall or the floor; posts are seated under the fascia's bottom perimeter; roof panel seams follow an even grid). It also requires the structure to stay rigid within a clip, with no member appearing, vanishing, duplicating or bending; scenes framed so supports and connections are visible and uncropped; and one slow camera move per clip, written as "The camera [move] as the structure stays completely still", following Runway's image-to-video guidance. Orbits, arcs, pull-back or crane reveals and zooms through the structure are avoided because they make the model invent geometry. The scene review now checks the structure piece by piece, and the final visual check compares consecutive frames for members that change between them. Jobs already planned under the previous version plan their next unfinished scene again under this one.

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
guidance. Structured reference-based scenes describe only their selected supports;
scenes with rods retain the positive above-roof cue without adding rods to post-only
designs. Legacy prompts and video edits retain the general conditional cue. Manually
edited and retried prompts receive the same planning and review rules. Job records preserve the
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

### Owner instruction: product-brief scope

On 2026-11-17, the owner asked for generation and checks to reason about what Tudelu
can reasonably build instead of enforcing every Builder default literally: "within the
scope of Tudelu's product offerings but able to be customized to fit a variety of
situations the same way product briefs are created". Weatherproofing a pergola is
possible; a hanger-rod canopy with its supports below the deck is not.

`tudelu-owner-visual-2026-11-17-v2` in `social/product_rules.py` adds three rules that
every planner, prompt writer, scene check and video check reads alongside the Builder
snapshot:

- `scope.product_brief`: the Builder guidance describes Tudelu's offerings and defaults.
  Footprint, size, post count and spacing, beams for longer spans, roof type (open louvers,
  insulated panels or glass), attachment, finish, trim, lighting, setting, weather and
  people may all change to fit the scene. Wording such as "only when selected" means the
  option counts as selected once the approved plan, the prompt or the situation calls for
  it. Hard limits are only what Tudelu doesn't make: rods below a canopy, operable pergola
  louvers, a MUTE partition that slides or folds, a different kind of product, and the
  coming-soon families. Physically broken rendering is still a defect.
- `scope.weather`: rain, snow and wet scenes are allowed. A pergola can take a panel or
  glass roof for weather cover; people kept dry in rain stand under one.
- `scope.people_and_framing`: people, furniture and plants may overlap parts of a structure.
  Framing fails only when the construction can't be read.

The idea planner now records each clip's **configuration** (for example "black pergola
with an insulated solid panel roof for rain cover"), shown and editable on the plan page.
Requests past a hard limit are listed in a warning at the top of the plan, with what the
plan shows instead. Later steps follow the approved shot; the original idea is kept for
mood and intent only. The image-side models receive product names and availability, not
the caption-claim limits in the fact sheet, which stay with the caption review.

When a scene check or video check reports that the approved shot itself asks for something
past a hard limit (`request_conflict`), automatic retries stop and the job or post waits
for a person to change the shot. A paid scene saved before a rule-wording release is kept
and reviewed under the current rules; plans saved before this release are planned once more.

An admin can instead approve a rejected scene as it is with **Approve scene anyway** on the
In progress page, for example when a finding rests on a restriction the generated prompt
added rather than on the request. The saved scene is animated without another scene review.
Its findings, the approver, the time and an optional note stay in the clip's plan and its
provenance. The finished video's check receives them as `accepted_scene_findings` and does
not fail the clip again for the same characteristics, but still checks everything else.

### Canopy and pergola geometry

On 2026-11-24 the owner clarified that Social Studio may depict canopies and pergolas of
any footprint or size. Each design determines its support shapes; post count and spacing
follow a realistic layout for it (see [Realistic supports](#realistic-supports)).
Examples include rectangular and hexagonal layouts, angled or curved posts, and supports
assembled from multiple connected members. A specified four-post design remains valid.
The rule `canopy.design_geometry` applies to planning, generation and visual review,
including jobs using older Builder snapshots. Review checks the connections and whether
the design stays consistent within a shot. Project-specific structural capacities and
dimensions for fabrication still require assessment.

A reference photo sets the outline the image model draws, and the prompt text can't reshape
it. Asked for an L-shaped pergola from a photo of a rectangular one, the model kept the
rectangle and framed over the open corner on repeated corrections. When the approved shot
needs a footprint that no candidate photo shows, the planner plans the scene from the
Builder design without a photo. If it still selects a photo and reports that the photo's
footprint differs (`footprint_differs`), the app plans the scene again without candidates.
The plan describes such a footprint the way it looks, for example two wings meeting at a
right angle with open sky over the corner between them, rather than in plan-geometry terms.

The company provides drawings and design examples on its [drawing studio](https://tudelu.com/design),
[canopy page](https://tudelu.com/canopies) and [pergola page](https://tudelu.com/pergolas).

### Realistic supports

On 2026-11-25 the owner asked that reviews focus on whether a structure is realistic and
within Tudelu's designs rather than on an exact number of posts. The rule
`structure.realistic_supports` applies to planning, scene review and the finished-video
check. Posts stand at the corners or ends and along longer spans, reach the ground and
connect to the frame. Any realistic layout passes, even when its post count or spacing
differs from the prompt, the plan, the approved configuration or an earlier review. The
same applies to exact numbers of louvers, beams and bays within the requested footprint.
An exact count is binding only when the admin's own idea or an operator edit asks for it.
Reviews still reject a floating or ungrounded post, a visibly unsupported span or corner,
disconnected or interpenetrating members, supports Tudelu doesn't make, and a count that
changes between frames of one clip. Planners describe supports by type and general
placement instead of writing an exact post count into the scene requirements.

### Finished canopy panels

On 2026-11-24 the owner clarified that adjacent canopy roof panels join flush. From
above or below, the finished face appears continuous across the full panel run.
Visible gaps, joint lines and grooves between panels fail visual review. This applies
to solid canopy panels used in a pergola as well. An open-louver design keeps the
specified spacing between its blades. The current rule `canopy.seamless_panels`
supersedes the visible-seam instruction in the original Builder snapshot.
[Tudelu canopy roof examples](https://tudelu.com/canopies)

### Updating the Builder snapshot

Review changed Builder sources before changing the curated rules. Preserve the original
source revision and file hashes for every imported version. Save a new facts version;
do not overwrite an existing document. `save_facts` validates source references and
product IDs. Deploy code support before importing a sheet with `visual_guidance`.

Validate with `pytest tests/test_product_rules.py tests/test_runway_prompts.py
tests/test_gate.py tests/test_idea_plans.py`. Tests use fixtures and mocked providers;
they do not purchase media generation or publish posts.
