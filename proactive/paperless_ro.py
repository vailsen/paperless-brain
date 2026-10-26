"""Read-only Paperless-ngx access for the worker, always as one user.

The proactive system never writes to Paperless (CLAUDE.md invariant) — this
module only issues GET requests, and a test pins that. Every user-specific call
takes that user's token (`background_access.token_for`); Paperless decides what
they may see. The superuser token appears only in `newest_ids_admin()`, which
feeds the new-document cursor and returns ids, never content.

A 401 means the stored token is no longer valid: `TokenRejected` lets the
caller mark the grant `needs_reauth` instead of retrying forever.
"""

from __future__ import annotations

import httpx

TIMEOUT = 30.0


class TokenRejected(Exception):
    """Paperless answered 401 for this user's token."""


def _base() -> str:
    from config.settings import settings

    return settings.paperless_url.rstrip("/")


async def _get(token: str, path: str, params: dict | None = None) -> httpx.Response:
    async with httpx.AsyncClient(
        headers={"Authorization": f"Token {token}"}, timeout=TIMEOUT
    ) as c:
        r = await c.get(_base() + path, params=params)
    if r.status_code == 401:
        raise TokenRejected(path)
    return r


async def can_read(token: str, doc_id: int) -> bool:
    """The authoritative access check (architecture §6): 404/403 → no."""
    r = await _get(token, f"/api/documents/{int(doc_id)}/", {"fields": "id"})
    if r.status_code in (403, 404):
        return False
    r.raise_for_status()
    return True


async def ids_after(token: str, after_id: int, limit: int = 25) -> list[int]:
    """Document ids > `after_id` this user can read, ascending."""
    r = await _get(
        token,
        "/api/documents/",
        {"id__gt": int(after_id), "ordering": "id", "page_size": int(limit), "fields": "id"},
    )
    r.raise_for_status()
    return [int(d["id"]) for d in r.json().get("results", [])]


async def newest_ids_admin(after_id: int, limit: int = 100) -> list[int]:
    """New-document poller: ids > cursor across the archive (superuser token —
    ids only; anything user-facing re-checks with the user's own token)."""
    from config.settings import settings

    return await ids_after(settings.paperless_superuser_token, after_id, limit)


async def max_id_admin() -> int:
    from config.settings import settings

    r = await _get(
        settings.paperless_superuser_token,
        "/api/documents/",
        {"ordering": "-id", "page_size": 1, "fields": "id"},
    )
    r.raise_for_status()
    res = r.json().get("results", [])
    return int(res[0]["id"]) if res else 0


async def document(token: str, doc_id: int) -> dict | None:
    """Title, dates and names for one document, or None without access."""
    r = await _get(
        token,
        f"/api/documents/{int(doc_id)}/",
        {"fields": "id,title,created,added,correspondent,document_type,tags,content"},
    )
    if r.status_code in (403, 404):
        return None
    r.raise_for_status()
    return r.json()


async def whoami(token: str) -> dict:
    """`/api/ui_settings/` carries the user; used for the guard's name list."""
    r = await _get(token, "/api/ui_settings/")
    r.raise_for_status()
    return r.json().get("user", {}) or {}
