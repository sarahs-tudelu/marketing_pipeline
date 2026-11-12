# Runway Media Studio

Media Studio (`/admin/media-studio`) handles custom ideas and recurring campaigns. Generation uses the Runway API key for images, videos, video edits and audio, including the models hosted by Runway. Caption generation and independent factual review still use Claude; visual review, audio listening and duplicate embeddings still use OpenAI.

## Configuration

The ignored root `.env` now contains a blank slot:

```dotenv
RUNWAY_API_KEY=
IMAGE_PROVIDER=runway
VIDEO_PROVIDER=runway
```

Paste the key after `RUNWAY_API_KEY=`. `gsm://projects/PROJECT/secrets/tudelu-runway-api-key/versions/1` is also supported when `SECRET_BACKEND=google`. `.env.example`, `.env.local` and the Droplet template include the same settings. `TUDELU_ENV_FILE` selects a different environment file; the local demo uses `.env.local`. Restart both web and worker after editing configuration. Do not commit keys.

`AI_MODE=live` permits paid generation. The checked-in defaults remain offline fixtures. Creating an idea queues a background job; run the worker to advance it. No API call is made by simply opening a page or saving a paused recurring campaign. `LIVE_PLATFORMS` and the existing account/fact checks separately govern publication.

Before starting updated services, run `tudelu init` (or `alembic upgrade head`) using the intended deployment environment. Migration `9b61d8c4a201` adds `media_jobs` and `media_campaigns` without modifying existing posts. Migration `d3a7f1c58e42` adds the optional asset `owner` column; existing rights text is unchanged. The Droplet Compose migration service runs this upgrade during deployment. Rebuild/recreate both web and worker. This repository update does not deploy to the Droplet or enable live publication.

## Inputs and operations

| Operation | Runway endpoint / model | Inputs |
|---|---|---|
| Image | `text_to_image` / `gen4_image` | Prompt and up to three uploaded images |
| Video | `text_to_video` / `gen4.5` | Prompt; up to 90 seconds using clips of 2–10 seconds |
| Image animation | `image_to_video` / `gen4.5` | Prompt and one uploaded image; up to 90 seconds using clips of 2–10 seconds |
| Video edit | `video_to_video` / `aleph2` | Prompt and one uploaded video; edits up to 90 seconds, split into inputs of at most 30 seconds |
| Audio / audio reworking | `sound_effect` / `seed_audio` | Prompt and up to three uploaded audio references |

