"""The message log: when was a user last told something (architecture §8).

Only the quiet-period digest reads it (`last_push`). There is no weekly limit
any more (removed 2026-10-27): a busy first week while the archive is swept
was judged better than findings held back. Deep-dive results and critical
reminders are not logged (`Notification.counts`).
"""

from __future__ import annotations

from proactive import clock
from proactive.db import connect

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
