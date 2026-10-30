"""Liveness (and version, where a service says it) of the services PaperlessBrain
leans on, for the dashboard's system status.

Each check answers `(ok, detail)` or None when the service is not configured
(the row is then left out). A check never raises and never takes long: the
dashboard runs them all at once on every load.
"""

from __future__ import annotations

import time
from urllib.parse import urlsplit

import httpx

from config.settings import settings

TIMEOUT = 3.0


async def paperless() -> tuple[bool, str]:
    """Paperless-ngx sends its version in the `X-Version` header of its API
    answers — not of `/api/` itself, which only redirects (and so showed no
    version). One document of the list is the cheapest answer that has it."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers={
                "Authorization": f"Token {settings.paperless_superuser_token}"}) as c:
            r = await c.get(f"{settings.paperless_url.rstrip('/')}/api/documents/",
                            params={"page_size": 1, "fields": "id"})
        if r.status_code >= 400:
            return False, f"HTTP {r.status_code}"
        version = r.headers.get("x-version", "")
        return True, (f"v{version}" if version and not version.startswith("v") else version) or "Online"
    except Exception:  # noqa: BLE001
        return False, ""


async def searxng() -> tuple[bool, str] | None:
    """SearXNG answers `/healthz` with OK. (Its version is a build string like
    "2026.5.29+4ea5c57a8" — noise on the dashboard.)"""
    host = (settings.searxng_host or "").rstrip("/")
    if not host:
        return None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(f"{host}/healthz")
        return r.status_code < 400, ""
    except Exception:  # noqa: BLE001
        return False, ""


async def whisper() -> tuple[bool, str] | None:
    """Bring-your-own transcription endpoint. OpenAI-compatible servers agree
    on no health route, so any HTTP answer from the host counts as up."""
    url = (settings.whisper_url or "").strip()
    if not url:
        return None
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as c:
            r = await c.get(base + "/")
        return r.status_code < 500, "Online" if r.status_code < 500 else f"HTTP {r.status_code}"
    except Exception:  # noqa: BLE001
        return False, ""


def worker() -> tuple[bool, int] | None:
    """The proactive worker touches a heartbeat file every tick; `(alive,
    seconds since)`. None while the proactive system is off."""
    from config.proactive import is_enabled

    if not is_enabled():
        return None
    from proactive.worker import TICK_SECONDS, heartbeat_path

    try:
        age = int(time.time() - heartbeat_path().stat().st_mtime)
    except OSError:
        return False, -1
    return age <= 3 * TICK_SECONDS, age
