# services/model_host.py
"""The local model host, as far as paperless-brain is concerned.

paperless-brain talks to any Ollama/OpenAI-compatible endpoint and does no
power management itself. A GPU host that sleeps gets a wake-on-request proxy
in front of it (docs/operations.md, "Sleeping GPU host"). This module knows
the optional parts of that proxy contract:

- `proxy_status(url)`: `GET /proxy/status` → `{"host": "asleep|waking|up|
  shutting_down", …}`, or None when the endpoint is a plain server (404) or
  unreachable. Never wakes the host.
- `headers()`: request headers for local inference. The worker's requests
  carry `X-Idle-Profile: background` (the proxy shuts a host it woke for them
  down sooner), and inside `no_wake()` also `X-No-Wake: 1` (the proxy answers
  503 instead of waking a sleeping host). A plain Ollama ignores both.
- `READ_TIMEOUT_S`: a local request may be held while the host boots and
  loads the model; read timeouts for local inference must cover that.
"""

from __future__ import annotations

import contextlib
import contextvars
from urllib.parse import urlsplit

import httpx

#: The proxy's default wake timeout is 240 s; boot + model load + margin.
READ_TIMEOUT_S = 330.0

_no_wake: contextvars.ContextVar[bool] = contextvars.ContextVar("model_host_no_wake", default=False)


def _base(url: str) -> str:
    """`http://h:11434/v1` → `http://h:11434` (scheme + host + port only)."""
    url = (url or "").strip()
    if not url:
        return ""
    p = urlsplit(url if "://" in url else f"http://{url}")
    return f"{p.scheme}://{p.netloc}" if p.netloc else ""


async def proxy_status(url: str, timeout: float = 3.0) -> dict | None:
    """The proxy's status for the endpoint `url`, or None (plain server,
    unreachable, or not a proxy answer)."""
    base = _base(url)
    if not base:
        return None
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(base + "/proxy/status")
    except (httpx.HTTPError, OSError):
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) and "host" in data else None


@contextlib.contextmanager
def no_wake(on: bool = True):
    """Requests made inside must not wake a sleeping host."""
    token = _no_wake.set(on)
    try:
        yield
    finally:
        _no_wake.reset(token)


def headers() -> dict[str, str]:
    """Headers for a request to a local model host (merge into the call's own)."""
    from services import gpu_yield

    h: dict[str, str] = {}
    if gpu_yield.role() == "worker":
        h["X-Idle-Profile"] = "background"
    if _no_wake.get():
        h["X-No-Wake"] = "1"
    return h


def read_timeout(current: float | None) -> float:
    """At least `READ_TIMEOUT_S` (None = no timeout stays None)."""
    return current if current is None else max(current, READ_TIMEOUT_S)
