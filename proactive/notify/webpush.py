"""Web Push: notifications on the user's own devices (installed app, browser).

One row in `push_subscription` per device the user turned notifications on
for, created from their signed-in session (`/api/push/subscribe`), so no link
code is needed. The payload is encrypted end-to-end (RFC 8291); the browser
vendor's push service (Google, Apple, Mozilla) only relays it.

A notification has no buttons — iOS shows none — and tapping it opens its
`link` in the app, where the reactions are. A device the push service reports
gone (404/410: permission withdrawn, app removed, site data cleared) is
deleted on the spot.

Keys: `python -m proactive.notify.webpush` prints a fresh VAPID pair for
`.env` (`PUSH_VAPID_PUBLIC_KEY`, `PUSH_VAPID_PRIVATE_KEY`).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging

from proactive import clock
from proactive.db import connect

log = logging.getLogger("proactive.webpush")

#: Endpoints are URLs of the browser vendors' push services. Anything else is
#: refused: the worker would otherwise POST to whatever URL a client named.
ALLOWED_HOST_SUFFIXES = (
    ".googleapis.com",          # Chrome, Edge (fcm.googleapis.com)
    ".push.apple.com",          # Safari, iOS
    ".push.services.mozilla.com",
    ".notify.windows.com",      # legacy Edge
)


class SubscriptionRefused(ValueError):
    pass


def _host_ok(endpoint: str) -> bool:
    from urllib.parse import urlparse

    u = urlparse(endpoint)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(
        host == s.lstrip(".") or host.endswith(s) for s in ALLOWED_HOST_SUFFIXES
    )


def subscribe(user: str, sub: dict, device: str = "") -> None:
    """Store a browser `PushSubscription.toJSON()` for `user`. An endpoint
    belongs to one device, so re-subscribing moves it to the current user."""
    endpoint = str(sub.get("endpoint") or "")
    keys = sub.get("keys") or {}
    p256dh, auth = str(keys.get("p256dh") or ""), str(keys.get("auth") or "")
    if not _host_ok(endpoint) or not p256dh or not auth or len(endpoint) > 1000:
        raise SubscriptionRefused("not a push subscription")
    with connect() as conn:
        conn.execute(
            "INSERT INTO push_subscription (endpoint, user, p256dh, auth, device, created_at)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET user = excluded.user,"
            " p256dh = excluded.p256dh, auth = excluded.auth, device = excluded.device,"
            " created_at = excluded.created_at",
            (endpoint, user, p256dh, auth, device[:80], clock.now_iso()),
        )


def unsubscribe(user: str, endpoint: str) -> None:
    with connect() as conn:
        conn.execute(
            "DELETE FROM push_subscription WHERE user = ? AND endpoint = ?", (user, endpoint)
        )


def devices(user: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT endpoint, device, created_at, last_status, last_sent FROM push_subscription"
            " WHERE user = ?"
            " ORDER BY created_at",
            (user,),
        ).fetchall()
    return [dict(r) for r in rows]


def _subscriptions(user: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT endpoint, p256dh, auth FROM push_subscription WHERE user = ?", (user,)
        ).fetchall()
    return [dict(r) for r in rows]


def _record(endpoint: str, status: int) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE push_subscription SET last_status = ?, last_sent = ? WHERE endpoint = ?",
            (int(status), clock.now_iso(), endpoint),
        )


def payload(title: str, body: str, link: str, tag: str = "") -> dict:
    return {"title": title, "body": body, "url": link, "tag": tag}


def _contact() -> str:
    from config.proactive import proactive_settings

    cfg = proactive_settings()
    c = cfg.push_contact or cfg.proactive_app_base_url
    if c and not c.startswith(("mailto:", "https://")):
        c = f"mailto:{c}" if "@" in c else ""
    return c or "mailto:admin@localhost"


def _send_one(sub: dict, data: dict, urgency: str = "normal") -> int:
    """POST one encrypted message. Returns the HTTP status (201 = accepted).

    `urgency` is the Web Push `Urgency` header (RFC 8030). `high` is what makes
    Android deliver at once — a phone in Doze, or one just back from airplane
    mode, otherwise holds `normal` messages for its next maintenance window."""
    from config.proactive import proactive_settings
    from pywebpush import WebPushException, webpush

    try:
        r = webpush(
            subscription_info={
                "endpoint": sub["endpoint"],
                "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
            },
            data=json.dumps(data),
            vapid_private_key=proactive_settings().push_vapid_private_key,
            vapid_claims={"sub": _contact()},
            ttl=24 * 3600,
            timeout=15,
            headers={"Urgency": urgency},
        )
        return r.status_code
    except WebPushException as exc:
        return exc.response.status_code if exc.response is not None else 0


async def send(user: str, data: dict, *, urgency: str = "normal") -> int:
    """Deliver to every device of `user`. Returns how many accepted it. Raises
    (→ job retry) only when devices exist and none accepted."""
    subs = _subscriptions(user)
    ok = 0
    for sub in subs:
        status = await asyncio.to_thread(_send_one, sub, data, urgency)
        _record(sub["endpoint"], status)
        if status in (200, 201, 202):
            ok += 1
        elif status in (404, 410):
            log.info("%s: push device gone (%s) — removed", user, status)
            unsubscribe(user, sub["endpoint"])
        else:
            log.warning("%s: push delivery failed with status %s", user, status)
    if subs and not ok and _subscriptions(user):
        raise RuntimeError("no device accepted the push message")
    return ok


def generate_keys() -> tuple[str, str]:
    """(public, private), base64url: the browser takes the uncompressed public
    point as `applicationServerKey`; pywebpush takes the raw private scalar."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    k = ec.generate_private_key(ec.SECP256R1())
    pub = k.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    priv = k.private_numbers().private_value.to_bytes(32, "big")

    def b64(x: bytes) -> str:
        return base64.urlsafe_b64encode(x).rstrip(b"=").decode()

    return b64(pub), b64(priv)


if __name__ == "__main__":
    pub, priv = generate_keys()
    print(f"PUSH_VAPID_PUBLIC_KEY={pub}\nPUSH_VAPID_PRIVATE_KEY={priv}")
