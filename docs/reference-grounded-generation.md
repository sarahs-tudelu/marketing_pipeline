# Product references in social content

AI images and videos use real media as inspiration when it exists, and never stop for lack of it. Any image of the requested product works, whatever its setting: a canopy photo at a warehouse can guide a canopy at a hotel. When no image shows the product, the worker creates an AI concept from the approved Tudelu Builder profile and saves the Builder version and construction rules with the render. The scene review checks the geometry before animation, and the finished render goes through its usual review.

Real media is required only when the post edits real media or posts it as it is, or when the idea specifically asks to show Tudelu's real or completed work, or a particular photo or footage. A campaign idea that describes real project posts alongside AI inspiration doesn't make its AI posts require real media. Pinterest posts only real media imported from Google Drive.

Current owner corrections and the versioned AI Builder drawing guide define correct product construction. The user's prompt controls the creative scene, and compatible company images ground the product's identity rather than locking its background or finish. The Builder snapshot and its source hashes are documented in [Builder visual guidance](builder-visual-guidance.md). Caption facts remain separate and immutable; reference images do not authorize new product claims.

`social/rendering_guidelines.py` records the current rendering policy. It requires straight, connected aluminum profiles, the supported roof and support arrangement, selected accessories only, and consistent geometry across a scene. Hanger rods stay above the canopy and connect the outer fascia to higher wall anchors. Wood Look remains an aluminum finish. Fixed louvers stay fixed. Camera moves should keep critical connections visible and avoid inventing the hidden side of a structure.

## How references reach the generator

