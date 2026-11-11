import base64
import hashlib
import secrets
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from sqlalchemy import select, update

from .db import now
from .models import Account, OAuthState
from .monitoring import alert
from .security import Vault, digest

OAUTH = {
    "linkedin": {
        "authorize": "https://www.linkedin.com/oauth/v2/authorization",
        "token": "https://www.linkedin.com/oauth/v2/accessToken",
        "scope": "w_organization_social r_organization_social rw_organization_admin",
    },
    "x": {
        "authorize": "https://x.com/i/oauth2/authorize",
        "token": "https://api.x.com/2/oauth2/token",
        "scope": "tweet.read tweet.write users.read media.write offline.access",
    },
    "tiktok": {
        "authorize": "https://www.tiktok.com/v2/auth/authorize/",
        "token": "https://open.tiktokapis.com/v2/oauth/token/",
        "scope": "user.info.basic,video.upload",
    },
}


def ready(settings, platform):
    return platform in OAUTH and bool(
        settings.app_base_url
        and getattr(settings, platform + "_client_id")
        and getattr(settings, platform + "_client_secret").get_secret_value()
        and settings.token_encryption_key.get_secret_value()
    )


def requested_scopes(settings, platform):
    scope = OAUTH[platform]["scope"]
    if platform == "tiktok" and settings.analytics_enabled:
        scope += ",video.list"
    return scope


def begin(db, settings, platform, browser_nonce):
    if not ready(settings, platform):
        raise ValueError("Not connected: configure app URL, client ID/secret and encryption key first")
    if not settings.app_base_url.startswith("https://"):
        raise ValueError("OAuth redirect requires HTTPS APP_BASE_URL")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64) if platform == "x" else ""
    db.add(
        OAuthState(
            state_hash=digest(state),
            platform=platform,
            browser_hash=digest(browser_nonce),
            verifier=Vault(settings).encrypt(verifier),
            expires_at=now() + timedelta(minutes=10),
        )
    )
    params = {
        "response_type": "code",
        "client_key" if platform == "tiktok" else "client_id": getattr(settings, platform + "_client_id"),
        "redirect_uri": settings.app_base_url.rstrip("/") + f"/oauth/{platform}/callback",
        "scope": requested_scopes(settings, platform),
        "state": state,
    }
    if platform == "x":
        params.update(
            code_challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("="),
            code_challenge_method="S256",
        )
    db.commit()
    return OAUTH[platform]["authorize"] + "?" + urlencode(params)


def token_call(settings, platform, data, client=None):
    data = dict(data)
    client_id = getattr(settings, platform + "_client_id")
    secret = getattr(settings, platform + "_client_secret").get_secret_value()
    auth = None
    if platform == "x":
        auth = httpx.BasicAuth(client_id, secret)
        data["client_id"] = client_id
    else:
        data["client_key" if platform == "tiktok" else "client_id"] = client_id
        data["client_secret"] = secret
    client = client or httpx.Client(timeout=settings.http_timeout_seconds)
    response = client.post(OAUTH[platform]["token"], data=data, auth=auth)
    if not response.is_success:
        raise ValueError(f"Token exchange failed (HTTP {response.status_code}); reconnect the account")
    payload = response.json()
    if payload.get("error") or not payload.get("access_token") or not payload.get("expires_in"):
        raise ValueError("Token response is invalid")
    if platform == "x" and not payload.get("refresh_token"):
        raise ValueError("X offline.access refresh token missing")
    return payload


def store_tokens(db, settings, account, payload, refreshing=False):
    vault = Vault(settings)
    old = vault.decrypt(account.credentials) if account.credentials else {}
    if payload.get("scope"):
        actual = set(payload["scope"].replace(",", " ").split())
        required = set(requested_scopes(settings, account.platform).replace(",", " ").split())
        if not required <= actual:
            raise ValueError("Required publishing scopes were not granted")
    allowed = {
        k: payload[k]
        for k in ("access_token", "refresh_token", "scope", "open_id", "token_type")
        if k in payload
    }
    account.credentials = vault.encrypt(old | allowed)
    account.expires_at = now() + timedelta(seconds=int(payload["expires_in"]))
    ttl = payload.get("refresh_expires_in", payload.get("refresh_token_expires_in"))
    if ttl is not None:
        expiry = now() + timedelta(seconds=int(ttl))
        # LinkedIn refresh lifetimes do not restart on refresh.
        if refreshing and account.platform == "linkedin" and account.refresh_expires_at:
            expiry = min(expiry, account.refresh_expires_at)
        account.refresh_expires_at = expiry
    account.auth_error = ""
    db.flush()


