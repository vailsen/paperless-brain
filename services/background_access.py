"""Background access: a user's Paperless token, kept so a background process can
act as that user — and only as that user.

Why this exists: the proactive worker runs every user-specific job with that
user's own Paperless token (Paperless decides what the user may read; the
superuser token is never used for user-facing work). But the token normally
exists only in the browser session (sealed, `services/session_auth.py`) and in
RAM — by design nothing persists it, and the user's model registry and mail
credentials are encrypted with it. A separate process therefore cannot act for
a user at all, unless the user explicitly grants it. That grant is this module
(decision "option A", `docs/proactive-diagnostics.md` D1).

Rules:

- **Only an explicit opt-in creates a grant** (`grant()`, from the user's own
  session). Nothing here ever creates one on its own.
- **`revoke()` deletes it**; the worker then has nothing to act with.
- **Login refreshes an existing grant** (`refresh_on_login`) with the token the
  user just logged in with. Paperless returns the same token until it is
  regenerated; after a regeneration the old grant would fail silently, and the
  user's choice to allow background access has not changed.
- **A rejected token is not retried forever**: the worker calls
  `mark_needs_reauth()` on a 401, and `status()` reports it so the settings page
  can ask the user to log in again (which refreshes the grant).

At rest the token is sealed with AES-256-GCM under a key derived from
`STORAGE_SECRET` with its own HKDF label (separate from the session-token key).
Same threat model as `session_auth`: it protects copies and partial leaks of
`data/`, not an attacker who also has `.env`. A rotated `STORAGE_SECRET` makes
every grant unreadable; `token_for()` then answers None and the user re-grants.
"""

from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_INFO = b"pbrain:background-access:v1"

Status = Literal["none", "active", "needs_reauth"]


def _dir() -> Path:
    from config.settings import settings

    return Path(settings.app_path) / "data" / "background_access"


def _path(username: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._@-]", "_", username) or "_"
    return _dir() / f"{safe}.enc"


def _key() -> bytes:
    from config.settings import settings

    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO).derive(
        settings.storage_secret.encode()
    )


def _seal(data: dict, username: str) -> bytes:
    nonce = os.urandom(12)
    # The username is bound into the ciphertext: a grant file copied onto
    # another user's name does not decrypt.
    aad = _INFO + b"|" + username.encode()
    ct = AESGCM(_key()).encrypt(nonce, json.dumps(data).encode(), aad)
    return base64.urlsafe_b64encode(nonce + ct)


def _open(blob: bytes, username: str) -> dict | None:
    try:
        raw = base64.urlsafe_b64decode(blob)
        aad = _INFO + b"|" + username.encode()
        return json.loads(AESGCM(_key()).decrypt(raw[:12], raw[12:], aad))
    except (InvalidTag, ValueError, TypeError):
        return None


def _read(username: str) -> dict | None:
    try:
        blob = _path(username).read_bytes()
    except OSError:
        return None
    return _open(blob, username)


def _write(username: str, data: dict) -> None:
    path = _path(username)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(_seal(data, username))
    os.replace(tmp, path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Public API ────────────────────────────────────────────────────────────────


def grant(username: str, token: str) -> None:
    """Allow background jobs to act as `username`. Call from their own session."""
    if not username or not token:
        raise ValueError("username and token are required")
    _write(username, {"token": token, "granted_at": _now(), "needs_reauth_since": None})


def revoke(username: str) -> None:
    """Withdraw the grant. Idempotent."""
    try:
        _path(username).unlink()
    except FileNotFoundError:
        pass


def token_for(username: str) -> str | None:
    """The token to act as `username`, or None (no grant, unreadable, or the
    token was rejected and the user has not logged in since)."""
    data = _read(username)
    if not data or data.get("needs_reauth_since"):
        return None
    return data.get("token") or None


def status(username: str) -> Status:
    data = _read(username)
    if not data:
        return "none"
    return "needs_reauth" if data.get("needs_reauth_since") else "active"


def mark_needs_reauth(username: str) -> None:
    """Paperless rejected the stored token (401). Stop using it until the user
    logs in again."""
    data = _read(username)
    if data and not data.get("needs_reauth_since"):
        data["needs_reauth_since"] = _now()
        _write(username, data)


def refresh_on_login(username: str, token: str) -> None:
    """Keep an existing grant working after a login. Never creates a grant."""
    if not username or not token:
        return
    data = _read(username)
    if data is None:
        return
    if data.get("token") != token or data.get("needs_reauth_since"):
        data.update(token=token, needs_reauth_since=None)
        _write(username, data)
