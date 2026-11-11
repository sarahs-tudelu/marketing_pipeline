"""Official Runway task API. Submissions are never automatically retried."""

import hashlib
import re
from pathlib import Path
from urllib.parse import urljoin

import httpx

from .provider_errors import ProviderError, request_json
from .security import public_url
from .storage import storage

BASE = "https://api.dev.runwayml.com/v1"


class Runway:
    def __init__(self, settings, client=None):
        self.s = settings
        self.client = client or httpx.Client(timeout=settings.http_timeout_seconds, follow_redirects=False)
        self.owned = client is None

    def close(self):
        if self.owned:
            self.client.close()

    @property
    def headers(self):
        key = self.s.runway_api_key.get_secret_value()
        if not key:
            raise ValueError(
                "RUNWAY_API_KEY is missing. Add it to the selected .env file and restart web/worker."
            )
        return {"Authorization": "Bearer " + key, "X-Runway-Version": self.s.runway_api_version}

    def reference(self, asset):
        """Upload verified original bytes, including assets in a private media bucket."""
        data = storage(self.s).read(asset.storage_key)
        if hashlib.sha256(data).hexdigest() != asset.checksum:
            raise ValueError("Reference asset integrity check failed")
        upload = request_json(
            self.client,
            "Runway",
            BASE + "/uploads",
            headers=self.headers,
            payload={"filename": Path(asset.storage_key).name, "type": "ephemeral"},
        )
        try:
            public_url(upload["uploadUrl"])
            if not upload["runwayUri"].startswith("runway://") or not isinstance(upload["fields"], dict):
                raise ValueError()
            # Upload host receives signed form fields, never the Runway API key.
            response = self.client.post(
                upload["uploadUrl"],
                data=upload["fields"],
                files={"file": (asset.original_name, data, asset.media["mime"])},
            )
            response.raise_for_status()
            return upload["runwayUri"]
        except (KeyError, ValueError, httpx.HTTPError):
            raise ProviderError(
                "Runway reference upload failed. Check the asset format and connection.",
                provider="Runway",
                category="upload",
            ) from None

    def submit(self, endpoint, payload):
        result = request_json(
            self.client, "Runway", BASE + "/" + endpoint, headers=self.headers, payload=payload
        )
        task_id = result.get("id", "")
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id):
            raise ProviderError(
                "Runway did not return a task ID. Check Runway before submitting again.",
                provider="Runway",
                category="response",
            )
        return task_id

    def task(self, task_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id):
            raise ValueError("Invalid Runway task ID")
        try:
            response = self.client.get(BASE + "/tasks/" + task_id, headers=self.headers)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (ValueError, httpx.HTTPError):
            raise ProviderError(
                "Could not read Runway task status. The saved task can be checked again.",
                provider="Runway",
                category="poll",
            ) from None

    def download(self, url):
        # Output URLs expire. Ingest promptly; never persist signed URLs or forward API credentials.
        try:
            for _ in range(5):
                public_url(url)
                with self.client.stream("GET", url) as response:
                    if response.is_redirect:
                        url = urljoin(url, response.headers["location"])
                        continue
                    response.raise_for_status()
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > self.s.max_upload_bytes:
                            raise ValueError("Generated file exceeds MAX_UPLOAD_BYTES")
                        chunks.append(chunk)
                    return b"".join(chunks)
            raise ValueError("Too many output redirects")
        except (ValueError, httpx.HTTPError):
            raise ProviderError(
                "Could not download the Runway output within the media size limit. "
                "Retry the saved task before its output expires.",
                provider="Runway",
                category="download",
            ) from None


def payload_for(settings, brief, platform, references, *, audio=False):
    """Only emit fields supported by the selected model's official request schema."""
    from .config import profiles

    p = profiles(settings)[platform]
    prompt = brief["audio_prompt"] if audio else brief["generation_prompt"]
    if audio or brief["operation"] == "audio":
        result = {"model": settings.runway_audio_model, "promptText": prompt, "outputFormat": "mp3"}
        if references:
            result["referenceAudios"] = references
        return "sound_effect", result
    result = {"promptText": prompt, "seed": brief["seed"]}
    if brief["operation"] == "image":
        result.update(
            model=settings.runway_image_model,
            ratio="1080:1440" if p.image_height > p.image_width else "1080:1080",
        )
        if references:
            result["referenceImages"] = [{"uri": uri, "tag": f"ref{i}"} for i, uri in enumerate(references)]
        return "text_to_image", result
    if brief["operation"] == "video_edit":
        result.update(
            model=settings.runway_edit_model,
            videoUri=references[0],
            outputFormat="mp4",
            targetAspectRatio="9:16" if p.video_height > p.video_width else "16:9",
        )
        return "video_to_video", result
    result.update(
        model=settings.runway_video_model,
        duration=brief["duration"],
        outputFormat="mp4",
        ratio="720:1280" if p.video_height > p.video_width else "1280:720",
    )
    if references:
        result["promptImage"] = references[0]
        return "image_to_video", result
    return "text_to_video", result