def callback(db, settings, platform, state, code, browser_nonce, client=None):
    record = db.get(OAuthState, digest(state))
    if (
        not record
        or record.platform != platform
        or record.consumed
        or record.expires_at < now()
        or record.browser_hash != digest(browser_nonce)
    ):
        raise ValueError("Invalid, expired, replayed or unbound OAuth state")
    changed = db.execute(
        update(OAuthState)
        .where(OAuthState.state_hash == record.state_hash, OAuthState.consumed.is_(False))
        .values(consumed=True)
    )
    if changed.rowcount != 1:
        raise ValueError("OAuth state already consumed")
    verifier = Vault(settings).decrypt(record.verifier)
    db.commit()
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.app_base_url.rstrip("/") + f"/oauth/{platform}/callback",
    }
    if platform == "x":
        data["code_verifier"] = verifier
    payload = token_call(settings, platform, data, client)
    account = db.get(Account, platform)
    store_tokens(db, settings, account, payload)
    from .adapters import ADAPTERS

    probe = ADAPTERS[platform](settings, db, None, client=client)
    identity = probe.validate_credentials()
    if platform == "x":
        external_id = identity["data"]["id"]
    elif platform == "tiktok":
        external_id = identity["data"]["user"]["open_id"]
    else:
        owner = "urn:li:organization:" + settings.linkedin_organization_id
        if not settings.linkedin_organization_id or not any(
            e.get("organization") == owner for e in identity.get("elements", [])
        ):
            raise ValueError("Configured organization is not administered by the connected LinkedIn user")
        external_id = settings.linkedin_organization_id
    account.external_id = external_id
    account.validated_at = now()
    db.commit()
    return account


def save_meta(db, settings, token, page_id, instagram_id, client=None):
    if not token or not page_id or not instagram_id:
        raise ValueError("System User token, Page ID and Instagram user ID are required")
    if client is None:
        with httpx.Client(timeout=settings.http_timeout_seconds, follow_redirects=False) as owned:
            return save_meta(db, settings, token, page_id, instagram_id, owned)
    base = f"https://graph.facebook.com/{settings.meta_api_version}"
    response = client.get(
        f"{base}/{page_id}",
        params={"fields": "id,access_token,instagram_business_account"},
        headers={"Authorization": "Bearer " + token},
    )
    if not response.is_success:
        raise ValueError("Meta System User validation failed")
    page = response.json()
    if (
        page.get("instagram_business_account", {}).get("id") != instagram_id
        or page.get("id") != page_id
        or not page.get("access_token")
    ):
        raise ValueError("Page token or linked Instagram account does not match")
    headers = {"Authorization": "Bearer " + page["access_token"]}
    for external_id in (page_id, instagram_id):
        result = client.get(f"{base}/{external_id}", params={"fields": "id"}, headers=headers)
        if not result.is_success or result.json().get("id") != external_id:
            raise ValueError("Meta Page/Instagram validation failed")
    vault = Vault(settings)
    for platform, external_id in (("facebook", page_id), ("instagram", instagram_id)):
        account = db.get(Account, platform)
        account.credentials = vault.encrypt({"access_token": page["access_token"], "system_token": token})
        account.external_id, account.validated_at, account.auth_error = external_id, now(), ""
    db.commit()


