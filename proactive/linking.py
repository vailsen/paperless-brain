"""Channel links and link codes (architecture §7.1, §7.2).

A `channel_link` row is the allowlist: notifications go only to an address the
user linked, and a Telegram callback or an email reply counts only if it comes
from the owner's linked address.

Link codes: 8 characters from an unambiguous alphabet, valid for 10 minutes,
usable once, stored as a SHA-256 hash (a leaked DB holds no usable code).
Telegram: the app shows a code, the user sends `/start <code>` to the bot, the
worker redeems it with the chat id. Email: the user enters an address, the
worker mails a code to it, the user types it back in the app.
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
CHANNELS = ("telegram", "email")


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
    again moves it (a family sharing one Telegram account re-links on purpose)."""
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


def unlink_address(channel: str, address: str) -> str | None:
    """`/stop` from a chat: remove whatever link it has. Returns the user."""
    address = norm(channel, address)
    with connect() as conn:
        r = conn.execute(
            "SELECT user FROM channel_link WHERE channel = ? AND address = ?", (channel, address)
        ).fetchone()
        if r is None:
            return None
        conn.execute("DELETE FROM channel_link WHERE channel = ? AND address = ?", (channel, address))
        return r["user"]


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