1. The planner and prompt writer receive the rendering policy and Builder rules. The writer saves a shared `visual_direction` for the film: compatible lighting, exposure, palette, lens character, composition and camera pacing. A montage may vary locations, architecture, product families and Builder-supported structures, colors and material finishes. Shots of the same installation retain that installation's configuration; different installations do not have to be identical.
2. Before each live generation, the worker finds Public company images across the asset library, including Google Drive imports. Selected references come first, then product matches from tags, filenames and existing visual summaries, with a campaign's shortlist ahead of the rest of the library; matching Drive imports have preference. Logos, expired/restricted media, unpermitted customer material and unsolicited earlier AI generations are excluded. Up to six library candidates are inspected per scene. Good product-family tags improve retrieval in large, unclassified libraries.
3. The selector can also use product images from the reviewed URLs in `social/resources/website-product-references.yaml`. The worker fetches bounded HTTPS images on `tudelu.com/system4/` and retains their bytes and checksums. It records their creation method as unverified. For photo-based generation of a drive-through, use a library reference because the catalog has no suitable image.
4. OpenAI inspects candidate pixels against the Builder and the requested scene. An image of the product takes priority. With none, Runway gets the supported design details in its prompt and the plan lists zero product images. Only something past a hard limit, or an idea that specifically asks for Tudelu's real work that no candidate shows, stops for correction. A repair uses a company photo as evidence when one exists and otherwise corrects the scene from the Builder rules.
5. Images use Runway `referenceImages`. For **each generated video clip**, Runway first creates a new scene image from the prompt: `@ref0` supplies product construction; an optional untagged reference supplies only the photographic style of a previously reviewed scene. Runway documents an untagged reference as a style input, so its product and setting aren't copied the way a tagged `@ref` image's are, and the prompt never names it. The first reviewed scene anchors the look across the film, including different product families. New scenes use the video's aspect ratio. This follows Runway's [image-reference prompting](https://docs.dev.runwayml.com/guides/using-the-api/), then [image-to-video inputs](https://docs.dev.runwayml.com/api/).
6. The scene image is stored unchanged with AI provenance and source ancestry. OpenAI checks its product construction, prompt adherence and compatibility with the shared visual direction before buying animation. A concrete defect stops the job. Approved scenes become the first frame (`promptImage`) of video generation; the separate motion prompt describes what happens after it, the people's actions, natural movement and one camera move, while the structure stays rigid (see [Movement in video clips](runway.md#movement-in-video-clips)). This adds one image-generation task and a scene review to each video clip. It does not replace the independent final video review.
7. References, checksums, original scene requests, creative differences, visual direction and exact prompts are committed before generation. Scene and video tasks use the same durable submission/poll/download checkpoints. Resume reuses a completed scene and never blindly repurchases an ambiguous task. Generate again explicitly creates a new scene/take, preserving completed clips. Permissions are rechecked between stages, and all source images join output ancestry.
8. Final visual review receives original product references and generated scene images separately from output frames. The scene image is an intended composition, not evidence of correctness. Allowed differences in location, architecture, supported configuration, color and finish are not rejected merely because they differ from the source photo. The workspace and In progress expose product references, generated scene links, creative differences and shared visual direction. Human review and publication checks remain active.

Campaigns with a library shortlist offer that pool first, followed by the library's other images of the product. The worker can extract a JPEG from a reference video and save its source ID, checksum and frame time. A private brief can use a Not public reference. The scene and finished media inherit usage restrictions. If neither the shortlist nor the library shows the requested product, the worker adds the website's product images and otherwise uses the Builder design. The final review samples the finished video.

The planner records concise `scene_requirements` for the chosen roof count, supports,
fascia/post finishes, trim and lighting. These go at the start of the Runway scene
prompt, ahead of optional scenery, with the Builder cues retained. Requirements
that cannot fit stop planning instead of being silently truncated. Choices such as
a single roof or Crown-only lighting belong to that scene; they are not blanket
restrictions on other supported canopy configurations.

When a scene is rejected, **Generate corrected scene** carries its full review,
previous intended changes and any edited prompt into the next planning attempt.
The planner receives the rejected image's pixels, labeled for diagnosis only, alongside
the original company candidates. The rejected image and plan remain in the audit history;
the rejected image is never sent to Runway as a product or style reference. Each correction must be expressed in the new scene's
requirements, and the replacement must pass review before animation. **Resume**
cannot re-review an unchanged rejected scene until it happens to pass. A temporary
review/download failure still resumes the saved work without buying another scene.
Runway's terminal internal errors use **Generate the scene again** to submit a new
task with a new seed, and other completed clips remain intact.

A rejected starting image can be corrected automatically while the clip is still being prepared. The planner receives the review findings and submits a revised scene prompt. When a video repair produces a starting image with a new defect, that defect is eligible for its own correction. Another rejection of a component already targeted for that shot goes to human review.

If visual review is the only failed check just before publication, the worker also queues eligible corrections for generated images and videos. To revisit an existing hold, open the post and select **Run automated checks**. A one-off draft that passes every check waits for approval of its final media and caption.

**Automatic video repairs.** The final visual review compares each affected shot with its starting image and product reference. It records the first affected second, the component, the requested correction, and any clean interval or matching library source. The held post shows the surrounding frames alongside its reference images. Recheck an older hold to get this diagnosis.

- A construction defect in the opening image goes to the scene planner with the failed footage samples and a cleared company reference. The corrected image must pass review before animation.
- A motion defect can use a clean portion of the take with the original soundtrack. A modest slowdown preserves the planned running time when product operation speed is immaterial. Otherwise the animation can restart from the approved image using the reviewer's motion correction.
- A localized appearance edit uses the failed frame to generate a corrected keyframe. Once that frame passes visual review, Aleph receives it at the affected timestamp together with the saved clip.
- A repeated defect goes to matching cleared library footage or a photo with a local camera move. A shot demonstrating product operation requires footage of that action. Ambiguity, an unlocated problem, or a missing match keeps the post available for human review.

Each repaired render goes through caption generation and the full gate. Settings ? Usage reports Runway credits per repaired clip in a render that passes visual review. Local trims and library cuts spend zero Runway credits. All automatic repairs respect the configured replacement limit, with one targeted attempt per affected component in a shot. Pre-animation scene checks also stop on a recurring component. Set `MEDIA_AUTO_RETRIES=0` to disable automatic retries.

Music/audio tasks have a separate bounded recovery for `THIRD_PARTY.RATE_LIMITED`:
[delayed music retries](runway.md#recovery) keep completed clips and wait before
submitting another audio task. Other provider failures still need manual recovery.

Structured scene prompts describe only the selected configuration. A two-post canopy
does not receive generic instructions about hanger rods; rod-supported scenes retain
the owner's above-roof orientation cue. The complete product requirements and setting
must fit the prompt limit or planning stops before a paid submission. Generic product
boilerplate no longer crowds out the setting. Animation preserves the checked scene's
geometry instead of listing alternative supports. Full Builder and owner rules still
apply in planning and both scene and final visual review. This follows Runway's guidance
to [describe positive visual outcomes](https://help.runwayml.com/hc/en-us/articles/35694045317139-Gen-4-Image-Prompting-Guide)
and use [explicit image-reference roles](https://help.runwayml.com/hc/en-us/articles/40042718905875-Creating-with-Gen-4-Image-References).

An unchanged retry uses the original idea and any previous explicit operator edit;
the prior submitted prompt is diagnostic history. A failed provider task does not erase
the last visual diagnosis. The queue shows saved scene attempts, task IDs, links and
newly recorded save times. It refreshes on retry/task/output changes even when a new
image fails with identical error text. The exact scene prompt and review time are visible
under the product reference. Generation remains probabilistic; these changes do not
guarantee that a new image will pass review.

**Regenerate clip** on a held post corrects a scene-based clip through its scene. The
prompt box holds the scene description the clip was animated from, with positive wording
added when the check found lettering, logos or people; the last camera movement is shown
for reference and is planned again with the new scene. The replacement job carries the
visual check's findings for that clip's time range (and any that don't say where), the old
scene image for diagnosis, its scene prompt, requirements and intended changes, and any
edit. The planner corrects them as it does for **Generate corrected scene**, and the scene
review fails a new scene that visibly repeats them, before animation is bought. Corrections
saved during the first run, its retry count and its last step don't carry over. If the old
scene's permissions changed since, it isn't shown to the planner, but its findings still
apply. A clip made before scene images existed also passes the findings to the planner.

Offline fixtures make no reference-selection or generation calls. Existing real-media uploads retain their own-media workflow. Existing video edits retain their source video and Builder constraints. Already-running tasks keep their recorded inputs; completed clips are not automatically regenerated or republished. A replacement affects the requested clip, so other flagged clips still need correction.

## Release and verification

No database migration is required: evidence uses existing job/asset JSON fields. Deploy updated web and worker from GitHub `main` through the user-managed release process. No production deployment or paid media generation was performed while implementing this change.

The Builder fallback rules and website reference catalog are bundled under `social/resources/`
in the installed Python package. The worker reads them through package resources, independently
of its working directory. A source checkout alone is insufficient to update an installed worker:
rebuild the image and recreate web/worker after pulling. If an older build reports
`Media processing failed (FileNotFoundError)` before submitting a reference-based job, update to
this build and use **Resume**. Existing task IDs and saved outputs remain intact; do not create a
duplicate job or copy files into Python's `site-packages` directory.

Tests cover scene-image generation before animation, shared style with distinct scenes, scene rejection and explicit replacement, ambiguous submissions and download recovery, later-clip resume, permissions between stages, original source selection and caching, and separate comparison-image roles in final review. Provider calls use mocks; public website images were retrieved and visually inspected separately.
