from urllib.parse import quote

from ..security import public_url
from .base import BaseAdapter, Deferred, PublishError, PublishResult


class Instagram(BaseAdapter):
    platform = "instagram"

    def delivery_result(self, body, headers):
        permalink = self.checkpoint("permalink")
        return PublishResult(body["id"], permalink[0].get("permalink", "") if permalink else "", "published")

    @property
    def base(self):
        return f"https://graph.facebook.com/{self.s.meta_api_version}"

    def validate_credentials(self):
        return self.direct("GET", f"{self.base}/{self.account.external_id}", params={"fields": "id,username"})

    def get_status(self, remote_id):
        return self.direct("GET", f"{self.base}/{remote_id}", params={"fields": "id,permalink"})

    def limit(self):
        return self.direct(
            "GET",
            f"{self.base}/{self.account.external_id}/content_publishing_limit",
            params={"fields": "config,quota_usage"},
        )

    def _publish(self):
        target = self.account.external_id or "DRY_IG_USER"
        limits, _ = self.request(
            "limit",
            "GET",
            f"{self.base}/{target}/content_publishing_limit",
            params={"fields": "config,quota_usage"},
            fake={"data": [{"quota_usage": 0, "config": {"quota_total": 50}}]},
            volatile=True,
        )
        item = limits["data"][0]
        if item["quota_usage"] >= min(item.get("config", {}).get("quota_total", 50), 50) - 1:
            raise PublishError("transient", "Instagram publishing quota reached", 3600)
        assets = self.assets()
        containers = []
        for i, asset in enumerate(assets):
            url = self.store.url(asset.storage_key)
            if not self.dry:
                public_url(url)
                if not url.startswith(self.s.media_base_url.rstrip("/") + "/"):
                    raise PublishError("validation", "Instagram media outside MEDIA_BASE_URL")
            video = asset.media["mime"].startswith("video/")
            payload = {"video_url" if video else "image_url": url}
            if video:
                payload["media_type"] = "REELS"
            if len(assets) > 1:
                payload["is_carousel_item"] = True
            else:
                payload.update(caption=self.post.caption, is_ai_generated=self.post.ai_disclosure)
            result, _ = self.request(
                f"container-{i}",
                "POST",
                f"{self.base}/{target}/media",
                json=payload,
                fake={"id": f"dry-container-{self.post.id}-{i}"},
            )
            containers.append(result["id"])
            status, _ = self.request(
                f"container-status-{i}",
                "GET",
                f"{self.base}/{result['id']}",
                params={"fields": "status_code"},
                fake={"status_code": "FINISHED"},
                volatile=True,
            )
            if status["status_code"] in {"ERROR", "EXPIRED"}:
                raise PublishError("validation", "Instagram media container failed or expired")
            if status["status_code"] != "FINISHED":
                raise Deferred(60)
        container = containers[0]
        if len(containers) > 1:
            result, _ = self.request(
                "carousel",
                "POST",
                f"{self.base}/{target}/media",
                json={
                    "media_type": "CAROUSEL",
                    "children": ",".join(containers),
                    "caption": self.post.caption,
                    "is_ai_generated": self.post.ai_disclosure,
                },
                fake={"id": f"dry-carousel-{self.post.id}"},
            )
            container = result["id"]
            status, _ = self.request(
                "carousel-status",
                "GET",
                f"{self.base}/{container}",
                params={"fields": "status_code"},
                fake={"status_code": "FINISHED"},
                volatile=True,
            )
            if status["status_code"] in {"ERROR", "EXPIRED"}:
                raise PublishError("validation", "Instagram carousel failed")
            if status["status_code"] != "FINISHED":
                raise Deferred(60)
        result, _ = self.request(
            "publish",
            "POST",
            f"{self.base}/{target}/media_publish",
            json={"creation_id": container},
            fake={"id": f"dry-instagram-{self.post.id}"},
            irreversible=True,
        )
        remote = result["id"]
        result, _ = self.request(
            "permalink",
            "GET",
            f"{self.base}/{remote}",
            params={"fields": "permalink"},
            fake={"permalink": ""},
        )
        return PublishResult(remote, result.get("permalink", ""), "published")


