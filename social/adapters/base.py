import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from ..config import profiles
from ..gate import check_disclosure, media_errors, weighted_length
from ..models import Account, Asset, Control, RequestRecord
from ..security import Vault, redact, safe_url
from ..storage import storage


class PublishError(Exception):
    def __init__(self, kind, message, retry_after=0):
        super().__init__(message)
        self.kind, self.retry_after = kind, retry_after


class Deferred(PublishError):
    def __init__(self, seconds=60):
        super().__init__("processing", "Remote media processing", seconds)


@dataclass
class PublishResult:
    platform_post_id: str
    url: str
    status: str


class PublishingAdapter(Protocol):
    def publish(self, post, idempotency_key: str) -> PublishResult: ...
    def get_status(self, remote_id: str) -> dict: ...
    def validate_credentials(self) -> dict: ...


def error_for(response, body, irreversible=False):
    status = response.status_code
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else error
    serialized = str(error or body).lower()
    if status == 429 or code in {4, 17, 32, 613, "rate_limit_exceeded", "spam_risk_too_many_pending_share"}:
        delay = 60
        retry = response.headers.get("retry-after", "")
        try:
            delay = int(retry)
        except ValueError:
            if retry:
                try:
                    delay = max(
                        1, int((parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds())
                    )
                except (ValueError, TypeError):
                    pass
        reset = response.headers.get("x-rate-limit-reset")
        if reset and reset.isdigit():
            delay = max(delay, int(reset) - int(datetime.now(timezone.utc).timestamp()))
        return PublishError("transient", "Platform rate limit", delay)
    if status == 401 or code in {190, "access_token_invalid"}:
        return PublishError("auth", "Platform credentials expired or invalid")
    if (
        status == 403
        or code in {368, 10, 200}
        or any(term in serialized for term in ("policy", "spam_risk", "suspended", "banned", "warning"))
    ):
        return PublishError("policy", "Platform policy/permission rejection; inspect platform dashboard")
    if status >= 500:
        return PublishError(
            "ambiguous" if irreversible else "transient",
            "Platform server error; outcome uncertain" if irreversible else "Platform server error",
        )
    if status >= 400 or (error and code != "ok"):
        return PublishError("validation", "Platform rejected the request")
    return None


class BaseAdapter:
    platform = ""

    def __init__(self, settings, db, post, client=None):
        self.s, self.db, self.post = settings, db, post
        self.account = db.get(Account, self.platform)
        self.store = storage(settings)
        self.vault = Vault(settings)
        self.client = client or httpx.Client(timeout=settings.http_timeout_seconds, follow_redirects=False)
        self.credentials = self.vault.decrypt(self.account.credentials) if self.account.credentials else {}
        self.dry = not self.connected() or not settings.wants_live(self.platform, self.account)

    def connected(self):
        required = bool(
            self.account.validated_at
            and self.credentials.get("access_token")
            and self.s.app_base_url
            and self.s.media_base_url
        )
        if self.platform == "linkedin":
            required = required and bool(
                self.s.linkedin_organization_id
                and self.s.linkedin_client_id
                and self.s.linkedin_client_secret.get_secret_value()
            )
        if self.platform == "x":
            required = required and bool(self.s.x_client_id and self.s.x_client_secret.get_secret_value())
        if self.platform == "tiktok":
            required = required and bool(
                self.s.tiktok_client_id and self.s.tiktok_client_secret.get_secret_value()
            )
            if self.s.tiktok_transfer == "url":
                required = required and bool(self.s.tiktok_verified_url_prefix)
        return bool(required)

    def headers(self):
        return {"Authorization": "Bearer " + self.credentials.get("access_token", "DRY_RUN")}

    def assets(self):
        return [self.db.get(Asset, id) for id in self.post.asset_ids]

    def validate_payload(self, post):
        profile = profiles(self.s)[self.platform]
        errors = media_errors(self.assets(), profile, self.platform)
        if not post.caption or weighted_length(post.caption, self.platform) > profile.max_length:
            errors.append("Invalid caption")
        disclosure_ok, disclosure = check_disclosure(post, self.assets(), live=not self.dry, settings=self.s)
        if not disclosure_ok:
            errors.extend(disclosure["reasons"])
        if errors:
            raise PublishError("validation", "; ".join(errors))

    def publish(self, post, idempotency_key):
        if idempotency_key != post.idempotency_key:
            raise PublishError("validation", "Idempotency key mismatch")
        if post.remote_id:
            return PublishResult(post.remote_id, post.live_url, post.status)
        if post.status != "publishing":
            raise PublishError("validation", "Post must be claimed by the worker")
        recovered = self.recover_delivery()
        if recovered:
            return recovered
        self.validate_payload(post)
        return self._publish()

    def checkpoint(self, step):
        record = self.db.scalar(
            select(RequestRecord).where(RequestRecord.post_id == self.post.id, RequestRecord.step == step)
        )
        if not record or record.state != "done":
            return None
        if record.dry_run != self.dry:
            raise PublishError("validation", "Cannot change execution mode mid-post")
        body = record.response if self.dry else self.vault.decrypt(record.response["encrypted"])
        return body, record.response_headers

    def recover_delivery(self):
        """Recover a durable acknowledgement without repeating uploads, polls or publication."""
        saved = self.checkpoint("inbox-status" if self.platform == "tiktok" else "publish")
        if not saved:
            return None
        if self.platform == "tiktok":
            if saved[0].get("data", {}).get("status") not in {"SEND_TO_USER_INBOX", "PUBLISH_COMPLETE"}:
                return None
            saved = self.checkpoint("inbox-init")
        return self.delivery_result(*saved)

    def guard(self):
        self.db.expire_all()
        if self.db.get(Control, 1).killed or self.db.get(Account, self.platform).paused:
            raise PublishError("paused", "Publishing paused")
        if not self.dry and self.db.get(Account, self.platform).live_enabled is False:
            raise PublishError("paused", "Live posting is switched off for this platform")

    def request(
        self,
        step,
        method,
        url,
        *,
        json=None,
        data=None,
        content=None,
        files=None,
        headers=None,
        params=None,
        fake=None,
        fake_headers=None,
        irreversible=False,
        volatile=False,
        authorized=True,
    ):
        """Durable step checkpoints; never replay an uncertain publish mutation."""
        self.guard()
        record = self.db.scalar(
            select(RequestRecord).where(RequestRecord.post_id == self.post.id, RequestRecord.step == step)
        )
        if record and not volatile:
            if record.dry_run != self.dry:
                raise PublishError("validation", "Cannot change execution mode mid-post")
            if record.state == "done":
                body = record.response if self.dry else self.vault.decrypt(record.response["encrypted"])
                return body, record.response_headers
            if record.state == "pending" and record.irreversible:
                raise PublishError("ambiguous", "Previous publish outcome unknown; reconciliation required")
            if record.state == "rejected":
                raise PublishError("validation", "Terminal platform rejection already recorded")
        actual_headers = (self.headers() if authorized else {}) | (headers or {})
        if json is not None:
            actual_headers.setdefault("Content-Type", "application/json")
        audit_headers = {
            k: ("<redacted>" if k.lower() in {"authorization", "cookie", "x-api-key"} else v)
            for k, v in actual_headers.items()
        }
        audit = {
            "method": method,
            "url": safe_url(url),
            "params": redact(params),
            "headers": audit_headers,
            "json": redact(json),
            "form": redact(data),
            "idempotency_key": self.post.idempotency_key,
        }
        if content is not None:
            audit["binary"] = {
                "bytes": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "asset_keys": [a.storage_key for a in self.assets()],
            }
        if files:
            audit["files"] = {
                k: {
                    "name": value[0],
                    "bytes": len(value[1]),
                    "sha256": hashlib.sha256(value[1]).hexdigest(),
                    "mime": value[2],
                }
                for k, value in files.items()
            }
        if not record:
            record = RequestRecord(
                post_id=self.post.id, step=step, request=audit, dry_run=self.dry, irreversible=irreversible
            )
            self.db.add(record)
        record.request, record.state = audit, "pending"
        self.db.commit()  # Write-ahead marker before any network mutation.
        if self.dry:
            body, response_headers = fake or {}, fake_headers or {}
        else:
            self._validate_host(url, authorized)
            try:
                response = self.client.request(
                    method,
                    url,
                    json=json,
                    data=data,
                    content=content,
                    files=files,
                    headers=actual_headers,
                    params=params,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                record.state = "pending" if irreversible else "retryable"
                self.db.commit()
                raise PublishError(
                    "ambiguous" if irreversible else "transient", "Network request did not complete"
                ) from exc
            try:
                body = response.json() if response.content else {}
            except ValueError:
                body = {}
                if response.is_success and irreversible:
                    raise PublishError("ambiguous", "Publish returned an unreadable response")
            response_headers = {
                k: v
                for k, v in response.headers.items()
                if k.lower()
                in {
                    "etag",
                    "x-restli-id",
                    "retry-after",
                    "x-rate-limit-remaining",
                    "x-rate-limit-reset",
                    "x-app-usage",
                    "x-page-usage",
                    "x-business-use-case-usage",
                }
            }
            failure = error_for(response, body, irreversible)
            if failure:
                record.state = (
                    "retryable"
                    if failure.kind in {"transient", "auth"}
                    else "pending"
                    if failure.kind == "ambiguous"
                    else "rejected"
                )
                record.response = {"encrypted": self.vault.encrypt(body)}
                record.response_headers = response_headers
                self.db.commit()
                raise failure
        record.state = "done"
        record.response = body if self.dry else {"encrypted": self.vault.encrypt(body)}
        record.response_headers = response_headers
        self.db.commit()
        return body, response_headers

    def _validate_host(self, url, authorized):
        p = urlsplit(url)
        hosts = {
            "graph.facebook.com",
            "graph-video.facebook.com",
            "api.linkedin.com",
            "api.x.com",
            "open.tiktokapis.com",
        }
        if p.scheme != "https" or not p.hostname:
            raise PublishError("validation", "Official API requires HTTPS")
        if authorized and p.hostname not in hosts:
            if not (
                self.platform == "linkedin"
                and p.hostname == "www.linkedin.com"
                and p.path.startswith("/dms-uploads/")
            ) and not (
                # Reels byte upload host; receives the Page token as documented by Meta.
                self.platform == "facebook"
                and p.hostname == "rupload.facebook.com"
                and p.path.startswith("/video-upload/")
            ):
                raise PublishError("validation", "Refusing to send credentials outside official API hosts")
        if not authorized and not any(
            p.hostname == h or p.hostname.endswith("." + h)
            for h in ("linkedin.com", "licdn.com", "tiktokapis.com")
        ):
            raise PublishError("validation", "Upload URL outside official platform hosts")

    def direct(self, method, url, **kwargs):
        self._validate_host(url, True)
        headers = self.headers() | kwargs.pop("headers", {})
        response = self.client.request(method, url, headers=headers, **kwargs)
        body = response.json()
        failure = error_for(response, body)
        if failure:
            raise failure
        return body

    def validate_credentials(self):
        raise NotImplementedError

    def get_status(self, remote_id):
        raise NotImplementedError
