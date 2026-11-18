# Runway Media Studio

**New post from an idea** (`/admin/create`) turns a written idea into a reviewed plan and then a post. **Campaigns & advanced** (`/admin/media-studio`) handles recurring campaigns, audio-only library media and every brief option. **In progress** (`/admin/media-queue`) shows generation progress. Generation uses the Runway API key for images, videos, video edits and audio, including the models hosted by Runway. Caption generation and independent factual review still use Claude; visual review, audio listening and duplicate embeddings still use OpenAI.

## Configuration

The ignored root `.env` now contains a blank slot:

```dotenv
RUNWAY_API_KEY=
IMAGE_PROVIDER=runway
VIDEO_PROVIDER=runway
RUNWAY_IMAGE_MODEL=nano_banana_2.1
RUNWAY_VIDEO_MODEL=veo3.1
```

Paste the key after `RUNWAY_API_KEY=`. `gsm://projects/PROJECT/secrets/tudelu-runway-api-key/versions/1` is also supported when `SECRET_BACKEND=google`. `.env.example`, `.env.local` and the Droplet template include the same settings. `TUDELU_ENV_FILE` selects a different environment file; the local demo uses `.env.local`. Restart both web and worker after editing configuration. Do not commit keys.

New jobs use Nano Banana 2.1 for images and scene stills, and standard Veo 3.1 for animation. Existing environment overrides take precedence: update `RUNWAY_IMAGE_MODEL` and `RUNWAY_VIDEO_MODEL` in the deployment environment when pulling this change. Each job saves its image/video models so recovery and replacements retain the original models and provenance. Older saved jobs without these fields retain Gen-4 Image / Gen-4.5; create a new job to use the new defaults. `gen4_image` and `gen4.5` remain supported explicit overrides.

`AI_MODE=live` permits paid generation. The checked-in defaults remain offline fixtures. Creating an idea queues a background job; run the worker to advance it. No API call is made by simply opening a page or saving a paused recurring campaign. `LIVE_PLATFORMS` and the existing account/fact checks separately govern publication.

Before starting updated services, run `tudelu init` (or `alembic upgrade head`) using the intended deployment environment. Migration `9b61d8c4a201` adds `media_jobs` and `media_campaigns` without modifying existing posts. Migration `d3a7f1c58e42` adds the optional asset `owner` column; existing rights text is unchanged. The Droplet Compose migration service runs this upgrade during deployment. Rebuild/recreate both web and worker. This repository update does not deploy to the Droplet or enable live publication.

## Inputs and operations

| Operation | Runway endpoint / model | Inputs |
|---|---|---|
| Image / scene still | `text_to_image` / `nano_banana_2.1` | Prompt, tagged product references and optional untagged style reference; high thinking, no seed |
| Video | `text_to_video` / `veo3.1` | Prompt; 4-, 6- or 8-second generation, native audio off |
| Image animation | `image_to_video` / `veo3.1` | Reviewed scene as the first frame only, plus a fixed negative prompt; 4-, 6- or 8-second generation, native audio off |
| Video edit | `video_to_video` / `aleph2` | Prompt and one uploaded video; edits up to 90 seconds, split into inputs of at most 30 seconds |
| Audio / audio reworking | `sound_effect` / `seed_audio` | Prompt and up to three uploaded audio references |

