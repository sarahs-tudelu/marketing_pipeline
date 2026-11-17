"""Request current Studio access using only the CRM session cookie."""
import re

import httpx
from fastapi import HTTPException


async def identity(settings, cookie):
    if not cookie or not re.fullmatch(r"[A-Za-z0-9._~-]{1,4096}", cookie):
        raise HTTPException(401, "Sign in to the CRM to open Social Studio")
    try:
        async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
            response = await client.get(settings.crm_access_url, cookies={settings.crm_session_cookie: cookie})
        if response.status_code in {401, 403}:
            raise HTTPException(response.status_code, "CRM Social Studio access required")
        response.raise_for_status()
        data = response.json()
        if (not isinstance(data, dict) or type(data.get("id")) is not int or data["id"] <= 0
                or data.get("role") not in ("admin", "editor") or not isinstance(data.get("label"), str)
                or not 1 <= len(data["label"]) <= 320):
            raise ValueError("Invalid identity response")
        return f"crm:{data['id']}", data["label"], data["role"]
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, "CRM sign-in is temporarily unavailable") from None


def editor_route(method, path):
    if method == "GET":
        return path in {"/", "/admin/create", "/api/session", "/api/posts"} or bool(
            re.fullmatch(r"/posts/[A-Za-z0-9-]+(?:/media/[A-Za-z0-9-]+)?", path)
        )
    return method == "POST" and path in {"/forms/ideas/plan", "/forms/ideas/create"}


def editor_asset(asset, actor):
    if not asset:
        return False
    if asset.tags.get("uploaded_with_idea"):
        return asset.tags.get("submitted_by") == actor
    return (asset.source == "company" and asset.rights == "public"
            and not any(asset.tags.get(key) for key in ("media_job_id", "source_asset_ids", "rendition")))
