"""Sweep windows, their doc budget, and `window_log` (architecture §3.1).

Backlog work (the per-user sweep over existing documents, topic revisits) only
runs inside a configured window and stops at the window's end *or* when its
doc budget is used up, whichever comes first. New-document work and deadline
reminders are not budgeted and run any time.

One `window_log` row per (window occurrence, model): the number of rows of a
window is the number of models it ran, so model swaps are countable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from config.proactive import SweepWindow
from proactive import clock
from proactive.db import connect


#: Backlog work: runs only inside a sweep window, counted against its budget.
#: Outside a window these jobs are not even looked at — in particular they
#: never make the worker wake the inference host (they used to: the host was
#: woken, the job saw no window and deferred itself, every half hour).
WINDOWED_KINDS = frozenset({"sweep_doc", "dossier_build", "dossier_revisit"})

#: Work the user just asked for ("Analyse now", "Interesting" → deep dive).
#: The only reason to wake the host outside a window — and only while the
#: request is fresh (`USER_WAKE_FRESH`), so a job that keeps deferring does
#: not wake the machine again and again through the evening.
USER_REQUESTED_KINDS = frozenset({"sweep_now", "deep_dive"})
USER_WAKE_FRESH = timedelta(minutes=30)


@dataclass(frozen=True)
class OpenWindow:
    window: SweepWindow
    start: datetime  # this occurrence's start, local time
    end: datetime

    @property
    def start_iso(self) -> str:
        return clock.iso(self.start)


def current(now_local: datetime | None = None, windows: list[SweepWindow] | None = None) -> OpenWindow | None:
    """The window `now` falls into (first match), with this occurrence's bounds."""
    if windows is None:
        from config.proactive import proactive_settings

        windows = proactive_settings().sweep_windows
    now = now_local or clock.local_now()
    t = now.timetz().replace(tzinfo=None)
    for w in windows:
        if not w.contains(t):
            continue
        start = now.replace(hour=w.start.hour, minute=w.start.minute, second=0, microsecond=0)
        if start > now:  # crossed midnight: started yesterday
            start -= timedelta(days=1)
        end = start.replace(hour=w.end.hour, minute=w.end.minute)
        if end <= start:
            end += timedelta(days=1)
        return OpenWindow(w, start, end)
    return None


def processed(window_start: str) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COALESCE(SUM(docs_processed), 0) FROM window_log WHERE window_start = ?",
            (window_start,),
        ).fetchone()[0]


def _row(conn, window_start: str, model: str):
    return conn.execute(
        "SELECT id FROM window_log WHERE window_start = ? AND model = ?",
        (window_start, model),
    ).fetchone()


def note_model(window_start: str, model: str, *, woke_host: bool = False) -> bool:
    """Record that `model` ran in this window. Returns True for a new row (a swap)."""
    with connect() as conn:
        if _row(conn, window_start, model):
            if woke_host:
                conn.execute(
                    "UPDATE window_log SET woke_host = 1 WHERE window_start = ? AND model = ?",
                    (window_start, model),
                )
            return False
        conn.execute(
            "INSERT INTO window_log (window_start, model, woke_host) VALUES (?,?,?)",
            (window_start, model, int(woke_host)),
        )
        return True


def add_processed(window_start: str, model: str, n: int = 1) -> None:
    note_model(window_start, model)
    with connect() as conn:
        conn.execute(
            "UPDATE window_log SET docs_processed = docs_processed + ?"
            " WHERE window_start = ? AND model = ?",
            (n, window_start, model),
        )


def close(window_start: str, reason: str) -> None:
    """`budget` | `window_end` | `no_backlog` — on every row still open."""
    with connect() as conn:
        conn.execute(
            "UPDATE window_log SET stop_reason = ? WHERE window_start = ? AND stop_reason = ''",
            (reason, window_start),
        )
        if not conn.execute(
            "SELECT 1 FROM window_log WHERE window_start = ?", (window_start,)
        ).fetchone():
            conn.execute(
                "INSERT INTO window_log (window_start, stop_reason) VALUES (?, ?)",
                (window_start, reason),
            )


def log_rows(window_start: str) -> list[dict]:
    with connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM window_log WHERE window_start = ? ORDER BY id", (window_start,)
            )
        ]


# ── Cloud cap (per user, per local day) ──────────────────────────────────────


def _today() -> str:
    return clock.local_now().date().isoformat()


def cloud_used_today(user: str) -> int:
    with connect() as conn:
        r = conn.execute(
            "SELECT docs FROM cloud_usage WHERE user = ? AND day = ?", (user, _today())
        ).fetchone()
    return r["docs"] if r else 0


def add_cloud_use(user: str, n: int = 1) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO cloud_usage (user, day, docs) VALUES (?,?,?)"
            " ON CONFLICT(user, day) DO UPDATE SET docs = docs + excluded.docs",
            (user, _today(), n),
        )


def seconds_until_tomorrow() -> float:
    now = clock.local_now()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
    return max((tomorrow - now).total_seconds(), 60.0)
