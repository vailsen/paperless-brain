"""Worker side of the internal API. Every call is best effort for the caller:
`InternalUnavailable` means "the app is not reachable right now" (restart,
update) — a job defers, it does not fail."""

from __future__ import annotations

import json

import httpx

from proactive.internal_auth import sign


class InternalUnavailable(Exception):
    pass


def _base() -> str:
    from config.proactive import proactive_settings
    from config.settings import settings

    return (proactive_settings().proactive_internal_url or f"http://127.0.0.1:{settings.port}").rstrip("/")


async def call(path: str, payload: dict, *, timeout: float = 60) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json", **sign(body)}
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(f"{_base()}/api/internal/proactive/{path}", content=body, headers=headers)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError) as exc:
        raise InternalUnavailable(type(exc).__name__) from exc
    if r.status_code in (502, 503, 504):
        raise InternalUnavailable(str(r.status_code))
    r.raise_for_status()
    return r.json()


async def similar(user: str, doc_id: int, k: int = 5) -> list[dict]:
    return (await call("similar", {"user": user, "doc_id": int(doc_id), "k": k}, timeout=60)).get("docs", [])


async def deep_dive(user: str, finding_id: str, model: dict) -> str:
    """Start the research run in the app; returns its Werkbank run id at once
    (only the brief's setup happens in the request)."""
    out = await call("deep_dive", {"user": user, "finding_id": finding_id, "model": model}, timeout=120)
    return str(out["run_id"])


async def deep_dive_result(user: str, finding_id: str, run_id: str, model: dict) -> dict:
    """The run's state; once it is done, the condensed result (one model call)."""
    return await call("deep_dive_result", {
        "user": user, "finding_id": finding_id, "run_id": run_id, "model": model,
    }, timeout=900)


async def vault_sync(user: str) -> dict:
    """Have the app commit and index the user's vault (`vault.sync.sync_user`)."""
    return await call("vault_sync", {"user": user}, timeout=900)


async def dossier(user: str, topic: str, seed_doc_id: int, existing: dict | None, model: dict) -> dict:
    return await call("dossier", {
        "user": user, "topic": topic, "seed_doc_id": int(seed_doc_id or 0),
        "existing": existing, "model": model,
    }, timeout=1800)