The request shapes and model IDs were checked against the [official Runway API specification](https://docs.dev.runwayml.com/openapi.json) and [API reference](https://docs.dev.runwayml.com/api.md) on November 17, 2026. The supported model settings intentionally accept only the implemented schemas. Changing to a different model requires updating its payload contract.

Upload company media in Asset library. Images, videos and MP3/WAV/M4A/OGG/FLAC audio are supported. Usage rights are **Public** (may be published) or **Not public** (internal use and dry runs only); the owner is a separate optional field. A Public idea cannot use a Not public reference, and media generated from a Not public reference is Not public. The gate holds Not public media from live publication. Rights saved as free text before this choice existed show as *Unclassified* and keep their earlier behavior; re-upload an asset to classify it. Objects already uploaded directly into the configured media bucket can be registered using **Import bucket asset** and their exact object key, rights, source and provenance. The existing object remains intact. Reference assets are read from the local store or private bucket, checked against their checksum, and sent through Runway's ephemeral upload API. Runway never receives the bucket credentials. Missing, expired or unpermitted references fail before generation.

## How Runway prompts are written

Creating an idea saves the admin prompt, a rotating creative direction (close-up, wide view, the system in operation, daylight and so on) and, with **Use website text for creative direction** checked, a timestamped, hashed snapshot of the tudelu.com homepage: meta descriptions, visible text in page order (so *Coming soon* stays beside its system) and image descriptions. No Runway prompt is written yet.

When the worker starts a visual job, `social/prompting.py` gathers four sources and asks Claude (`ANTHROPIC_MODEL`) to write one Runway prompt:

1. **Rules:** the selected approved facts version (products, brand voice, banned phrases and relevant facts), the platform tone and orientation, and fixed visual rules that match the vision gate (one Tudelu system as the subject, people when requested, plain unmarked surfaces).
2. **tudelu.com:** the saved snapshot, which lists the systems Tudelu offers.
3. **Uploaded media:** up to 8 company uploads marked Public (matching the product tag first), plus any selected references, described by their observed visual summaries. Uploads without a summary are summarized once with `OPENAI_VISION_MODEL` (at most 6 per prompt) and the summary is kept.
4. **Previous generations:** summaries of the 6 latest Runway originals with their post status. These keep the look consistent and the compositions varied. They never establish what Tudelu sells, and held or deleted ones are never copied.

The writer must name the Tudelu system it depicts. Before anything is sent to Runway, the app checks that the name appears on tudelu.com, in the facts or in uploaded/selected media. A prompt that cannot be grounded fails the job with the reason. Resume it to try again, or edit the idea. The prompt is written in positive language, because Runway's image models and Gen-4.5 have no negative prompt. Veo 3.1 clips also send one fixed `negativePrompt`: frozen pictures, people passing through closed doors, glass or walls, warping or changing structure, text and logos. The prompt is trimmed to Runway's 1,000-character limit and saved with its sources before the paid request. Resuming never rewrites a prompt that was already submitted. Ideas that were queued before this change get a newly written prompt when they start. Expand **Prompt sent to Runway** on a queue card to see the prompt, the depicted system and its sources. Fixture mode uses a deterministic offline prompt.

For generated images and videos, the [reference workflow](reference-grounded-generation.md) follows this creative writing step: actual company product images ground construction, while the prompt controls new locations, architecture and supported colors, finishes and configurations. Video clips first receive newly composed scene images, checked before animation. A saved visual direction and a reviewed scene used only as a style reference coordinate lighting, palette, lens character, framing and camera pacing across the film.

Website text and media summaries guide what is shown; they never become approved product claims. The selected immutable facts version remains the source for captions. The custom prompt, concept, content kind and depicted system also accompany caption generation.

For a video soundtrack, enter a music direction. Without audio references, Runway's `text_to_music` endpoint (`RUNWAY_MUSIC_MODEL`, default `eleven_music_v2_5`) composes a track of exactly the finished video length in a second saved task; it is requested as instrumental unless the direction mentions vocals, and is billed per requested second. With audio references, `seed_audio` reworks the uploaded bites instead; it has no length parameter, so a shorter result is padded with silence. The app replaces the video's soundtrack with the result and trims it to the video. Jobs queued before this change keep `seed_audio`. Leaving the soundtrack blank gives calm ambient music (see the rule below), so no video is silent. Audio-only jobs are library assets; select a video operation to create a social post with audio.

## Finished videos up to 90 seconds

The length field on the plan review and in Campaigns & advanced accepts 3–90 seconds for immediate, waiting and recurring ideas. New Veo video generation uses an ordered shot plan with clips no longer than 8 seconds. Each task requests the smallest supported length (4, 6 or 8 seconds) that covers its planned clip; assembly trims surplus footage, keeping the chosen finished length and three-second logo ending. Provider charges cover the generated seconds, including trimmed footage. A 90-second video uses eleven scene-image tasks and eleven animation tasks for 87 seconds of footage, plus its soundtrack and logo ending. Explicit Gen-4.5 jobs retain the 10-second clip maximum. Editing 90 seconds of uploaded footage uses three inputs of at most 30 seconds. Each source interval is saved separately with its original checksum, rights and provenance; the uploaded original is unchanged. An edit cannot request more footage than the source provides. Longer source files can supply the requested opening interval.

Each clip's prompt, seed, source interval, Runway task and result asset are saved. Prompts can mix settings and supported product configurations while sharing photographic direction; shots of the same installation retain its configuration. Continuity between independently generated shots is not guaranteed; the finished montage still goes through vision review. Each generated clip incurs a scene-image cost, scene-review cost and video-generation cost. The queue reports the number of saved clips, and recovery reuses completed clips and known tasks. Ambiguous submissions still require reconciliation before continuing.

Assembly joins clips in order into one H.264 video at the destination dimensions, with continuous timestamps and original clip audio (silence where a clip has no track). An optional generated soundtrack replaces the combined audio. Short or missing generated clips hold the job instead of stretching footage to fill the requested duration. The maximum is also enforced before generation and at publication; configured lower platform limits remain effective. Existing campaigns keep their saved duration until a new brief is saved.

The Veo per-task bounds were checked on November 17, 2026 against the [Runway API specification](https://docs.dev.runwayml.com/openapi.json). Legacy generation and edit bounds were checked on November 12, 2026 against [Runway Gen-4.5](https://help.runwayml.com/hc/en-us/articles/46974685288467-Creating-with-Gen-4-5) and [Aleph 2 video editing](https://dev.runwayml.com/learn/edit-and-finish-video-api). Platform references are in [platform verification](platform-verification.md).

## Movement in video clips

A clip is meant to show something happening. The animation prompt for each clip describes what the people in the scene do, in order, the setting's natural movement (leaves, water, rain, light) and one slow camera move, while the Tudelu structure stays rigid. A camera held still is used only when people or the setting carry the movement. People pass only through real openings: the scene image starts with a door, gate or glass wall already open, or the prompt has someone open it by hand (or automatic doors slide apart) before they step through.

Veo receives the reviewed scene as its first frame only. Until November 18, 2026 it was also sent as the last frame, which made Veo return to the opening picture: clips came back frozen, sometimes with a person or two moving near the edges, and people walked through doors that had to end up closed. All six Veo clips in the November 17 benchmark were frozen this way; the Gen-4.5 clips, which received only a first frame, moved.

The post check also measures, from the pixels, whether each generated clip moves. A clip in which less than 3% of the middle of the picture ever visibly changes, and whose middle changes by under 4 of 255 levels on average, is frozen; the logo ending isn't measured. A frozen clip fails the visual check with "Clip N (seconds A–B) is frozen", and the automatic replacement makes that clip again, with the finding passed to the planner. The visual check itself also looks for people passing through closed doors, glass or walls, and people frozen mid-step. Uploaded footage, video edits and offline placeholders aren't measured. Rechecking a post made before this change measures its clips too.

Jobs whose scene was already saved before this change keep their saved camera prompt, but are animated from the first frame only.

## Audio review

Every post check now includes `audio_review` for uploaded and AI media. The exact file checksum is verified, and every audio stream is decoded in full (up to 90 seconds, four mono/stereo tracks). A file without an audio stream passes without a listening call. Unsupported channels, failed decoding, missing coverage or uncertain words hold the post.

`OPENAI_AUDIO_REVIEW_MODEL` (default `gpt-audio-1.5`, using `OPENAI_API_KEY`) listens to the complete PCM WAV tracks and records sounds, speech and sung lyrics. The app saves summaries, transcripts, model, timestamps and checksums in asset metadata. Asset library exposes **Audio summary and transcript**. Audio is untrusted content, and machine listening does not establish music ownership or licensing.

Claude independently checks every transcript segment and its combination with the caption against currently active approved facts and declared rights. Instrumental music, ambience or silence can pass without fact citations. Unclear listening, unsupported claims, incomplete transcript review or provider failures hold publication. Ordinary Recheck runs this step too, and final caption review receives the result alongside vision evidence. The two reviews are independent: passing audio does not clear a vision hold.

Successful observations are reused only for identical file bytes, model and execution mode; partial multitrack work can resume. Factual review runs fresh for each post/check so changed captions or expired facts cannot reuse an old approval. Fixture results are explicitly simulated and cannot satisfy live readiness. Live review adds OpenAI and Anthropic charges. This uses [OpenAI audio inputs](https://developers.openai.com/api/docs/guides/audio-chat-completions) with [gpt-audio-1.5](https://developers.openai.com/api/docs/models/gpt-audio-1.5); no new database migration is required.

## Platform output and provenance

Every Runway image/video original and publishing rendition receives a text summary in `Asset.media.visual_summary`. The configured OpenAI vision model observes the saved bytes; generation prompts are not treated as evidence. Metadata includes the description, readable visible text, source checksum, model, timestamp and limitations. Videos use at most 12 chronological frames, including the final frame; audio and unseen action are not analyzed. Original media bytes and embedded provenance remain unchanged. See [OpenAI image inputs](https://developers.openai.com/api/docs/guides/images-vision).

Caption generation receives this metadata as `visual_context` for mood and relevant framing; it does not become approved product facts or replace the independent checks. Older Runway assets get summaries when captions are next generated. Fixture mode stores explicitly simulated descriptions without provider calls. Live summaries use `OPENAI_API_KEY` and `OPENAI_VISION_MODEL`, with provider charges for the additional analysis. A failed summary leaves saved originals/renditions intact; resume the job to retry analysis without resubmitting to Runway. Summaries are visible in the Asset library.

The In progress page displays loading indicators and refreshes in place. Post pages detect processing in the web app, another tab or the worker, disable conflicting actions, and refresh completed results. Changed results do not discard unsaved caption edits. Duplicate form requests during an active operation show progress instead of an error or another provider call. Browser scripts/styles use content-versioned URLs so deployments refresh cached loading behavior. These changes use the existing JSON metadata and transaction locks; no additional database migration is needed.

Each selected platform gets its own job, prompt framing, seed and caption. Platform profiles set rendition dimensions: Instagram images default to 1080×1350, other images to 1080×1080; Instagram, TikTok and Facebook (Reels) videos default to 720×1280 and other videos to 1280×720. Runway receives a supported orientation; the app fits the result into the exact configured dimensions with padding, without stretching or discarding the original. Images become JPEG; videos become H.264/yuv420p at 30 fps with AAC audio where present. Normal platform byte/duration/format checks still apply.

Generated originals and reference originals are retained unchanged. A publishing rendition is a new asset linked to source IDs/checksums, rights, expiry, prompt, model, task, website snapshot and campaign. Embedded metadata in originals is retained; a transcoded rendition is not claimed to retain a cryptographic C2PA signature. Native AI-label flags and review checks remain active. Audio receives its own full-track review, described above.

AI image renditions embed an **IPTC XMP DigitalSourceType** declaration, the generator and the source-file checksum. This also covers Runway outputs that arrive without embedded AI metadata. Fully generated images use `trainedAlgorithmicMedia`; AI-edited images use `compositeWithTrainedAlgorithmicMedia`. This is a truthful application declaration, not a provider signature or a C2PA credential. Original files and any signatures they contain remain unchanged in the library. The declaration does not establish product facts or rights.

For live Facebook photos and LinkedIn images, the disclosure gate re-reads the delivered file, verifies its checksum, and checks that its embedded declaration agrees with the recorded source. The adapter repeats this check before publication. Database flags and `metadata_keys` alone cannot pass it. The independent reviewer receives the explicit result and byte evidence. Missing or unreadable metadata holds live delivery, including older renditions; they need a new, correctly marked rendition and fresh checks. This image checker does not validate video provenance or C2PA-only files; a metadata-only route without supported evidence remains held. Native-flag routes (Instagram, X and Facebook Reels) retain their existing behavior. Platform recognition of an IPTC marker is not guaranteed.

Verified against the [IPTC Photo Metadata standard](https://www.iptc.org/std/photometadata/specification/IPTC-PhotoMetadata-2025.1.html#digital-source-type) and [Meta's explanation of IPTC/C2PA image signals](https://about.fb.com/news/2024/02/labeling-ai-generated-images-on-facebook-instagram-and-threads/). Captions remain free of automatic AI disclosure text.

## New post from an idea

An idea is created in three steps: **Describe your idea → Review the plan → Generate and publish**.

1. **Describe your idea** asks only for the idea, a format and platforms. Format defaults to **Let the idea decide**; Image, Video and Edit an uploaded video force a format. **More options** holds the content style (decided from the idea by default), an optional product focus, the approved facts version (newest non-demo version by default), usage rights (Public by default) and owner, tudelu.com use and reference media.
2. **Plan this idea** makes no Runway request. In live mode Claude reads the idea against tudelu.com and the approved facts and proposes a post name, format, finished length, an ordered list of clips (a setting and a Tudelu system for each), and a music direction. Explicit words in the idea always win over the model: a stated length (`30 second`, `1-minute`), video words (`clip`, `reel`, `footage`, `montage`) and music words set the format, length and soundtrack even if the model disagrees, and a negated request such as `no music` leaves the music direction empty, which means calm ambient music. Every video ends on the logo. Systems not named on tudelu.com or in the approved facts are cleared so the grounded prompt writer chooses instead, and clips beyond what fits (at least 2 seconds each, at most 9) are dropped with a note. Fixture mode plans from the same signals without a model. The review page explains how the idea was read, lets you edit every field, add or remove clips, and shows what Runway will be asked to generate per platform.
3. **Create now** or **Use for next automatic post** queues the reviewed plan; **Change the idea** returns to step 1 with your entries.

**Your own photos, videos and audio.** Step 1 also takes files. They're saved to the asset library straight away as real company media, with the usage rights and owner under **More options**, and stay listed (ticked) if the plan needs a change, so nothing is uploaded twice. Choose how to use them:

- **Post it as it is** takes one photo or video, plus one audio file as the video's soundtrack. Nothing is sent to Runway. Each platform gets a copy that keeps the media's shape, scaled to the platform's size (padded to the platform's size only when the shape or size doesn't fit, such as a small video for Facebook Reels). A video ends on the logo card, and its length can be shortened on the plan page. Without an audio file it keeps its own sound, or gets an ambient track from the asset library when it's silent, when the idea asks for music or when you choose ambient music on the plan page. AI writes the caption and the normal checks run; the visual check isn't needed for real media. The original stays unchanged.
- **Use it as a starting point for new AI media** adds images and videos as references (an image starts a video, a video can be edited) and audio as soundtrack references to rework, then plans as above.

A video with several clips gets one Runway clip per planned shot, sized to share the footage evenly; extra clips needed for long videos continue the nearest shot. The prompt writer receives the storyboard and returns one grounded prompt per clip with a consistent look across clips; each clip's system must be named on tudelu.com, in the approved facts or in uploaded media, or nothing is sent. A selected start image applies to the first clip only.

**Every video has sound and ends with a cut to the Tudelu logo.** This applies to generated videos, your own videos posted as they are and videos posted from the library. Generated videos get Runway music: the direction you describe, or calm ambient music when you leave it blank (including ideas that say `no music`). The ambient direction rotates through six styles (warm pads, felt piano, glass bells, soft keys, marimba and nature sounds), choosing the one used least recently. Real videos never get AI-made sound, so they aren't labelled as AI: a real video keeps its own sound, and when it has none (or you choose ambient music on the plan page) it gets an ambient track from the asset library, with no Runway request. Mark tracks with **Ambient music for real videos** when uploading or editing them; only company audio (music you own or have licensed, not AI-made) can be marked. The track used least recently is chosen, every platform's copy of a post gets the same one, and it is looped with a crossfade when shorter than the video and faded out at the end. A Not public track is never used for a Public video. Only while the library has no such track does the app synthesize simple ambient music itself, in the same rotating styles. Videos need at least 5 seconds, because the logo card takes the last 3. A check named `video_finish` holds any video post without sound or the logo ending, including posts approved before this rule. On its page, **Add the logo ending and sound** makes a copy of a real video with them (without Runway) and runs the checks again; for a silent generated video, **Add Runway ambient music and the logo ending** has Runway compose ambient music for it and replaces the post when the new video is ready. Generated videos queued before this rule also get Runway ambient music before they are assembled. Nothing can be made until a logo is in the asset library.

**Logo end card.** Upload the official logo once in **Asset library → Add media** as a PNG (a transparent background works best) and tick **This is the Tudelu logo**. Every video then ends on a 3-second card with that logo, unaltered, centered on white (or near-black for a light logo). The video cuts straight to it. With several logos, the plan page lets you choose; elsewhere the newest tagged logo is used. The card is rendered by the app, not Runway, and counts toward the finished length: a 30-second video has 27 seconds of generated footage. Music continues under the card. The publishing rendition records the logo asset and checksum, takes Not public rights if the logo is Not public, and tells the vision review that the final seconds show the company-supplied logo. Logos or brand text inside generated scenes are still held. Images cannot carry a logo card or music. Your own videos keep their own sound under the footage and are silent on the card unless music was added.

**Why a post is held.** A held post's page starts with **Why this post is held**: the caption-writing error in plain words, or each failed check with what it found and what fixes it. **Write the caption again** (or **Write a new caption** when one exists) writes a fresh caption for the same media and runs the checks again. A post with no caption can't be rechecked until one is written.

**Regenerate part of the video.** When a generated video or image is held, its page lists the parts that can be made again on their own: each clip of a multi-clip video (time range, setting and system), a single-take video or image, and the music. The visual check reports the time of each frame that shows a problem, and the clips holding those frames open automatically and are tagged **Flagged by the check**. The held post's button names that part (**Fix clip 3**) and opens it; when the check didn't say where the problem is, the button reads **Choose the clip to fix** and you open the clip whose setting matches what it found. **Regenerate clip N** queues a job that generates only that clip with a new seed and an editable prompt; when the check found lettering, logos or people, positive wording that keeps them out of frame is added to the suggested prompt. For a clip animated from a generated scene image, the prompt is that scene's description, and the check's findings for the clip and the old scene go to the planner as a correction (see [Product references](reference-grounded-generation.md)). The other clips, the music track and the logo end card are reused, so a live run costs one Runway scene image and one Runway clip. When the visual check holds generated media, the flagged clips (or every clip, when it didn't say where) are regenerated automatically in one replacement, up to `MEDIA_AUTO_RETRIES` replacements in a row (default 3); see [Product references](reference-grounded-generation.md). **Regenerate the music** composes a new track from an editable music prompt and reuses every clip and the logo card. The new media becomes a new post that goes through captions and checks like a **Create now** post, and the held post is marked failed with a link to its replacement. A post can have one regeneration in progress at a time. Clip prompts for storefronts, streets and parking also ask for one plain, unbranded facade with blank neighboring walls, because these settings invite generated lettering.

**Use for next automatic post** saves a `planned` job per platform until **Automatic posts** is on and that platform's media queue is available. The worker activates the oldest waiting idea per platform first, ahead of new recurring campaign batches. You can remove a waiting idea from In progress.

**Create now** queues a prioritized request on the next available worker tick. It handles media generation/editing, visual summaries, caption writing, checks and scheduling even when routine automation is off. It does not immediately publish: checks can hold the post, and platform caps, scheduling windows and live/simulated delivery settings still apply. Global and platform pauses remain effective. In progress follows both media and post status.

Campaigns & advanced offers the same **Create now** and **Next automatic post** workflows without the plan step, with additional audio and recurring options. Check **Create posts** for the full workflow; uncheck it for library-only media. Turn off **Automation → Automatic posts** to pause routine caption/check/schedule progression and recurring batches while explicit Create now requests and queued media jobs finish. Already scheduled posts can still publish. Until the UI switch is changed, `AUTO_ADVANCE` supplies the default.

Choose **Recurring campaign** and save a brief with specific formats or **Varied images and videos**, and optionally **Balanced mix**. Campaigns start paused. Enable one in Campaigns & advanced when ready. They spend only when `AI_MODE=live`, **Automatic posts** is on, and every selected platform is explicitly live, connected and unpaused. The global stop switch stops both generation and publication. Pausing a campaign stops future batches; existing jobs/posts retain their own queue status.

Every interval (minimum 24 hours), an eligible campaign creates one job per platform. It rotates eight creative directions using recent platform history, selects a new seed, cycles available source references and formats, and plans promotional/editorial content against the platform ratio. TikTok always gets video. Facebook videos publish as Reels with Meta's AI label, so mixed campaigns rotate images and videos there too. The scheduler still enforces its own mix, spacing and capacity limits.

Only one unfinished batch per campaign is allowed. Unfinished, uncertain or failed media work, or held/failed/unsent posts, blocks further batches so repeated failures cannot keep spending. Fix/resume a job, remove it from the queue, or correct the post; check the campaign's displayed reason. Automatic variation improves diversity but does not guarantee visual novelty; the existing caption duplicate checks still run. There is no automatic recurring spending in fixture mode.

## Recovery

Scene planning keeps the actual idea, reviewed shot and explicit retry edits separate from model-written prompts and review findings. Unspecified structural details follow a compatible product reference: a supported center beam and two louver bays in one plane are not a second roof, and a Builder example's six-post count does not apply to every pergola. The planner must reconcile earlier findings with the request, reference pixels, Builder rules and owner corrections before repeating them as requirements. Actual unsupported construction and visible defects still fail review.

The plan page fixes each clip's setting, system and configuration before anything is generated. Planning fits the idea to what Tudelu offers, the way a product brief would: a rainy dinner under a pergola becomes a pergola with a panel or glass roof, not a request the checks keep rejecting. Every later step follows that approved shot, and checks pass supported customizations. Only a hard limit, something Tudelu doesn't make, fails as a scope problem; see [Builder visual guidance](builder-visual-guidance.md#owner-instruction-product-brief-scope). When a check says the approved shot itself asks for one, automatic retries stop and the job waits for the shot to be changed.

Retry edits save the before/after text so moving a dining table does not silently endorse unrelated restrictions already in the generated prompt. Submitting an unchanged suggested correction is not recorded as a new operator instruction. Earlier explicit edits survive subsequent automatic retries and provider failures; legacy edits without a saved baseline remain explicit instructions.

These changes apply when planning new scenes. Existing paid scenes/tasks and saved rejections remain intact; a rejected scene is not rerun through the reviewer to obtain a different verdict. For an older job with unwanted restrictions copied into a legacy manual edit, create a new idea from the original request after updating the application, or explicitly correct those restrictions. New jobs use the configured model pair; old jobs retain their saved models. No database migration is required for these additional JSON provenance fields.

For a failed **post**, open it from Needs attention and use **Retry post** once its underlying issue is corrected. The new attempt reuses the saved media and caption, runs fresh checks and schedules normally, including with routine automation off. It preserves the original error/audit and delivery mode, and the campaign follows the retry. No new Runway generation is requested. If publication may already have been accepted, retry is blocked until the result is reconciled. This post retry is separate from resuming an unfinished media job below. Apply migration `f2b6d9a51034` before updating web/worker.

Runway submissions checkpoint `submitting` before the paid call, then save the returned task ID. The worker polls once per tick, downloads completed outputs immediately and resumes from stored originals if rendition/storage processing fails. It never sleeps waiting for a video inside a web request. `MEDIA_JOBS_PER_TICK` bounds work, and `RUNWAY_TASK_TIMEOUT_SECONDS` bounds polling time.

Use **Resume saved task / output** after correcting a download/storage/configuration problem. Known tasks are polled again without another paid submission. An interrupted/ambiguous submission becomes **uncertain** and is never automatically retried; recover the original task ID from Runway and use **Recover original task**. Do not create another job until the first submission is reconciled. When Runway reports a task as failed or cancelled, In progress says which step failed (clip N, the video, the image or the music) and why, from Runway's failure code: content moderation, an unusable reference, or an internal error. **Generate … again** submits only that step, with an editable prompt and a new seed; clips already saved are kept. It is also offered when Runway rejected a submission. Resume is not offered for a failed task, because polling it again cannot succeed. Queued or failed work can be removed from the local queue; this does not cancel or refund an already accepted Runway task.

**Music provider rate limits.** A terminal music/audio task failure with code
`THIRD_PARTY.RATE_LIMITED` now queues a new attempt after a delay: 2, 4 and 8 minutes
with the default `MEDIA_AUTO_RETRIES=3`. Raising that setting allows more attempts,
with delays capped at 30 minutes; `0` disables automatic retries. This limit is
separate from scene-quality corrections. The job retains completed video clips,
their prompts and task history, and retries only audio. The queue shows the earliest
retry time. The delay survives worker restarts and does not prevent other ready jobs
from running. After the limit, a video's generated music switches once to **Seed Audio
through Runway**. If the provider specifically reports `Music generation timed out
after 80s` (or another duration), the app shows that diagnosis even when the failure
code says rate limited, and switches immediately instead of repeating the same model.
Uploaded audio references and audio-only jobs keep their selected model.

For an already-failed video music task from an older build with this rate-limit code,
use **Try Seed Audio through Runway** after updating web/worker. The alternate request
keeps the music direction and all saved clips; it uses the existing `RUNWAY_API_KEY`.
**Generate the music again** still retries the current model after two minutes.
`MEDIA_AUTO_RETRIES=0` also disables automatic model switching; manual recovery remains
available. If Seed Audio fails, the job stops for manual recovery without another
automatic retry cycle. The queue displays the selected alternate model, and the
brief records the original failure, model switch and prompt. No database migration
is needed. Other terminal failure codes, HTTP errors and ambiguous submissions
retain their existing recovery behavior. A new
generation can incur Runway charges; this delay does not guarantee capacity has
recovered. Runway's [task-failure guidance](https://docs.dev.runwayml.com/errors/task-failures/)
recommends delaying retries for temporary upstream failures.

Seed Audio's [Runway request schema](https://docs.dev.runwayml.com/api/) supports music
prompts but has no explicit duration parameter. The app preserves the returned audio
as an original asset and fits the publishing soundtrack to the full video duration:
short tracks loop with crossfades, long tracks are trimmed, and the ending fades out.
The rendition records the source checksum and fitting details. Normal caption,
disclosure, visual and full-stream audio checks still apply before publication.

The authenticated, CSRF-protected API also exposes `POST /api/media-jobs` with a `CreativeBrief` JSON object and `GET /api/media-jobs` for status. Neither endpoint returns credentials or expiring signed output URLs.
