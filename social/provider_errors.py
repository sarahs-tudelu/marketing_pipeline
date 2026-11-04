"""Provider failures safe to show in the admin app and structured audit logs."""

import re

import httpx


class ProviderError(ValueError):
    def __init__(self, message, *, provider, category, status=502, upstream_status=None, request_id=""):
        super().__init__(message)
        self.provider = provider
        self.category = category
        self.status = status
        self.upstream_status = upstream_status
        self.request_id = request_id if re.fullmatch(r"[A-Za-z0-9_-]{1,100}", request_id) else ""


def request_json(client, provider, url, *, headers, payload):
    try:
        response = client.post(url, headers=headers, json=payload)
    except httpx.TimeoutException:
        raise ProviderError(
            f"{provider} timed out. The request may still be processing; check its status before retrying.",
            provider=provider,
            category="timeout",
            status=504,
        ) from None
    except httpx.RequestError:
        raise ProviderError(
            f"Could not reach {provider}. Check the server's outbound HTTPS connection.",
            provider=provider,
            category="connection",
        ) from None
    if response.is_error:
        # Never echo provider messages, request headers, prompts or response bodies.
        code = ""
        try:
            error = response.json().get("error", {})
            if isinstance(error, dict):
                code = error.get("code") or error.get("type")
        except (ValueError, AttributeError):
            pass
        status = response.status_code
        category, hint = "request", "rejected the request. Check the configured model and request settings."
        if status == 401:
            category, hint = (
                "authentication",
                "rejected the API key. Check its Secret Manager value and restart the app after correcting it.",
            )
        elif status == 403:
            category, hint = (
                "permission",
                "denied access. Check the API project's permissions, model access and any required organization verification.",
            )
        elif code in ("content_policy_violation", "moderation_blocked"):
            category, hint = (
                "policy",
                "declined this content under its policy. Review the prompt before submitting again.",
            )
        elif status == 404 or code == "model_not_found":
            category, hint = (
                "model",
                "could not find or access the configured model. Check the model setting and the API project's model access.",
            )
        elif status == 429:
            category, hint = (
                "limit",
                "reported a quota or rate limit. Check API usage and billing limits before retrying.",
            )
        elif status >= 500:
            category, hint = "service", "reported a service error. Try again after the service recovers."
        raise ProviderError(
            f"{provider} {hint}",
            provider=provider,
            category=category,
            upstream_status=status,
            request_id=response.headers.get("x-request-id") or response.headers.get("request-id", ""),
        )
    try:
        result = response.json()
        if not isinstance(result, dict):
            raise ValueError
        return result
    except ValueError:
        raise ProviderError(
            f"{provider} returned an unreadable response. No content was saved.",
            provider=provider,
            category="response",
        ) from None