The request shapes and model IDs were checked against the [official Runway API specification](https://docs.dev.runwayml.com/openapi.json) and [API reference](https://docs.dev.runwayml.com/api.md) on November 11, 2026. The supported model settings intentionally accept only the implemented schemas. Changing to a different model requires updating its payload contract.

Upload company media in Asset library. Images, videos and MP3/WAV/M4A/OGG/FLAC audio are supported. Usage rights are **Public** (may be published) or **Not public** (internal use and dry runs only); the owner is a separate optional field. A Public idea cannot use a Not public reference, and media generated from a Not public reference is Not public. The gate holds Not public media from live publication. Rights saved as free text before this choice existed show as *Unclassified* and keep their earlier behavior; re-upload an asset to classify it. Objects already uploaded directly into the configured media bucket can be registered using **Import bucket asset** and their exact object key, rights, source and provenance. The existing object remains intact. Reference assets are read from the local store or private bucket, checked against their checksum, and sent through Runway's ephemeral upload API. Runway never receives the bucket credentials. Missing, expired or unpermitted references fail before generation.

## How Runway prompts are written

Creating an idea saves the admin prompt, a rotating creative direction (close-up, wide view, the system in operation, daylight and so on) and, with **Use tudelu.com** checked, a timestamped, hashed snapshot of the tudelu.com homepage: meta descriptions, visible text in page order (so *Coming soon* stays beside its system) and image descriptions. No Runway prompt is written yet.

When the worker starts a visual job, `social/prompting.py` gathers four sources and asks Claude (`ANTHROPIC_MODEL`) to write one Runway prompt:

1. **Rules:** the selected approved facts version (products, brand voice, banned phrases and relevant facts), the platform tone and orientation, and fixed visual rules that match the vision gate (one Tudelu system as the subject, unoccupied scenes, plain unmarked surfaces).
2. **tudelu.com:** the saved snapshot, which lists the systems Tudelu offers.
3. **Uploaded media:** up to 8 company uploads marked Public (matching the product tag first), plus any selected references, described by their observed visual summaries. Uploads without a summary are summarized once with `OPENAI_VISION_MODEL` (at most 6 per prompt) and the summary is kept.
4. **Previous generations:** summaries of the 6 latest Runway originals with their post status. These keep the look consistent and the compositions varied. They never establish what Tudelu sells, and held or deleted ones are never copied.

The writer must name the Tudelu system it depicts. Before anything is sent to Runway, the app checks that the name appears on tudelu.com, in the facts or in uploaded/selected media. A prompt that cannot be grounded fails the job with the reason. Resume it to try again, or edit the idea. The prompt is written in positive language because Runway models do not support negative prompts. It is trimmed to Runway's 1,000-character limit and saved with its sources before the paid request. Resuming never rewrites a prompt that was already submitted. Ideas that were queued before this change get a newly written prompt when they start. Expand **Prompt sent to Runway** on a queue card to see the prompt, the depicted system and its sources. Fixture mode uses a deterministic offline prompt.

Website text and media summaries guide what is shown; they never become approved product claims. The selected immutable facts version remains the source for captions. The custom prompt, concept, content kind and depicted system also accompany caption generation.

For a video soundtrack, enter audio direction and optionally choose audio references. Runway generates/reworks the audio in a second saved task. The app replaces the visual clip's soundtrack with that result, trims it to the clip, and pads a shorter track with silence. Leaving the soundtrack blank retains any audio delivered with the generated/edited video. Audio-only jobs are library assets; select a video operation to create a social post with audio.

## Finished videos up to 90 seconds

The duration field in Media Studio and the next-post idea form accepts 3–90 seconds for immediate, waiting and recurring ideas. New video generation uses an ordered shot plan with clips no longer than 10 seconds; a 90-second video uses nine Runway tasks. Editing 90 seconds of uploaded footage uses three inputs of at most 30 seconds. Each source interval is saved separately with its original checksum, rights and provenance; the uploaded original is unchanged. An edit cannot request more footage than the source provides. Longer source files can supply the requested opening interval.

Each clip's prompt, seed, source interval, Runway task and result asset are saved. Prompts use a shared setting/system with different camera directions. Continuity between independently generated shots is not guaranteed; the finished montage still goes through vision review. Each clip incurs its own Runway generation cost. The queue reports the number of saved clips, and recovery reuses completed clips and known tasks. Ambiguous submissions still require reconciliation before continuing.

Assembly joins clips in order into one H.264 video at the destination dimensions, with continuous timestamps and original clip audio (silence where a clip has no track). An optional generated soundtrack replaces the combined audio. Short or missing generated clips hold the job instead of stretching footage to fill the requested duration. The maximum is also enforced before generation and at publication; configured lower platform limits remain effective. Existing campaigns keep their saved duration until a new brief is saved.

The per-task bounds were checked on November 12, 2026 against [Runway Gen-4.5](https://help.runwayml.com/hc/en-us/articles/46974685288467-Creating-with-Gen-4-5) and [Aleph 2 video editing](https://dev.runwayml.com/learn/edit-and-finish-video-api). Platform references are in [platform verification](platform-verification.md).

## Audio review

Every post check now includes `audio_review` for uploaded and AI media. The exact file checksum is verified, and every audio stream is decoded in full (up to 90 seconds, four mono/stereo tracks). A file without an audio stream passes without a listening call. Unsupported channels, failed decoding, missing coverage or uncertain words hold the post.

`OPENAI_AUDIO_REVIEW_MODEL` (default `gpt-audio-1.5`, using `OPENAI_API_KEY`) listens to the complete PCM WAV tracks and records sounds, speech and sung lyrics. The app saves summaries, transcripts, model, timestamps and checksums in asset metadata. Asset library exposes **Audio summary and transcript**. Audio is untrusted content, and machine listening does not establish music ownership or licensing.

Claude independently checks every transcript segment and its combination with the caption against currently active approved facts and declared rights. Instrumental music, ambience or silence can pass without fact citations. Unclear listening, unsupported claims, incomplete transcript review or provider failures hold publication. Ordinary Recheck runs this step too, and final caption review receives the result alongside vision evidence. The two reviews are independent: passing audio does not clear a vision hold.

Successful observations are reused only for identical file bytes, model and execution mode; partial multitrack work can resume. Factual review runs fresh for each post/check so changed captions or expired facts cannot reuse an old approval. Fixture results are explicitly simulated and cannot satisfy live readiness. Live review adds OpenAI and Anthropic charges. This uses [OpenAI audio inputs](https://developers.openai.com/api/docs/guides/audio-chat-completions) with [gpt-audio-1.5](https://developers.openai.com/api/docs/models/gpt-audio-1.5); no new database migration is required.

## Platform output and provenance

Every Runway image/video original and publishing rendition receives a text summary in `Asset.media.visual_summary`. The configured OpenAI vision model observes the saved bytes; generation prompts are not treated as evidence. Metadata includes the description, readable visible text, source checksum, model, timestamp and limitations. Videos use at most 12 chronological frames, including the final frame; audio and unseen action are not analyzed. Original media bytes and embedded provenance remain unchanged. See [OpenAI image inputs](https://developers.openai.com/api/docs/guides/images-vision).

Caption generation receives this metadata as `visual_context` for mood and relevant framing; it does not become approved product facts or replace the independent checks. Older Runway assets get summaries when captions are next generated. Fixture mode stores explicitly simulated descriptions without provider calls. Live summaries use `OPENAI_API_KEY` and `OPENAI_VISION_MODEL`, with provider charges for the additional analysis. A failed summary leaves saved originals/renditions intact; resume the job to retry analysis without resubmitting to Runway. Summaries are visible in the Asset library.

The Media Studio queue displays loading indicators and refreshes in place while preserving an unfinished creative brief. Post pages detect processing in the web app, another tab or the worker, disable conflicting actions, and refresh completed results. Changed results do not discard unsaved caption edits. Duplicate form requests during an active operation show progress instead of an error or another provider call. Browser scripts/styles use content-versioned URLs so deployments refresh cached loading behavior. These changes use the existing JSON metadata and transaction locks; no additional database migration is needed.

Each selected platform gets its own job, prompt framing, seed and caption. Platform profiles set rendition dimensions: Instagram images default to 1080×1350, other images to 1080×1080; Instagram, TikTok and Facebook (Reels) videos default to 720×1280 and other videos to 1280×720. Runway receives a supported orientation; the app fits the result into the exact configured dimensions with padding, without stretching or discarding the original. Images become JPEG; videos become H.264/yuv420p at 30 fps with AAC audio where present. Normal platform byte/duration/format checks still apply.

Generated originals and reference originals are retained unchanged. A publishing rendition is a new asset linked to source IDs/checksums, rights, expiry, prompt, model, task, website snapshot and campaign. Embedded metadata in originals is retained; a transcoded rendition is not claimed to retain a cryptographic C2PA signature. Native AI-label flags and review checks remain active. Audio receives its own full-track review, described above.

## Custom ideas and recurring campaigns

Use **Overview → Give the next post an idea** to enter an admin prompt, platforms, references, approved facts and usage rights. **Use for next automatic post** saves a `planned` job per platform until **Automatic posts** is on and that platform's media queue is available. The worker activates the oldest waiting idea per platform first, ahead of new recurring campaign batches. You can remove a waiting idea from Media Studio.

**Create now** queues a prioritized request on the next available worker tick. It handles media generation/editing, visual summaries, caption writing, checks and scheduling even when routine automation is off. It does not immediately publish: checks can hold the post, and platform caps, scheduling windows and live/simulated delivery settings still apply. Global and platform pauses remain effective. Progress in Media Studio follows both media and post status without clearing an unfinished prompt.

Media Studio offers the same **Create now** and **Next automatic post** workflows, with additional audio and recurring options. Check **Create posts** for the full workflow; uncheck it for library-only media. Turn off **Overview → Automatic posts** to pause routine caption/check/schedule progression and recurring batches while explicit Create now requests and queued media jobs finish. Already scheduled posts can still publish. Until the UI switch is changed, `AUTO_ADVANCE` supplies the default.

Choose **Recurring campaign** and save a brief with specific formats or **Varied images and videos**, and optionally **Balanced mix**. Campaigns start paused. Enable one in Media Studio when ready. They spend only when `AI_MODE=live`, **Automatic posts** is on, and every selected platform is explicitly live, connected and unpaused. The global stop switch stops both generation and publication. Pausing a campaign stops future batches; existing jobs/posts retain their own queue status.

Every interval (minimum 24 hours), an eligible campaign creates one job per platform. It rotates eight creative directions using recent platform history, selects a new seed, cycles available source references and formats, and plans promotional/editorial content against the platform ratio. TikTok always gets video. Facebook videos publish as Reels with Meta's AI label, so mixed campaigns rotate images and videos there too. The scheduler still enforces its own mix, spacing and capacity limits.

Only one unfinished batch per campaign is allowed. Unfinished, uncertain or failed media work, or held/failed/unsent posts, blocks further batches so repeated failures cannot keep spending. Fix/resume a job, remove it from the queue, or correct the post; check the campaign's displayed reason. Automatic variation improves diversity but does not guarantee visual novelty; the existing caption duplicate checks still run. There is no automatic recurring spending in fixture mode.

## Recovery

For a failed **post**, open it from Needs attention and use **Retry post** once its underlying issue is corrected. The new attempt reuses the saved media and caption, runs fresh checks and schedules normally, including with routine automation off. It preserves the original error/audit and delivery mode, and the campaign follows the retry. No new Runway generation is requested. If publication may already have been accepted, retry is blocked until the result is reconciled. This post retry is separate from resuming an unfinished media job below. Apply migration `f2b6d9a51034` before updating web/worker.

Runway submissions checkpoint `submitting` before the paid call, then save the returned task ID. The worker polls once per tick, downloads completed outputs immediately and resumes from stored originals if rendition/storage processing fails. It never sleeps waiting for a video inside a web request. `MEDIA_JOBS_PER_TICK` bounds work, and `RUNWAY_TASK_TIMEOUT_SECONDS` bounds polling time.

Use **Resume saved task / output** after correcting a download/storage/configuration problem. Known tasks are polled again without another paid submission. An interrupted/ambiguous submission becomes **uncertain** and is never automatically retried; recover the original task ID from Runway and use **Recover original task**. Do not create another job until the first submission is reconciled. Terminal Runway failures require a corrected new brief; resuming a failed remote task only rechecks that same task. Queued or failed work can be removed from the local queue; this does not cancel or refund an already accepted Runway task.

The authenticated, CSRF-protected API also exposes `POST /api/media-jobs` with a `CreativeBrief` JSON object and `GET /api/media-jobs` for status. Neither endpoint returns credentials or expiring signed output URLs.
