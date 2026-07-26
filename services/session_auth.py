# services/session_auth.py
"""Session token handling: encrypted at rest in NiceGUI user storage.

NiceGUI persists ``app.storage.user`` as plaintext JSON on disk
(.nicegui/storage-user-*.json); STORAGE_SECRET only signs the browser cookie.
To keep the Paperless API token out of those files in cleartext, it is sealed
with AES-256-GCM under a key derived (HKDF-SHA256) from STORAGE_SECRET before
being stored, and opened on read.

Threat model honesty: STORAGE_SECRET lives in .env on the same host, so this
does not defend against an attacker with full filesystem access. It does
defend against partial leaks (a copied/backed-up .nicegui directory alone is
useless) and against casual reads.

All access to the session token goes through get_session_token() /
set_session_token() — never read app.storage.user["paperless_token"] directly.
Legacy plaintext tokens are migrated to the sealed form on first read.
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from nicegui import app as ng_app

from config.settings import settings

_TOKEN_KEY_INFO = b"papersage:session-token:v1"
_STORE_KEY = "paperless_token_enc"
_LEGACY_KEY = "paperless_token"

_key_cache: bytes | None = None


def _key() -> bytes:
    global _key_cache
    if _key_cache is None:
        _key_cache = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=_TOKEN_KEY_INFO
        ).derive(settings.storage_secret.encode())
    return _key_cache


def seal(plaintext: str) -> str:
    """Encrypt a string for at-rest storage. Returns urlsafe base64."""
    nonce = os.urandom(12)
    ct = AESGCM(_key()).encrypt(nonce, plaintext.encode("utf-8"), _TOKEN_KEY_INFO)
    return base64.urlsafe_b64encode(nonce + ct).decode("ascii")


def open_sealed(sealed: str) -> str:
    """Decrypt a seal()ed string. Raises ValueError on tampering/key change."""
    try:
        blob = base64.urlsafe_b64decode(sealed.encode("ascii"))
        nonce, ct = blob[:12], blob[12:]
        return AESGCM(_key()).decrypt(nonce, ct, _TOKEN_KEY_INFO).decode("utf-8")
    except (InvalidTag, ValueError, TypeError) as exc:
        raise ValueError("sealed token invalid or STORAGE_SECRET changed") from exc


# ── Session accessors (require browser/session context) ──────────────────────


def set_session_token(token: str) -> None:
    """Store the Paperless token sealed; drop any legacy plaintext copy."""
    ng_app.storage.user[_STORE_KEY] = seal(token)
    ng_app.storage.user.pop(_LEGACY_KEY, None)


def get_session_token() -> str:
    """Return the session's Paperless token, or "" if not logged in.

    A sealed token that no longer decrypts (rotated STORAGE_SECRET) counts as
    logged out — the user simply logs in again. Legacy plaintext tokens are
    sealed in place on first read.
    """
    store = ng_app.storage.user
    sealed = store.get(_STORE_KEY, "")
    if sealed:
        try:
            return open_sealed(sealed)
        except ValueError:
            store.pop(_STORE_KEY, None)
            return ""
    legacy = store.get(_LEGACY_KEY, "")
    if legacy:
        set_session_token(legacy)
        return legacy
    return ""


def clear_session() -> None:
    """Log out: remove token and user identity from the session storage."""
    for key in (_STORE_KEY, _LEGACY_KEY, "paperless_user"):
        ng_app.storage.user.pop(key, None)