class Facebook(BaseAdapter):
    platform = "facebook"

    def delivery_result(self, body, headers):
        remote = body.get("post_id", body["id"])
        return PublishResult(remote, "" if self.dry else f"https://www.facebook.com/{remote}", "published")

    @property
    def base(self):
        return f"https://graph.facebook.com/{self.s.meta_api_version}"

    def validate_credentials(self):
        return self.direct("GET", f"{self.base}/me", params={"fields": "id,name"})

    def get_status(self, remote_id):
        return self.direct("GET", f"{self.base}/{remote_id}", params={"fields": "id,permalink_url"})

    def _publish(self):
        target = self.account.external_id or "DRY_PAGE"
        asset = self.assets()[0]
        if asset.media["mime"].startswith("video/"):
            if not self.dry and not self.s.meta_app_id:
                raise PublishError("validation", "META_APP_ID required for video uploads")
            # Resumable Upload API uses the System User token, then Page token publishes.
            system_headers = {"Authorization": "OAuth " + self.credentials.get("system_token", "DRY_RUN")}
            result, _ = self.request(
                "video-init",
                "POST",
                f"{self.base}/{self.s.meta_app_id or 'DRY_APP'}/uploads",
                params={
                    "file_name": asset.original_name,
                    "file_length": asset.media["bytes"],
                    "file_type": "video/mp4",
                },
                headers=system_headers,
                fake={"id": "upload:dry-session"},
            )
            result, _ = self.request(
                "video-upload",
                "POST",
                f"{self.base}/{result['id']}",
                headers=system_headers | {"file_offset": "0", "Content-Type": "application/octet-stream"},
                content=self.store.read(asset.storage_key),
                fake={"h": "dry-video-handle"},
            )
            result, _ = self.request(
                "publish",
                "POST",
                f"https://graph-video.facebook.com/{self.s.meta_api_version}/{target}/videos",
                data={
                    "title": self.post.campaign,
                    "description": self.post.caption,
                    "fbuploader_video_file_chunk": result["h"],
                },
                fake={"id": f"dry-facebook-{self.post.id}"},
                irreversible=True,
            )
        else:
            url = self.store.url(asset.storage_key)
            if not self.dry:
                public_url(url)
            result, _ = self.request(
                "publish",
                "POST",
                f"{self.base}/{target}/photos",
                json={"url": url, "message": self.post.caption, "published": True},
                fake={"id": "dry-photo", "post_id": f"dry-facebook-{self.post.id}"},
                irreversible=True,
            )
        remote = result.get("post_id", result["id"])
        return PublishResult(remote, "" if self.dry else f"https://www.facebook.com/{remote}", "published")


