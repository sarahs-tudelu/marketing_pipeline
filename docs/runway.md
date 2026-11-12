# Runway Media Studio

Media Studio (`/admin/media-studio`) handles custom ideas and recurring campaigns. Generation uses the Runway API key for images, videos, video edits and audio, including the models hosted by Runway. Caption generation and independent factual review still use Claude; visual review and duplicate embeddings still use OpenAI.

## Configuration

The ignored root `.env` now contains a blank slot:

```dotenv
RUNWAY_API_KEY=
IMAGE_PROVIDER=runway
VIDEO_PROVIDER=runway
```

Paste the key after `RUNWAY_API_KEY=`. `gsm://projects/PROJECT/secrets/tudelu-runway-api-key/versions/1` is also supported when `SECRET_BACKEND=google`. `.env.example`, `.env.local` and the Droplet template include the same settings. `TUDELU_ENV_FILE` selects a different environment file; the local demo uses `.env.local`. Restart both web and worker after editing configuration. Do not commit keys.

`AI_MODE=live` permits paid generation. The checked-in defaults remain offline fixtures. Creating an idea queues a background job; run the worker to advance it. No API call is made by simply opening a page or saving a paused recurring campaign. `LIVE_PLATFORMS` and the existing account/fact checks separately govern publication.

Before starting updated services, run `tudelu init` (or `alembic upgrade head`) using the intended deployment environment. Migration `9b61d8c4a201` adds `media_jobs` and `media_campaigns` without modifying existing posts. The Droplet Compose migration service runs this upgrade during deployment. Rebuild/recreate both web and worker. This repository update does not deploy to the Droplet or enable live publication.

## Inputs and operations

| Operation | Runway endpoint / model | Inputs |
|---|---|---|
| Image | `text_to_image` / `gen4_image` | Prompt and up to three uploaded images |
| Video | `text_to_video` / `gen4.5` | Prompt; 3–10 seconds |
| Image animation | `image_to_video` / `gen4.5` | Prompt and one uploaded image; 3–10 seconds |
| Video edit | `video_to_video` / `aleph2` | Prompt and one uploaded video up to 30 seconds; publishing rendition trimmed to requested duration |
| Audio / audio reworking | `sound_effect` / `seed_audio` | Prompt and up to three uploaded audio references |

