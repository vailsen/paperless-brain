"""Channel links and link codes (architecture §7.1, §7.2).

A `channel_link` row is the allowlist: notifications go only to an address the
user linked, and an email reply counts only if it comes from the owner's
linked address. (Push devices live in `push_subscription`, registered from the
user's own session — no code needed.)

Link codes: 8 characters from an unambiguous alphabet, valid for 10 minutes,
usable once, stored as a SHA-256 hash (a leaked DB holds no usable code). The
user enters an address, the worker mails a code to it, the user types it back
in the app.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from proactive import clock
from proactive.db import connect

CODE_TTL = timedelta(minutes=10)
CODE_LEN = 8
# No 0/O, 1/I/L: a code is read off a screen and typed on a phone.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CHANNELS = ("email",)
# "Send code" mails whatever address was typed in, so without a limit a signed-in
# user could flood a stranger's inbox. Per user: a burst of 3 in the first hour
# (a typo, a slow mail), then 1 an hour; 24 hours after the first code it resets.
EMAIL_CODE_WINDOW = timedelta(hours=24)
EMAIL_CODE_BURST = 3
EMAIL_CODE_INTERVAL = timedelta(hours=1)


def norm(channel: str, address: str) -> str:
    """Email addresses compare case-insensitively; chat ids are strings as given."""
    address = (address or "").strip()
    return address.lower() if channel == "email" else address


def _hash(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode()).hexdigest()


def create_code(user: str, channel: str, address: str = "") -> str:
    """A new code for `user`; earlier unused codes of the same channel die."""
    if channel not in CHANNELS:
        raise ValueError(channel)
    code = "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LEN))
    now = clock.utcnow()
    with connect() as conn:
        conn.execute(
            "UPDATE link_code SET used_at = ? WHERE user = ? AND channel = ? AND used_at IS NULL",
            (clock.iso(now), user, channel),
        )
        conn.execute(
            "INSERT INTO link_code (code_hash, user, channel, address, expires_at) VALUES (?,?,?,?,?)",
            (_hash(code), user, channel, norm(channel, address), clock.iso(now + CODE_TTL)),
        )
    return code


def email_code_wait(user: str) -> timedelta | None:
    """Time left before `user` may have another email code sent; None = now.
    A code's creation time is its `expires_at` minus `CODE_TTL`."""
    now = clock.utcnow()
    since = clock.iso(now - 2 * EMAIL_CODE_WINDOW + CODE_TTL)
    with connect() as conn:
        rows = conn.execute(
            "SELECT expires_at FROM link_code WHERE user = ? AND channel = 'email'"
            " AND expires_at > ? ORDER BY expires_at",
            (user, since),
        ).fetchall()
    # Walk the codes into 24-hour windows, each opened by the first code after
    # the previous one ran out.
    start, sent = None, []
    for r in rows:
        t = clock.parse(r["expires_at"]) - CODE_TTL
        if start is None or t >= start + EMAIL_CODE_WINDOW:
            start, sent = t, []
        sent.append(t)
    if start is None or now >= start + EMAIL_CODE_WINDOW:
        return None
    if now < start + EMAIL_CODE_INTERVAL and len(sent) < EMAIL_CODE_BURST:
        return None
    left = sent[-1] + EMAIL_CODE_INTERVAL - now
    return left if left > timedelta(0) else None


def redeem(code: str, channel: str, *, user: str | None = None) -> tuple[str, str] | None:
    """`(user, address)` for a valid code, which is used up; None otherwise.
    With `user`, the code must also belong to that user (email: typed in the
    user's own settings)."""
    now = clock.now_iso()
    with connect() as conn:
        r = conn.execute(
            "SELECT * FROM link_code WHERE code_hash = ? AND channel = ?",
            (_hash(code), channel),
        ).fetchone()
        if r is None or r["used_at"] is not None or r["expires_at"] <= now:
            return None
        if user is not None and r["user"] != user:
            return None
        conn.execute("UPDATE link_code SET used_at = ? WHERE code_hash = ?", (now, r["code_hash"]))
        return r["user"], r["address"]


def pending_address(user: str, channel: str = "email") -> str:
    """The address of the user's newest unexpired, unused code ('' if none)."""
    with connect() as conn:
        r = conn.execute(
            "SELECT address FROM link_code WHERE user = ? AND channel = ? AND used_at IS NULL"
            " AND expires_at > ? ORDER BY expires_at DESC LIMIT 1",
            (user, channel, clock.now_iso()),
        ).fetchone()
    return r["address"] if r else ""


def link(user: str, channel: str, address: str) -> None:
    """Link `address` to `user`. An address belongs to one user: linking it
    again moves it (a family sharing one mailbox re-links on purpose)."""
    address = norm(channel, address)
    with connect() as conn:
        conn.execute(
            "DELETE FROM channel_link WHERE channel = ? AND address = ? AND user != ?",
            (channel, address, user),
        )
        conn.execute(
            "INSERT INTO channel_link (user, channel, address, created_at) VALUES (?,?,?,?)"
            " ON CONFLICT(user, channel) DO UPDATE SET address = excluded.address,"
            " created_at = excluded.created_at",
            (user, channel, address, clock.now_iso()),
        )


def unlink(user: str, channel: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM channel_link WHERE user = ? AND channel = ?", (user, channel))


def links(user: str) -> dict[str, dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM channel_link WHERE user = ?", (user,)).fetchall()
    return {
        r["channel"]: {"address": r["address"], "full_text": bool(r["full_text_optin"])}
        for r in rows
    }


def address(user: str, channel: str) -> str | None:
    return links(user).get(channel, {}).get("address")


def user_for(channel: str, address: str) -> str | None:
    address = norm(channel, address)
    with connect() as conn:
        r = conn.execute(
            "SELECT user FROM channel_link WHERE channel = ? AND address = ?", (channel, address)
        ).fetchone()
    return r["user"] if r else None


def set_full_text(user: str, channel: str, on: bool) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE channel_link SET full_text_optin = ? WHERE user = ? AND channel = ?",
            (int(on), user, channel),
        )
