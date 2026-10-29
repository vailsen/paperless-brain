"""Recurring work as rows in `schedules` (architecture §3: own tick loop, no
APScheduler).

`interval_spec` is `every:<seconds>` or `daily:HH:MM` (local time, `TZ`). A due
schedule enqueues one job of the schedule's name and moves `next_run_at` to the
next occurrence after now — missed runs are not caught up one by one; a worker
that was down at 07:00 runs the daily job once when it comes back.

A schedule's job carries `dedupe_key = schedule:<name>`: if the previous run is
still queued or running, the tick does not stack a second one.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from proactive import clock, jobs
from proactive.db import connect

log = logging.getLogger("proactive.scheduler")


def next_occurrence(spec: str, after: datetime) -> datetime:
    """The first time strictly after `after` that `spec` fires (UTC-aware)."""
    kind, _, arg = spec.partition(":")
    if kind == "every":
        seconds = int(arg)
        if seconds < 1:
            raise ValueError(f"bad interval {spec!r}")
        return after + timedelta(seconds=seconds)
    if kind == "daily":
        from config.settings import local_tz

        hh, mm = (int(x) for x in arg.split(":"))
        local = after.astimezone(local_tz())
        candidate = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if candidate <= local:
            # Date arithmetic on the local date, then re-attach the zone, so a
            # DST change does not move the run by an hour.
            d = (candidate + timedelta(days=1)).date()
            candidate = datetime(d.year, d.month, d.day, hh, mm, tzinfo=local_tz())
        return candidate
    raise ValueError(f"unknown interval spec {spec!r}")


def ensure(name: str, spec: str, *, run_now: bool = False) -> None:
    """Create or update a schedule. An unchanged spec keeps its `next_run_at`
    (a restart does not reset the daily run); a changed one is rescheduled."""
    now = clock.utcnow()
    first = now if run_now else next_occurrence(spec, now)
    with connect() as conn:
        row = conn.execute(
            "SELECT interval_spec FROM schedules WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO schedules (name, next_run_at, interval_spec) VALUES (?,?,?)",
                (name, clock.iso(first), spec),
            )
        elif row["interval_spec"] != spec:
            conn.execute(
                "UPDATE schedules SET interval_spec = ?, next_run_at = ? WHERE name = ?",
                (spec, clock.iso(first), name),
            )


def remove(name: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM schedules WHERE name = ?", (name,))


def tick() -> list[str]:
    """Enqueue every due schedule's job. Returns the names that fired."""
    now = clock.utcnow()
    fired: list[str] = []
    with connect() as conn:
        rows = conn.execute(
            "SELECT name, interval_spec FROM schedules WHERE next_run_at <= ?",
            (clock.iso(now),),
        ).fetchall()
        for r in rows:
            try:
                nxt = next_occurrence(r["interval_spec"], now)
            except ValueError as exc:
                log.error("schedule %s: %s — removed", r["name"], exc)
                conn.execute("DELETE FROM schedules WHERE name = ?", (r["name"],))
                continue
            conn.execute(
                "UPDATE schedules SET next_run_at = ? WHERE name = ?",
                (clock.iso(nxt), r["name"]),
            )
            fired.append(r["name"])
    for name in fired:
        if jobs.registered(name) is None:
            log.error("schedule %s has no job handler", name)
            continue
        jobs.enqueue(name, dedupe_key=f"schedule:{name}")
    return fired