The request shapes and model IDs were checked against the [official Runway API specification](https://docs.dev.runwayml.com/openapi.json) and [API reference](https://docs.dev.runwayml.com/api.md) on November 11, 2026. The supported model settings intentionally accept only the implemented schemas. Changing to a different model requires updating its payload contract.

Upload company media in Asset library. Images, videos and MP3/WAV/M4A/OGG/FLAC audio are supported. Objects already uploaded directly into the configured media bucket can be registered using **Import bucket asset** and their exact object key, rights, source and provenance. The existing object remains intact. Reference assets are read from the local store or private bucket, checked against their checksum, and sent through Runway's ephemeral upload API. Runway never receives the bucket credentials. Missing, expired or unpermitted references fail before generation.

With website inspiration checked, each batch saves a timestamped, hashed snapshot of the title, description and headings from `https://tudelu.com/`. That text guides the visual mood; it does not become approved product evidence. Uploaded images provide actual visual reference material. The selected immutable facts version remains the source for claims and captions. The full custom prompt, concept and content kind also accompany caption generation.

For a video soundtrack, enter audio direction and optionally choose audio references. Runway generates/reworks the audio in a second saved task. The app replaces the visual clip's soundtrack with that result, trims it to the clip, and pads a shorter track with silence. Leaving the soundtrack blank retains any audio delivered with the generated/edited video. Audio-only jobs are library assets; select a video operation to create a social post with audio.

## Platform output and provenance

Every Runway image/video original and publishing rendition receives a text summary in `Asset.media.visual_summary`. The configured OpenAI vision model observes the saved bytes; generation prompts are not treated as evidence. Metadata includes the description, readable visible text, source checksum, model, timestamp and limitations. Videos use at most 12 chronological frames, including the final frame; audio and unseen action are not analyzed. Original media bytes and embedded provenance remain unchanged. See [OpenAI image inputs](https://developers.openai.com/api/docs/guides/images-vision).

Caption generation receives this metadata as `visual_context` for mood and relevant framing; it does not become approved product facts or replace the independent checks. Older Runway assets get summaries when captions are next generated. Fixture mode stores explicitly simulated descriptions without provider calls. Live summaries use `OPENAI_API_KEY` and `OPENAI_VISION_MODEL`, with provider charges for the additional analysis. A failed summary leaves saved originals/renditions intact; resume the job to retry analysis without resubmitting to Runway. Summaries are visible in the Asset library.

The Media Studio queue displays loading indicators and refreshes in place while preserving an unfinished creative brief. Post pages detect processing in the web app, another tab or the worker, disable conflicting actions, and refresh completed results. Changed results do not discard unsaved caption edits. Duplicate form requests during an active operation show progress instead of an error or another provider call. Browser scripts/styles use content-versioned URLs so deployments refresh cached loading behavior. These changes use the existing JSON metadata and transaction locks; no additional database migration is needed.

Each selected platform gets its own job, prompt framing, seed and caption. Platform profiles set rendition dimensions: Instagram images default to 1080×1350, other images to 1080×1080; Instagram/TikTok videos default to 720×1280 and other videos to 1280×720. Runway receives a supported orientation; the app fits the result into the exact configured dimensions with padding, without stretching or discarding the original. Images become JPEG; videos become H.264/yuv420p at 30 fps with AAC audio where present. Normal platform byte/duration/format checks still apply.

Generated originals and reference originals are retained unchanged. A publishing rendition is a new asset linked to source IDs/checksums, rights, expiry, prompt, model, task, website snapshot and campaign. Embedded metadata in originals is retained; a transcoded rendition is not claimed to retain a cryptographic C2PA signature. Native AI-label flags and review checks remain active. Audio is not transcribed or semantically verified by the existing visual reviewer.

## Custom ideas and recurring campaigns

Use **Overview → Give the next post an idea** to enter an admin prompt, platforms, references, approved facts and usage rights. **Use for next automatic post** saves a `planned` job per platform until **Automatic posts** is on and that platform's media queue is available. The worker activates the oldest waiting idea per platform first, ahead of new recurring campaign batches. You can remove a waiting idea from Media Studio.

**Create now** queues a prioritized request on the next available worker tick. It handles media generation/editing, visual summaries, caption writing, checks and scheduling even when routine automation is off. It does not immediately publish: checks can hold the post, and platform caps, scheduling windows and live/simulated delivery settings still apply. Global and platform pauses remain effective. Progress in Media Studio follows both media and post status without clearing an unfinished prompt.

Media Studio offers the same **Create now** and **Next automatic post** workflows, with additional audio and recurring options. Check **Create posts** for the full workflow; uncheck it for library-only media. Turn off **Overview → Automatic posts** to pause routine caption/check/schedule progression and recurring batches while explicit Create now requests and queued media jobs finish. Already scheduled posts can still publish. Until the UI switch is changed, `AUTO_ADVANCE` supplies the default.

Choose **Recurring campaign** and save a brief with specific formats or **Varied images and videos**, and optionally **Balanced mix**. Campaigns start paused. Enable one in Media Studio when ready. They spend only when `AI_MODE=live`, **Automatic posts** is on, and every selected platform is explicitly live, connected and unpaused. The global stop switch stops both generation and publication. Pausing a campaign stops future batches; existing jobs/posts retain their own queue status.

Every interval (minimum 24 hours), an eligible campaign creates one job per platform. It rotates eight creative directions using recent platform history, selects a new seed, cycles available source references and formats, and plans promotional/editorial content against the platform ratio. TikTok always gets video. A live mixed Facebook campaign uses images because the existing Facebook adapter cannot set the required AI-video label. The scheduler still enforces its own mix, spacing and capacity limits.

Only one unfinished batch per campaign is allowed. Unfinished, uncertain or failed media work, or held/failed/unsent posts, blocks further batches so repeated failures cannot keep spending. Fix/resume a job, remove it from the queue, or correct the post; check the campaign's displayed reason. Automatic variation improves diversity but does not guarantee visual novelty; the existing caption duplicate checks still run. There is no automatic recurring spending in fixture mode.

## Recovery

Runway submissions checkpoint `submitting` before the paid call, then save the returned task ID. The worker polls once per tick, downloads completed outputs immediately and resumes from stored originals if rendition/storage processing fails. It never sleeps waiting for a video inside a web request. `MEDIA_JOBS_PER_TICK` bounds work, and `RUNWAY_TASK_TIMEOUT_SECONDS` bounds polling time.

Use **Resume saved task / output** after correcting a download/storage/configuration problem. Known tasks are polled again without another paid submission. An interrupted/ambiguous submission becomes **uncertain** and is never automatically retried; recover the original task ID from Runway and use **Recover original task**. Do not create another job until the first submission is reconciled. Terminal Runway failures require a corrected new brief; resuming a failed remote task only rechecks that same task. Queued or failed work can be removed from the local queue; this does not cancel or refund an already accepted Runway task.

The authenticated, CSRF-protected API also exposes `POST /api/media-jobs` with a `CreativeBrief` JSON object and `GET /api/media-jobs` for status. Neither endpoint returns credentials or expiring signed output URLs.
