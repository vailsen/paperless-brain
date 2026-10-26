"""Job queue on `proactive.db` and the handler registry (architecture §3).

A job is a row: `kind`, `user`, payload, `needs_llm`, status
`queued → running → done | failed`. A handler that raises is retried with
exponential backoff and ends `failed` with `last_error` after
`PROACTIVE_JOB_MAX_ATTEMPTS`. A handler that cannot run *yet* (inference host
asleep, user's grant missing) raises `Defer`: the job waits without spending
an attempt, because nothing about it was wrong.

`dedupe_key` keeps one queued-or-running job per key (a document is not
analysed twice because two pollers saw it).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Awaitable, Callable

from proactive import clock
from proactive.db import connect

log = logging.getLogger("proactive.jobs")

#: Backoff never waits longer than this, however many attempts.
MAX_BACKOFF = timedelta(hours=6)


class Defer(Exception):
    """Not now, not wrong: re-queue after `seconds` without spending an attempt."""

    def __init__(self, reason: str, seconds: float = 300) -> None:
        super().__init__(reason)
        self.seconds = seconds


@dataclass
class Job:
    id: int
    kind: str
    user: str
    payload: dict[str, Any]
    needs_llm: bool
    status: str
    attempts: int
    run_after: str
    last_error: str | None = None
    dedupe_key: str | None = None
    model_key: str = ""
    lane: str = ""

    @classmethod
    def from_row(cls, r: sqlite3.Row) -> "Job":
        return cls(
            id=r["id"],
            kind=r["kind"],
            user=r["user"],
            payload=json.loads(r["payload_json"] or "{}"),
            needs_llm=bool(r["needs_llm"]),
            status=r["status"],
            attempts=r["attempts"],
            run_after=r["run_after"],
            last_error=r["last_error"],
            dedupe_key=r["dedupe_key"],
            model_key=r["model_key"],
            lane=r["lane"],
        )


Handler = Callable[[Job], Awaitable[None]]


@dataclass
class _Registered:
    fn: Handler
    needs_llm: bool


_HANDLERS: dict[str, _Registered] = {}


def handler(kind: str, *, needs_llm: bool = False) -> Callable[[Handler], Handler]:
    """Register the coroutine that runs jobs of `kind`."""

    def deco(fn: Handler) -> Handler:
        _HANDLERS[kind] = _Registered(fn, needs_llm)
        return fn

    return deco


def registered(kind: str) -> _Registered | None:
    return _HANDLERS.get(kind)


# ── Queue operations ─────────────────────────────────────────────────────────


def enqueue(
    kind: str,
    user: str = "",
    payload: dict | None = None,
    *,
    needs_llm: bool | None = None,
    delay: timedelta = timedelta(0),
    dedupe_key: str | None = None,
    model=None,
) -> int | None:
    """Queue a job. Returns its id, or None if `dedupe_key` is already queued
    or running. `needs_llm` defaults to the handler's registration. `model`
    (a `proactive.models.ResolvedModel`) sets the grouping key and lane of an
    LLM job; the handler resolves again when it runs."""
    if needs_llm is None:
        reg = _HANDLERS.get(kind)
        needs_llm = bool(reg and reg.needs_llm)
    model_key = model.key if (needs_llm and model is not None) else ""
    lane = (model.lane if model is not None else "local") if needs_llm else ""
    now = clock.utcnow()
    with connect() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO jobs (kind, user, payload_json, needs_llm, status,"
                " run_after, created_at, dedupe_key, model_key, lane)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    kind,
                    user,
                    json.dumps(payload or {}, ensure_ascii=False),
                    int(needs_llm),
                    "queued",
                    clock.iso(now + delay),
                    clock.iso(now),
                    dedupe_key,
                    model_key,
                    lane,
                ),
            )
        except sqlite3.IntegrityError:
            return None
        return cur.lastrowid


def claim_next(
    *,
    llm_ok: bool = True,
    cloud_ok: bool | None = None,
    prefer_model: str = "",
    kinds: set[str] | None = None,
) -> Job | None:
    """Mark the next due job `running` and return it.

    `llm_ok=False` leaves local-lane LLM jobs queued (the inference host is not
    up); `cloud_ok` does the same for cloud-lane ones (defaults to `llm_ok`).
    Among due jobs, those on `prefer_model` go first: the runner keeps the
    loaded model busy before switching, so a window costs one swap per model
    instead of one per job."""
    if cloud_ok is None:
        cloud_ok = llm_ok
    sql = "SELECT * FROM jobs WHERE status = 'queued' AND run_after <= ?"
    args: list[Any] = [clock.now_iso()]
    lanes = [lane for lane, ok in (("local", llm_ok), ("cloud", cloud_ok)) if ok]
    sql += " AND (needs_llm = 0" + (
        f" OR lane IN ({','.join('?' * len(lanes))}))" if lanes else ")"
    )
    args.extend(lanes)
    if kinds is not None:
        if not kinds:
            return None
        sql += f" AND kind IN ({','.join('?' * len(kinds))})"
        args.extend(sorted(kinds))
    # Jobs without LLM first (they are cheap and some are time-critical, like
    # reminders), then the loaded model's jobs, then the rest by age.
    sql += " ORDER BY needs_llm, (model_key != ?), run_after, id LIMIT 1"
    args.append(prefer_model)
    with connect() as conn:
        row = conn.execute(sql, args).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (row["id"],))
        job = Job.from_row(row)
        job.status = "running"
        return job


def finish(job: Job) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'done', finished_at = ?, last_error = NULL"
            " WHERE id = ?",
            (clock.now_iso(), job.id),
        )


def backoff(attempts: int, base_seconds: int) -> timedelta:
    """`base · 2^(attempts-1)`, capped: 30 s, 60 s, 120 s, … ≤ 6 h."""
    return min(timedelta(seconds=base_seconds * 2 ** max(attempts - 1, 0)), MAX_BACKOFF)


def fail(job: Job, error: str, *, max_attempts: int, base_seconds: int) -> str:
    """Count an attempt. Re-queue with backoff, or end `failed`. Returns the new
    status."""
    attempts = job.attempts + 1
    error = error[:2000]
    with connect() as conn:
        if attempts >= max_attempts:
            conn.execute(
                "UPDATE jobs SET status = 'failed', attempts = ?, last_error = ?,"
                " finished_at = ? WHERE id = ?",
                (attempts, error, clock.now_iso(), job.id),
            )
            return "failed"
        conn.execute(
            "UPDATE jobs SET status = 'queued', attempts = ?, last_error = ?,"
            " run_after = ? WHERE id = ?",
            (
                attempts,
                error,
                clock.iso(clock.utcnow() + backoff(attempts, base_seconds)),
                job.id,
            ),
        )
        return "queued"


def defer(job: Job, reason: str, seconds: float) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE jobs SET status = 'queued', last_error = ?, run_after = ?"
            " WHERE id = ?",
            (
                f"deferred: {reason}"[:2000],
                clock.iso(clock.utcnow() + timedelta(seconds=seconds)),
                job.id,
            ),
        )


def requeue_running() -> int:
    """After a crash or kill -9: jobs left `running` never finished. Queue them
    again (handlers are idempotent; that is a rule for every handler)."""
    with connect() as conn:
        return conn.execute(
            "UPDATE jobs SET status = 'queued' WHERE status = 'running'"
        ).rowcount


def prune(older_than: timedelta = timedelta(days=14)) -> int:
    """Drop finished jobs; failed ones stay for diagnosis until pruned too."""
    cutoff = clock.iso(clock.utcnow() - older_than)
    with connect() as conn:
        return conn.execute(
            "DELETE FROM jobs WHERE status IN ('done', 'failed') AND finished_at < ?",
            (cutoff,),
        ).rowcount


def get(job_id: int) -> Job | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return Job.from_row(row) if row else None


def pending_count(
    *, needs_llm: bool | None = None, kind: str | None = None, due_only: bool = False,
    lane: str | None = None,
) -> int:
    sql = "SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')"
    args: list[Any] = []
    if needs_llm is not None:
        sql += " AND needs_llm = ?"
        args.append(int(needs_llm))
    if kind is not None:
        sql += " AND kind = ?"
        args.append(kind)
    if lane is not None:
        sql += " AND lane = ?"
        args.append(lane)
    if due_only:
        sql += " AND status = 'queued' AND run_after <= ?"
        args.append(clock.now_iso())
    with connect() as conn:
        return conn.execute(sql, args).fetchone()[0]


# ── Running one job ──────────────────────────────────────────────────────────


@dataclass
class RunResult:
    job: Job
    status: str
    error: str = ""
    extra: dict = field(default_factory=dict)


async def run_one(job: Job, *, max_attempts: int, base_seconds: int) -> RunResult:
    reg = _HANDLERS.get(job.kind)
    if reg is None:
        status = fail(job, f"no handler for kind {job.kind!r}", max_attempts=1, base_seconds=base_seconds)
        log.error("job %s: no handler for kind %r", job.id, job.kind)
        return RunResult(job, status, "no handler")
    try:
        await reg.fn(job)
    except Defer as d:
        defer(job, str(d), d.seconds)
        log.info("job %s (%s) deferred %ss: %s", job.id, job.kind, int(d.seconds), d)
        return RunResult(job, "deferred", str(d))
    except Exception as exc:  # the job failed, the worker does not
        error = f"{type(exc).__name__}: {exc}"
        status = fail(job, error, max_attempts=max_attempts, base_seconds=base_seconds)
        log.warning(
            "job %s (%s) attempt %s failed → %s: %s",
            job.id, job.kind, job.attempts + 1, status, error,
        )
        return RunResult(job, status, error)
    finish(job)
    return RunResult(job, "done")
