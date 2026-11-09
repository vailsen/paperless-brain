"""Links that open Paperless-ngx itself in the user's browser.

The API address (`PAPERLESS_URL`) is what this server reaches — often an
internal IP a phone outside the LAN cannot. `PAPERLESS_PUBLIC_URL` is the
address the browser reaches; without it the API address is the best guess.
"""
from __future__ import annotations

from config.settings import settings


def public_base() -> str:
    return (settings.paperless_public_url or settings.paperless_url or "").strip().rstrip("/")


def document_url(doc_id: int | str) -> str:
    base = public_base()
    return f"{base}/documents/{int(doc_id)}/details" if base else ""
