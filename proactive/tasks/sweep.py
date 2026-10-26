"""Backlog sweep: existing documents, inside sweep windows, within the budget.

`sweep_round` (scheduled every minute, and re-queued after each document)
hands out one document per eligible user per round — round-robin, so one user
with 3,000 documents cannot starve the others — and only while the previous
round is finished. `sweep_doc` checks the window again before it starts: a job
left over when the window closed is dropped without advancing the cursor, so
that document is the first of the next window.

The cursor is per `(user, PROACTIVE_ANALYSIS_VERSION)` over the documents that
user can read (`id__gt` with their own token). A finished cursor means done
until the version is bumped. What happens to each document is the processor's
business (`set_processor`, Phase 6 onwards); it returns whether the document
used the LLM, and only those count against the budget.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

from proactive import clock, jobs, users, windows
from proactive.db import connect
from proactive.jobs import Job, handler
from proactive.models import ResolvedModel, resolve_model
from proactive.paperless_ro import TokenRejected, ids_after

log = logging.getLogger("proactive.sweep")

SWEEP_ROUND = "sweep_round"
SWEEP_DOC = "sweep_doc"

#: (user, token, doc_id, model) → True if the LLM was used for this document.
Processor = Callable[[str, str, int, ResolvedModel], Awaitable[bool]]
_processor: Processor | None = None

#: Rotates which user goes first in a round, so a budget smaller than the
#: number of users does not always favour the same ones.
_rr_offset = 0


def set_processor(fn: Processor | None) -> None:
    global _processor
    _processor = fn


def _version() -> int:
    from config.proactive import proactive_settings

    return proactive_settings().proactive_analysis_version


def cursor(user: str) -> tuple[int, bool]:
    """(last processed doc id, finished) for the current analysis version."""
    with connect() as conn:
        r = conn.execute(
            "SELECT last_doc_id, finished_at FROM sweep_cursor WHERE user = ? AND analysis_version = ?",
            (user, _version()),
        ).fetchone()
    return (r["last_doc_id"], bool(r["finished_at"])) if r else (0, False)


def _set_cursor(user: str, last_doc_id: int | None = None, *, finished: bool = False) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO sweep_cursor (user, analysis_version, last_doc_id, finished_at)"
            " VALUES (?,?,?,?) ON CONFLICT(user, analysis_version) DO UPDATE SET"
            " last_doc_id = COALESCE(?, last_doc_id), finished_at = excluded.finished_at",
            (
                user,
                _version(),
                last_doc_id or 0,
                clock.now_iso() if finished else None,
                last_doc_id,
            ),
        )


@handler(SWEEP_ROUND)
async def sweep_round(job: Job) -> None:
    global _rr_offset
    if _processor is None:
        return
    win = windows.current()
    if win is None:
        return
    if jobs.pending_count(kind=SWEEP_DOC) > 0:
        return  # the previous round is still running
    remaining = win.window.max_docs - windows.processed(win.start_iso)
    if remaining <= 0:
        windows.close(win.start_iso, "budget")
        return
    eligible = users.eligible()
    if not eligible:
        return
    k = _rr_offset % len(eligible)
    order = eligible[k:] + eligible[:k]
    _rr_offset += 1
    queued = 0
    for user, tok in order:
        if queued >= remaining:
            break
        last, finished = cursor(user)
        if finished:
            continue
        try:
            nxt = await ids_after(tok, last, limit=1)
        except TokenRejected:
            users.token_rejected(user)
            continue
        if not nxt:
            _set_cursor(user, finished=True)
            log.info("%s: backlog sweep finished for analysis version %s", user, _version())
            continue
        model = resolve_model(user, tok)
        if model is None:
            continue  # no allowed model: this user's LLM work waits
        jobs.enqueue(
            SWEEP_DOC,
            user,
            {"doc_id": nxt[0], "window_start": win.start_iso},
            needs_llm=True,
            model=model,
            dedupe_key=f"sweep:{user}:{nxt[0]}",
        )
        queued += 1
    if queued == 0 and all(cursor(u)[1] for u, _ in eligible):
        windows.close(win.start_iso, "no_backlog")


@handler(SWEEP_DOC, needs_llm=True)
async def sweep_doc(job: Job) -> None:
    p = job.payload
    win = windows.current()
    if win is None or win.start_iso != p["window_start"]:
        windows.close(p["window_start"], "window_end")
        log.info("%s: doc %s dropped — its window has ended", job.user, p["doc_id"])
        return
    if windows.processed(win.start_iso) >= win.window.max_docs:
        windows.close(win.start_iso, "budget")
        return
    if _processor is None:
        return
    tok = users.token(job.user)
    if not tok:
        return  # grant withdrawn since the round
    model = resolve_model(job.user, tok)
    if model is None:
        return
    try:
        used_llm = await _processor(job.user, tok, int(p["doc_id"]), model)
    except TokenRejected:
        users.token_rejected(job.user)
        return
    _set_cursor(job.user, int(p["doc_id"]))
    if used_llm:
        windows.add_processed(win.start_iso, model.key)
    jobs.enqueue(SWEEP_ROUND, dedupe_key=f"schedule:{SWEEP_ROUND}")