def import_meta_from_env(db, settings, client=None, *, force=False):
    """Validate configured Meta credentials on startup without reconnecting on every restart."""
    token = settings.meta_system_user_token.get_secret_value().strip()
    page_id, instagram_id = settings.meta_page_id.strip(), settings.instagram_user_id.strip()
    if not any((token, page_id, instagram_id)):
        return False

    # Serialize web/worker startup. A second process observes the first process's saved connection.
    if db.get_bind().dialect.name == "sqlite":
        db.execute(
            update(Account).where(Account.platform == "facebook").values(auth_error=Account.auth_error)
        )
    else:
        db.scalar(select(Account).where(Account.platform == "facebook").with_for_update())
    accounts = [db.get(Account, p) for p in ("facebook", "instagram")]
    for account in accounts:
        db.refresh(account)
    if not force and any(a.auth_error == "Disconnected" for a in accounts):
        db.rollback()
        return False  # An explicit UI disconnect must survive restarts.

    complete = all((token, page_id, instagram_id))
    try:
        if not complete:
            raise ValueError(
                "Set META_SYSTEM_USER_TOKEN, META_PAGE_ID and INSTAGRAM_USER_ID together in .env"
            )
        if not settings.token_encryption_key.get_secret_value():
            raise ValueError("Set TOKEN_ENCRYPTION_KEY before importing Meta credentials")
        vault = Vault(settings)
        if not force:
            matches = []
            for account, external_id in zip(accounts, (page_id, instagram_id)):
                credentials = vault.decrypt(account.credentials) if account.credentials else {}
                matches.append(
                    bool(
                        account.validated_at
                        and not account.auth_error
                        and account.external_id == external_id
                        and credentials.get("access_token")
                        and secrets.compare_digest(credentials.get("system_token", ""), token)
                    )
                )
            if all(matches):
                db.rollback()
                return True
        save_meta(db, settings, token, page_id, instagram_id, client)
        return True
    except Exception as exc:
        db.rollback()
        if isinstance(exc, httpx.HTTPError):
            message = "Could not reach Meta to validate .env credentials. Restart or use Connect from environment to retry."
        elif isinstance(exc, ValueError):
            # JSON parsing errors can contain response content; only expose our fixed validation messages.
            allowed = {
                "Set META_SYSTEM_USER_TOKEN, META_PAGE_ID and INSTAGRAM_USER_ID together in .env",
                "Set TOKEN_ENCRYPTION_KEY before importing Meta credentials",
                "Meta System User validation failed",
                "Page token or linked Instagram account does not match",
                "Meta Page/Instagram validation failed",
            }
            message = str(exc) if str(exc) in allowed else "Meta returned an invalid validation response"
        else:
            message = "Meta environment import failed. Check credentials and TOKEN_ENCRYPTION_KEY."
        for account in accounts:
            db.refresh(account)
            # Partial env config must not invalidate a working UI-managed connection.
            if complete or not account.credentials:
                account.validated_at = None
                account.auth_error = message
        alert(db, "meta_connection", message, f"meta-env:{digest(message)}:{now().date()}")
        db.commit()
        return False


def refresh_account(db, settings, platform, client=None, force=False):
    account = db.scalar(select(Account).where(Account.platform == platform).with_for_update())
    if not account.credentials or platform not in OAUTH:
        return False
    if not force and account.expires_at and account.expires_at > now() + timedelta(minutes=15):
        return False
    try:
        creds = Vault(settings).decrypt(account.credentials)
        if not creds.get("refresh_token"):
            raise ValueError("No refresh token available; reconnect")
        if account.refresh_expires_at and account.refresh_expires_at <= now():
            raise ValueError("Refresh token expired; reconnect")
        payload = token_call(
            settings,
            platform,
            {"grant_type": "refresh_token", "refresh_token": creds["refresh_token"]},
            client,
        )
        store_tokens(db, settings, account, payload, refreshing=True)
        account.validated_at = now()
        db.commit()
        return True
    except Exception as exc:
        db.rollback()
        account = db.get(Account, platform)
        account.auth_error = "Token refresh failed; reconnect via Connections"
        account.paused = True
        alert(
            db,
            "auth_failure",
            f"{platform}: token refresh failed ({type(exc).__name__}); publishing paused",
            f"refresh:{platform}:{now().date()}",
        )
        db.commit()
        return False


def refresh_all(db, settings):
    for platform in OAUTH:
        account = db.get(Account, platform)
        expiry = account.refresh_expires_at or account.expires_at
        if expiry and expiry <= now() + timedelta(days=settings.expiry_alert_days):
            alert(
                db,
                "auth_expiry",
                f"{platform}: authorization expires {expiry.isoformat()} UTC; reconnect if needed",
                f"expiry:{platform}:{expiry.date()}:{now().date()}",
            )
            db.commit()
        if not account.auth_error:
            refresh_account(db, settings, platform)