class LinkedIn(BaseAdapter):
    platform = "linkedin"
    base = "https://api.linkedin.com/rest"

    def delivery_result(self, body, headers):
        remote = headers.get("x-restli-id")
        if not remote:
            raise PublishError("ambiguous", "LinkedIn publish response missing post ID")
        return PublishResult(
            remote, "" if self.dry else "https://www.linkedin.com/feed/update/" + remote, "published"
        )

    def headers(self):
        return super().headers() | {
            "LinkedIn-Version": self.s.linkedin_api_version,
            "X-Restli-Protocol-Version": "2.0.0",
        }

    @property
    def owner(self):
        return "urn:li:organization:" + (self.s.linkedin_organization_id or "DRY_ORG")

    def validate_credentials(self):
        return self.direct(
            "GET",
            f"{self.base}/organizationAcls",
            params={"q": "roleAssignee", "role": "ADMINISTRATOR", "state": "APPROVED"},
        )

    def get_status(self, remote_id):
        return self.direct("GET", f"{self.base}/posts/{quote(remote_id, safe='')}")

    def _publish(self):
        urns = []
        for i, asset in enumerate(self.assets()):
            data = self.store.read(asset.storage_key)
            video = asset.media["mime"].startswith("video/")
            kind = "videos" if video else "images"
            payload = {"initializeUploadRequest": {"owner": self.owner}}
            if video:
                payload["initializeUploadRequest"].update(
                    fileSizeBytes=len(data), uploadCaptions=False, uploadThumbnail=False
                )
                fake = {
                    "value": {
                        "video": f"urn:li:video:dry{i}",
                        "uploadToken": "",
                        "uploadInstructions": [
                            {
                                "uploadUrl": "https://www.linkedin.com/dms-uploads/dry",
                                "firstByte": 0,
                                "lastByte": len(data) - 1,
                            }
                        ],
                    }
                }
            else:
                fake = {
                    "value": {
                        "image": f"urn:li:image:dry{i}",
                        "uploadUrl": "https://www.linkedin.com/dms-uploads/dry",
                    }
                }
            result, _ = self.request(
                f"media-init-{i}",
                "POST",
                f"{self.base}/{kind}",
                params={"action": "initializeUpload"},
                json=payload,
                fake=fake,
            )
            value = result["value"]
            urn = value["video" if video else "image"]
            if video:
                part_ids = []
                for j, instruction in enumerate(value["uploadInstructions"]):
                    first, last = instruction["firstByte"], instruction["lastByte"]
                    _, headers = self.request(
                        f"media-upload-{i}-{j}",
                        "PUT",
                        instruction["uploadUrl"],
                        content=data[first : last + 1],
                        headers={"Content-Type": "application/octet-stream"},
                        authorized=False,
                        fake_headers={"etag": f"dry-part-{j}"},
                    )
                    part_ids.append(headers["etag"])
                self.request(
                    f"media-finalize-{i}",
                    "POST",
                    f"{self.base}/videos",
                    params={"action": "finalizeUpload"},
                    json={
                        "finalizeUploadRequest": {
                            "video": urn,
                            "uploadToken": value["uploadToken"],
                            "uploadedPartIds": part_ids,
                        }
                    },
                )
            else:
                self.request(
                    f"media-upload-{i}",
                    "PUT",
                    value["uploadUrl"],
                    content=data,
                    headers={"Content-Type": asset.media["mime"]},
                    authorized=True,
                )
            status, _ = self.request(
                f"media-status-{i}",
                "GET",
                f"{self.base}/{kind}/{quote(urn, safe='')}",
                fake={"status": "AVAILABLE"},
                volatile=True,
            )
            if status["status"] == "PROCESSING_FAILED":
                raise PublishError("validation", "LinkedIn media processing failed")
            if status["status"] != "AVAILABLE":
                raise Deferred(60)
            urns.append(urn)
        content = (
            {"media": {"id": urns[0]}}
            if len(urns) == 1
            else {"multiImage": {"images": [{"id": u} for u in urns]}}
        )
        _, headers = self.request(
            "publish",
            "POST",
            f"{self.base}/posts",
            json={
                "author": self.owner,
                "commentary": self.post.caption,
                "visibility": "PUBLIC",
                "distribution": {
                    "feedDistribution": "MAIN_FEED",
                    "targetEntities": [],
                    "thirdPartyDistributionChannels": [],
                },
                "content": content,
                "lifecycleState": "PUBLISHED",
                "isReshareDisabledByAuthor": False,
            },
            fake_headers={"x-restli-id": "urn:li:share:dry-" + self.post.id},
            irreversible=True,
        )
        remote = headers.get("x-restli-id")
        if not remote:
            raise PublishError("ambiguous", "LinkedIn publish response missing post ID")
        return PublishResult(
            remote, "" if self.dry else "https://www.linkedin.com/feed/update/" + remote, "published"
        )


class X(BaseAdapter):
    platform = "x"

    def delivery_result(self, body, headers):
        remote = body["data"]["id"]
        return PublishResult(remote, "" if self.dry else f"https://x.com/i/status/{remote}", "published")

    @property
    def base(self):
        return f"https://api.x.com/{self.s.x_api_version}"

    def validate_credentials(self):
        return self.direct("GET", f"{self.base}/users/me")

    def get_status(self, remote_id):
        return self.direct("GET", f"{self.base}/tweets/{remote_id}")

    def _publish(self):
        ids = []
        for i, asset in enumerate(self.assets()):
            data = self.store.read(asset.storage_key)
            result, _ = self.request(
                f"media-init-{i}",
                "POST",
                f"{self.base}/media/upload/initialize",
                json={
                    "media_type": asset.media["mime"],
                    "total_bytes": len(data),
                    "media_category": "tweet_video"
                    if asset.media["mime"].startswith("video/")
                    else "tweet_image",
                },
                fake={"data": {"id": f"dry-media-{i}"}},
            )
            media_id = result["data"]["id"]
            for segment, offset in enumerate(range(0, len(data), 4 * 1024 * 1024)):
                self.request(
                    f"media-append-{i}-{segment}",
                    "POST",
                    f"{self.base}/media/upload/{media_id}/append",
                    data={"segment_index": str(segment)},
                    files={
                        "media": (
                            asset.original_name,
                            data[offset : offset + 4 * 1024 * 1024],
                            asset.media["mime"],
                        )
                    },
                )
            result, _ = self.request(
                f"media-finalize-{i}",
                "POST",
                f"{self.base}/media/upload/{media_id}/finalize",
                fake={"data": {"id": media_id}},
            )
            if result.get("data", {}).get("processing_info"):
                status, _ = self.request(
                    f"media-status-{i}",
                    "GET",
                    f"{self.base}/media/upload",
                    params={"command": "STATUS", "media_id": media_id},
                    volatile=True,
                    fake={"data": {"processing_info": {"state": "succeeded"}}},
                )
                info = status["data"]["processing_info"]
                if info["state"] == "failed":
                    raise PublishError("validation", "X media processing failed")
                if info["state"] != "succeeded":
                    raise Deferred(max(15, info.get("check_after_secs", 15)))
            ids.append(media_id)
        result, _ = self.request(
            "publish",
            "POST",
            f"{self.base}/tweets",
            json={
                "text": self.post.caption,
                "media": {"media_ids": ids},
                "made_with_ai": self.post.ai_disclosure,
            },
            fake={"data": {"id": f"dry-x-{self.post.id}"}},
            irreversible=True,
        )
        remote = result["data"]["id"]
        return PublishResult(remote, "" if self.dry else f"https://x.com/i/status/{remote}", "published")


