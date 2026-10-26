"""Signing for the app's internal proactive API.

Worker and app share `.env`, so both derive the same key from `STORAGE_SECRET`
(own HKDF label). A request carries a timestamp and an HMAC over
`timestamp.body`; the app rejects a bad signature and anything older than
`MAX_SKEW` seconds. Requests name a user, never carry a token — the app looks
the token up in that user's background-access grant itself.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_INFO = b"pbrain:proactive-internal:v1"
MAX_SKEW = 120
TS_HEADER = "X-PB-Timestamp"
SIG_HEADER = "X-PB-Signature"


def _key() -> bytes:
    from config.settings import settings

    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO).derive(
        settings.storage_secret.encode()
    )


def sign(body: bytes, ts: int | None = None) -> dict[str, str]:
    ts = int(time.time()) if ts is None else ts
    mac = hmac.new(_key(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return {TS_HEADER: str(ts), SIG_HEADER: mac}


def verify(body: bytes, ts: str | None, sig: str | None, now: float | None = None) -> bool:
    if not ts or not sig or not ts.isdigit():
        return False
    now = time.time() if now is None else now
    if abs(now - int(ts)) > MAX_SKEW:
        return False
    expected = hmac.new(_key(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)
