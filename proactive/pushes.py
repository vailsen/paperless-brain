"""The weekly message limit (architecture §8).

Rolling 7 days per user, counted once per notification (Telegram *and* email =
one), a digest once however many items it holds. Requested deep-dive results
and critical deadline reminders do not count and are never held back. The
user's limit is 0 … `PROACTIVE_MAX_PUSHES_PER_WEEK_LIMIT`, default
`PROACTIVE_MAX_PUSHES_PER_WEEK`; anything over it becomes `held`, never dropped.
"""

from __future__ import annotations

from datetime import timedelta

from proactive import clock
from proactive.db import connect

WEEK = timedelta(days=7)


def limit_for(user: str) -> int:
    from config.proactive import proactive_settings
    from proactive import prefs

    cfg = proactive_settings()
    n = prefs.get(user).max_pushes_per_week
    if n is None:
        n = cfg.proactive_max_pushes_per_week
    return max(0, min(int(n), cfg.proactive_max_pushes_per_week_limit))


def count_week(user: str) -> int:
    since = clock.iso(clock.utcnow() - WEEK)
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM push_log WHERE user = ? AND at > ?", (user, since)
        ).fetchone()[0]


def allowed(user: str) -> bool:
    return count_week(user) < limit_for(user)


def record(user: str, kind: str, finding_id: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO push_log (user, at, kind, finding_id) VALUES (?,?,?,?)",
            (user, clock.now_iso(), kind, finding_id),
        )


def last_push(user: str) -> str | None:
    with connect() as conn:
        r = conn.execute("SELECT MAX(at) FROM push_log WHERE user = ?", (user,)).fetchone()
    return r[0] if r and r[0] else None