class TikTok(BaseAdapter):
    platform = "tiktok"

    def delivery_result(self, body, headers):
        return PublishResult(body["data"]["publish_id"], "", "awaiting_manual")

    @property
    def base(self):
        return f"https://open.tiktokapis.com/{self.s.tiktok_api_version}"

    def validate_credentials(self):
        return self.direct("GET", f"{self.base}/user/info/", params={"fields": "open_id,display_name"})

    def get_status(self, remote_id):
        return self.direct("POST", f"{self.base}/post/publish/status/fetch/", json={"publish_id": remote_id})

    def _publish(self):
        asset = self.assets()[0]
        data = self.store.read(asset.storage_key)
        if self.s.tiktok_transfer == "url":
            url = self.store.url(asset.storage_key)
            if not self.dry:
                public_url(url)
                if not url.startswith(self.s.tiktok_verified_url_prefix.rstrip("/") + "/"):
                    raise PublishError("validation", "TikTok URL must be under verified prefix")
            source = {"source": "PULL_FROM_URL", "video_url": url}
        else:
            # TikTok allows a single chunk below 5 MB; otherwise >=5 MB chunks,
            # with any remainder merged into the final chunk (maximum 128 MB).
            chunk_size = min(len(data), 10_000_000)
            count = max(1, len(data) // chunk_size)
            source = {
                "source": "FILE_UPLOAD",
                "video_size": len(data),
                "chunk_size": chunk_size,
                "total_chunk_count": count,
            }
        result, _ = self.request(
            "inbox-init",
            "POST",
            f"{self.base}/post/publish/inbox/video/init/",
            json={"source_info": source},
            fake={
                "data": {
                    "publish_id": f"dry-tiktok-{self.post.id}",
                    "upload_url": "https://open-upload.tiktokapis.com/video/dry",
                },
                "error": {"code": "ok"},
            },
            irreversible=True,
        )
        info = result["data"]
        if source["source"] == "FILE_UPLOAD":
            for i in range(count):
                first = i * chunk_size
                last = len(data) - 1 if i == count - 1 else first + chunk_size - 1
                self.request(
                    f"inbox-upload-{i}",
                    "PUT",
                    info["upload_url"],
                    content=data[first : last + 1],
                    authorized=False,
                    headers={
                        "Content-Type": "video/mp4",
                        "Content-Length": str(last - first + 1),
                        "Content-Range": f"bytes {first}-{last}/{len(data)}",
                    },
                )
        status, _ = self.request(
            "inbox-status",
            "POST",
            f"{self.base}/post/publish/status/fetch/",
            json={"publish_id": info["publish_id"]},
            volatile=True,
            fake={"data": {"status": "SEND_TO_USER_INBOX"}, "error": {"code": "ok"}},
        )
        if status["data"]["status"] == "FAILED":
            raise PublishError("policy", "TikTok inbox delivery failed; inspect status in platform")
        if status["data"]["status"] not in {"SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"}:
            raise Deferred(30)
        return PublishResult(info["publish_id"], "", "awaiting_manual")
