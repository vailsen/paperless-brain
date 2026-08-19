"""DEPRECATED — v1 execution path, retired 2026-08-18.

Nothing imports this any more: `/werkbank` is `werkbank/v2/ui/page.py`, the chat
hand-off goes to `werkbank.v2.pipeline`, and `main.py` no longer starts the v1
scheduler. The files stay on disk for exactly one release because
`docs/werkbank-tasks.md` (Phase 7) conditions removal on v2 passing a complete
run on real data, and that run has not happened yet — the first real run *is*
the test. If it fails, re-adding two imports in `main.py` brings v1 back.

Delete this module once v2 has completed a run against the live archive.

werkbank/scheduler.py — background polling loop for queued tasks.

Started once at app startup via asyncio.create_task(run_scheduler()).

Token handling
--------------
Paperless session tokens are NEVER stored in SQLite. When the UI starts a task
(TRIAGE → QUEUED), it calls register_token(task_id, token) to store the token
in the in-memory _task_tokens dict. The scheduler pops it exactly once when the
task starts executing. After that the token is gone from memory.

Consequence: if the server restarts between enqueue and execution, the token is
lost → user-specific credentials (IMAP, CalDAV, per-user Claude key) fall back
to global settings. Document search and web tools still work.

Concurrency
-----------
v1: Tasks run concurrently across backends (local vs API lanes), but sub-tasks
within a task are serial. The lanes (Semaphore in llm_lane) prevent GPU overload.
"""

from __future__ import annotations

import asyncio
import logging

from werkbank import orchestrator, repository
from werkbank.models import TaskStatus

_log = logging.getLogger(__name__)

_POLL_INTERVAL = 2.0  # seconds between next_queued_task() polls

# In-memory token registry: task_id → paperless session token
# Populated by UI before setting task to QUEUED.
_task_tokens: dict[int, str] = {}

# Currently running task IDs — prevents double-starting
_running: set[int] = set()


def register_token(task_id: int, token: str) -> None:
    """Register a user's session token before enqueuing.

    Called by the UI page at the moment the user confirms TRIAGE and the task
    transitions to QUEUED. Token lives in memory only — never written to disk.
    """
    _task_tokens[task_id] = token


def _on_task_done(fut: asyncio.Future, task_id: int) -> None:
    _running.discard(task_id)
    try:
        exc = fut.exception()
    except asyncio.CancelledError:
        return
    if exc:
        _log.error("Task %d failed with exception: %s", task_id, exc)
        try:
            repository.mark_task_failed(task_id)
        except Exception as db_exc:
            _log.error("Task %d could not be marked FAILED: %s", task_id, db_exc)


async def run_scheduler() -> None:
    """Long-lived coroutine. Call once at app startup.

    Polls for QUEUED tasks and starts an orchestrator coroutine for each.
    Cross-task concurrency is limited only by the backend lanes in llm_lane.
    """
    _log.info("[werkbank/scheduler] started")

    # On startup: reset any stale RUNNING tasks back to QUEUED (crash recovery)
    _reset_stale_running_tasks()

    while True:
        try:
            task = repository.next_queued_task()
            if task and task.id not in _running:
                token = _task_tokens.pop(task.id, "")
                _running.add(task.id)
                repository.update_task_status(task.id, task.user_id, TaskStatus.RUNNING)

                coro = orchestrator.run_task(
                    task.id, task.user_id, task.model, token=token
                )
                fut = asyncio.ensure_future(coro)
                fut.add_done_callback(
                    lambda f, tid=task.id: _on_task_done(f, tid)
                )
        except Exception as exc:
            _log.error("[werkbank/scheduler] poll error: %s", exc)

        await asyncio.sleep(_POLL_INTERVAL)


def _reset_stale_running_tasks() -> None:
    """On startup: move any RUNNING tasks back to QUEUED so they resume."""
    import sqlite3
    from werkbank.repository import _DB_PATH, _now

    try:
        with sqlite3.connect(_DB_PATH) as conn:
            conn.execute(
                "UPDATE agent_tasks SET status='QUEUED', updated_at=? WHERE status='RUNNING'",
                (_now(),),
            )
            conn.commit()
    except Exception as exc:
        _log.warning("[werkbank/scheduler] could not reset stale tasks: %s", exc)
