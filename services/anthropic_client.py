"""One place that builds the Anthropic SDK client.

SDK 2.x (API version 2026-10-21) sends an API key as `Authorization: Bearer`
instead of `X-Api-Key`. The Claude API takes either; an Anthropic-format
gateway behind a custom base URL may read only `X-Api-Key` — which is what
every 1.x request sent. So for a custom base URL the key goes out as
`X-Api-Key` again, through the SDK's documented `auth` hook (it runs on
every request, retries included). First-party requests keep the SDK default.
"""

from __future__ import annotations

from typing import Any


def x_api_key_auth(api_key: str):
    """An httpx auth function that moves the key from `Authorization` back to
    `X-Api-Key` (migration guide: "If a proxy or gateway reads X-Api-Key")."""
    def _auth(request):
        if request.headers.get("Authorization") == f"Bearer {api_key}":
            del request.headers["Authorization"]
            request.headers["X-Api-Key"] = api_key
        return request

    return _auth


def async_client(api_key: str, base_url: str = "", **extra: Any):
    import anthropic

    kwargs: dict[str, Any] = {"api_key": api_key, **extra}
    if base_url:
        kwargs["base_url"] = base_url
        kwargs["http_client"] = anthropic.DefaultAsyncHttpxClient(auth=x_api_key_auth(api_key))
    return anthropic.AsyncAnthropic(**kwargs)
