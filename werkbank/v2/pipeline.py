"""Werkbank v2 — one run from request to report.

The orchestrator is deterministic Python. The model does cognitive work inside
the roles and never decides the order, the retries or the outcome:

    BRIEFER → (user confirms) → PLANNER → PLAN_CRITIC → SCHEDULER
        → per subtask: RUNNER → checks → FACT_CRITIC → revise
        → CONTRADICTION_CHECKER → WRITER

Every step is persisted the moment it produces something, so a run survives a
restart and resumes at the first subtask that is not finished. `start_run()` is
therefore safe to call again on an existing run — that is the resume path, not
a second execution.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from werkbank.v2 import briefer, contradictions, planner, registry, scheduler, store, writer
from werkbank.v2.llm import LLMContext, PromptLog
from werkbank.v2.models import Brief, RunState, SubtaskStatus

_log = logging.getLogger(__name__)

# Kept in memory only, exactly like v1's token registry: the Paperless session
# token is what user-scoped tools authenticate with and must never be written
# to disk.
_tokens: dict[str, str] = {}

# Running executions, so deleting a run can stop it. Without this the task keeps
# going after the row is gone, and every write it makes has to be swallowed by
# the store — work nobody will ever see, on a model nobody is waiting for.
_running: dict[str, "asyncio.Task"] = {}


def track(run_id: str, task: "asyncio.Task") -> None:
    _running[run_id] = task
    task.add_done_callback(lambda _t, rid=run_id: _running.pop(rid, None))


def cancel_run(run_id: str) -> bool:
    """Stop a run that is executing. True if something was actually cancelled."""
    task = _running.pop(run_id, None)
    if task is None or task.done():
        return False
    task.cancel()
    return True


def register_token(user_id: str, token: str) -> None:
    if user_id and token:
        _tokens[user_id] = token


def token_for(user_id: str) -> str:
    return _tokens.get(user_id, "")


def context_for(user_id: str, model: str, run_id: str = "") -> LLMContext:
    return LLMContext(model=model, user_id=user_id, token=token_for(user_id), run_id=run_id)


# A run's life before it starts working:
#   BRIEFING  — the briefer is formulating the assignment
#   DRAFT     — the brief is ready and waiting for the user to confirm it
#   PLANNED   — confirmed, ready to execute (or interrupted and resumable)
STATUS_BRIEFING = "briefing"
STATUS_DRAFT = "draft"


async def make_brief(request: str, user_id: str, model: str) -> Brief:
    """Step 1. Nothing is persisted yet — the user has not confirmed anything."""
    return await briefer.build_brief(request, context_for(user_id, model))


def create_draft(request: str, user_id: str, model: str) -> str:
    """Put the run in the list *before* the briefer runs.

    Formulating an assignment takes the better part of a minute. While it ran,
    the request existed only inside one open dialog: navigating away threw it
    away, and the user had to sit and wait for a screen that was not doing
    anything for them. The row exists from the moment they press the button, so
    they can leave and come back to a brief that is waiting.
    """
    run_id = uuid.uuid4().hex[:12]
    store.create_run(
        run_id, user_id,
        Brief(original_request=request, goal=""),
        model=model, status=STATUS_BRIEFING,
    )
    return run_id


async def brief_draft(run_id: str, user_id: str, model: str) -> Brief | None:
    """Formulate the assignment for a run that is already in the list."""
    state = store.load_state(run_id, user_id)
    if state is None or state.brief is None:
        return None
    request = state.brief.original_request
    try:
        brief = await briefer.build_brief(request, context_for(user_id, model, run_id))
    except Exception:
        # The request is not lost: it is on the row, and the user can retry.
        store.set_run_status(run_id, user_id, STATUS_BRIEFING + "_failed")
        raise
    store.save_brief(run_id, user_id, brief)
    store.set_run_status(run_id, user_id, STATUS_DRAFT)
    return brief


def confirm_draft(run_id: str, user_id: str, brief: Brief) -> None:
    """The user accepted (possibly edited) the brief."""
    store.save_brief(run_id, user_id, brief)
    store.set_run_status(run_id, user_id, "planned")


def create_run(brief: Brief, user_id: str, model: str) -> str:
    """Persist a confirmed brief and return the run id."""
    run_id = uuid.uuid4().hex[:12]
    store.create_run(run_id, user_id, brief, model=model)
    return run_id


async def start_run(
    run_id: str,
    user_id: str,
    model: str,
    *,
    progress: Callable[[str, SubtaskStatus], None] | None = None,
) -> RunState:
    """Plan (if needed), execute, check contradictions, write the report.

    Resumable: an existing plan is reused and finished subtasks are skipped.
    """
    state = store.load_state(run_id, user_id)
    if state is None or state.brief is None:
        raise ValueError(f"run {run_id} not found for this user")

    # Agents reach for vault_search and brain_search, and the note editor writes
    # files without indexing them (the next sync does that). Without this the run
    # reads whatever the last chat turn happened to index — a note written minutes
    # ago would be invisible to the run started to act on it. Same contract as the
    # chat turn: force=True, once per run, never fatal.
    try:
        from vault.sync import sync_user

        await sync_user(user_id, force=True)
    except Exception as exc:                                # noqa: BLE001
        _log.warning("werkbank v2 %s: vault sync failed: %s", run_id, exc)

    state.model = model
    ctx = context_for(user_id, model, run_id)
    prompt_log = PromptLog()
    caps = registry.user_capabilities(user_id, token_for(user_id))
    reg = registry.available_agents(caps, user_agents=registry.load_user_agents(user_id))

    if not state.subtasks:
        subtasks, report, coverage = await planner.plan_with_review(
            state.brief, reg, ctx, prompt_log=prompt_log
        )
        state.subtasks = subtasks
        state.plan_coverage = coverage
        if not report.ok:
            # The plan runs anyway; its weaknesses are recorded and surface in
            # the report's reflection rather than blocking the run.
            _log.warning("werkbank v2 %s: plan has defects: %s", run_id, report.defects)
        store.save_plan(run_id, user_id, subtasks)
        store.save_state(run_id, user_id, state)

    state = await scheduler.run_plan(
        state, reg, ctx, prompt_log=prompt_log, progress=progress
    )

    await contradictions.find(state, ctx, prompt_log=prompt_log)
    for result in state.results.values():
        store.save_result(run_id, user_id, result)

    report_md = await writer.write_report(state, ctx, prompt_log=prompt_log)
    state.finished_at = state.finished_at or datetime.now(timezone.utc).isoformat()
    store.save_state(run_id, user_id, state)
    store.save_report(run_id, user_id, report_md)
    return state


async def run_from_request(
    request: str, user_id: str, model: str
) -> tuple[str, RunState]:
    """Brief → run, without the confirmation dialog.

    For callers that already have a formulated request (the chat hand-off).
    The confirmation step is a UI affordance, not a safety mechanism — the
    brief is still visible in the finished report either way.
    """
    brief = await make_brief(request, user_id, model)
    run_id = create_run(brief, user_id, model)
    return run_id, await start_run(run_id, user_id, model)
