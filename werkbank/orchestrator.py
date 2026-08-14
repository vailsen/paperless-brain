"""werkbank/orchestrator.py — deterministic state machine for ONE task.

Drives the Ready-Set-Walk over the sub-task DAG:
  for each ready sub-task → Worker → prechecks → (retry) → Critic → (redo) → compact → DONE

Failed-policy: after retry cap, mark FAILED + placeholder, continue with remaining tasks.
Pause: checked after each sub-task; PAUSED status set, coroutine exits (resumable by scheduler).
"""

from __future__ import annotations

import asyncio

from werkbank import compaction, prechecks, repository
from werkbank.models import SubTask, SubTaskStatus, Task, TaskStatus
from werkbank.roles import critic, synthesizer, worker

# Total attempts per sub-task (1 initial + retries/redos combined)
_SUBTASK_ATTEMPTS = 3

# Module-level pause flags: task_id → True = "pause after current sub-task"
_pause_flags: dict[int, bool] = {}


# ── Public control API (called by UI) ─────────────────────────────────────────

def pause_task(task_id: int) -> None:
    """Signal the running orchestrator to pause after the current sub-task."""
    _pause_flags[task_id] = True


def resume_task(task_id: int, user_id: str) -> None:
    """Clear the pause flag and re-queue the task so the scheduler picks it up."""
    _pause_flags.pop(task_id, None)
    repository.update_task_status(task_id, user_id, TaskStatus.QUEUED)


# ── Sub-task execution ────────────────────────────────────────────────────────

async def _run_subtask(
    subtask: SubTask,
    task: Task,
    all_subtasks: list[SubTask],
    *,
    model: str,
    user_id: str,
    token: str,
) -> None:
    """Run one sub-task through the full Worker → Critic → Compact pipeline.

    Updates subtask status in SQLite after every significant step.
    Applies failed-policy on exhausted retries: FAILED + placeholder, no exception.
    """
    from werkbank.archetypes import resolve_archetype
    from werkbank.llm_lane import create_llm
    from services.chat_service import TOOL_DEFINITIONS

    tool_map = {t["name"]: t for t in TOOL_DEFINITIONS}

    # Resolve archetype → soul_text + tool subset
    if subtask.archetype_id is not None:
        resolved = resolve_archetype(subtask.archetype_id, user_id)
    else:
        resolved = None

    if resolved:
        soul_text, tool_names = resolved
        arch_name = (repository.get_archetype(subtask.archetype_id, user_id) or object()).name  # type: ignore[union-attr]
    else:
        soul_text = "You are a helpful assistant."
        tool_names = []
        arch_name = ""

    from i18n import language_directive
    soul_text = f"{soul_text}\n\n{language_directive(task.language)}"

    tool_subset = [tool_map[n] for n in tool_names if n in tool_map]

    # Build dependency context from compacted results of terminal predecessors.
    # FAILED deps are passed through as their [FEHLER] placeholder — a silently
    # missing dependency would invite the worker to invent the gap.
    terminal_map = {
        s.id: s
        for s in all_subtasks
        if s.status in (SubTaskStatus.DONE, SubTaskStatus.FAILED)
    }
    dep_results = []
    for dep_id in subtask.depends_on:
        dep = terminal_map.get(dep_id)
        if dep is None:
            continue
        if dep.status == SubTaskStatus.DONE:
            dep_results.append(dep.result_compacted or dep.result_raw or "")
        else:
            dep_results.append(
                dep.result_compacted
                or "[ERROR] Previous sub-task failed — no data."
            )

    llm = create_llm(model, user_id, token)
    extra_context: list[str] = []
    raw_result = ""
    failed_reason = ""

    for attempt in range(_SUBTASK_ATTEMPTS):
        repository.set_subtask_running(subtask.id)

        # ── Worker ────────────────────────────────────────────────────
        try:
            raw = await worker.run(
                subtask.instruction,
                soul_text,
                tool_subset,
                dep_results + extra_context,
                model=model,
                user_id=user_id,
                token=token,
            )
        except Exception as exc:
            failed_reason = f"Worker error: {exc}"
            repository.increment_subtask_retry(subtask.id)
            extra_context = [f"[Previous attempt failed: {exc}]"]
            continue

        # ── Prechecks ─────────────────────────────────────────────────
        try:
            prechecks.run(raw, arch_name)
        except prechecks.PrecheckError as exc:
            failed_reason = f"Precheck: {exc}"
            repository.increment_subtask_retry(subtask.id)
            extra_context = [f"[Previous attempt failed – {exc}. Please improve.]"]
            raw_result = raw  # save partial for placeholder
            continue

        # ── Critic ────────────────────────────────────────────────────
        ok, feedback = await critic.run(
            subtask.instruction,
            subtask.success_criteria,
            raw,
            model=model,
            user_id=user_id,
            token=token,
        )

        if not ok:
            failed_reason = f"Critic: {feedback}"
            repository.increment_subtask_retry(subtask.id)
            # Keep the rejected draft: weak models repeat the same mistake
            # when they only see a one-line feedback note.
            draft = raw if len(raw) <= 6_000 else raw[:6_000] + "\n[… truncated …]"
            extra_context = [
                f"[Your previous draft – revise it, do not start over]:\n{draft}",
                f"[Critic feedback – revision required]: {feedback}",
            ]
            raw_result = raw
            continue

        # ── Compact + DONE ────────────────────────────────────────────
        compacted = await compaction.run(
            raw, task.refined_request or task.original_request, llm=llm
        )
        repository.set_subtask_result(
            subtask.id,
            status=SubTaskStatus.DONE,
            result_raw=raw,
            result_compacted=compacted,
            critic_verdict=feedback or "Success criteria met.",
            retry_count=attempt,
        )
        return  # success

    # ── Failed-policy: placeholder, continue ──────────────────────────
    placeholder = f"[ERROR] {failed_reason or 'No reliable data determined.'}"
    repository.set_subtask_result(
        subtask.id,
        status=SubTaskStatus.FAILED,
        result_raw=raw_result or placeholder,
        result_compacted=placeholder,
        critic_verdict=failed_reason,
        retry_count=_SUBTASK_ATTEMPTS,
    )


# ── Splitting helper ─────────────────────────────────────────────────────────

async def _split_task(
    task: repository.Task,
    user_id: str,
    model: str,
    token: str,
) -> None:
    """Run the Splitter and insert the resulting sub-task DAG into the DB."""
    from werkbank.archetypes import list_archetype_summaries, seed_defaults_if_needed
    from werkbank.roles.splitter import _fallback_spec, insert_subtasks
    from werkbank.roles.splitter import run as splitter_run

    seed_defaults_if_needed(user_id)
    available_archetypes = list_archetype_summaries(user_id)
    arch_map = {a.name: a.id for a in repository.get_archetypes(user_id)}

    try:
        specs = await splitter_run(
            task.refined_request or task.original_request,
            available_archetypes,
            model=model,
            user_id=user_id,
            token=token,
        )
    except Exception as exc:
        print(f"[orchestrator] Splitter error: {exc} — falling back to single subtask")
        specs = _fallback_spec(task.refined_request or task.original_request)

    insert_subtasks(task.id, user_id, specs, arch_map)


# ── Task state machine ────────────────────────────────────────────────────────

async def run_task(
    task_id: int,
    user_id: str,
    model: str,
    token: str = "",
) -> None:
    """Drive one task from RUNNING → AWAITING_REVIEW (or PAUSED / FAILED).

    Token is in-memory only (never persisted). Passed by scheduler from the
    _task_tokens registry populated when the user starts the task in the UI.

    v1: sub-tasks within a task execute serially (parallel branches in v2).
    """
    from werkbank.llm_lane import create_llm

    task = repository.get_task(task_id, user_id)
    if not task:
        return

    # ── Vault sync before any retrieval this run ─────────────────────
    # Workers reach for vault_search and brain_search, and the note editor
    # writes files without indexing them (the next sync does that). Without
    # this the run would read whatever the last chat turn happened to index —
    # a note written minutes ago would be invisible to the task started to act
    # on it. Same contract as the chat turn: force=True, exactly once per run,
    # and never fatal — a broken vault must not take the task down with it.
    try:
        from vault.sync import sync_user

        await sync_user(user_id, force=True)
    except Exception as exc:
        print(f"[orchestrator] vault sync failed: {exc}")

    # ── Split: create sub-task DAG on first run (not on resume) ──────
    if not repository.get_subtasks(task_id, user_id):
        await _split_task(task, user_id, model, token)
        task = repository.get_task(task_id, user_id)  # refresh after split

    # Ready-Set-Walk
    while True:
        # Pause check — runs before each sub-task
        if _pause_flags.pop(task_id, False):
            repository.update_task_status(task_id, user_id, TaskStatus.PAUSED)
            return

        subtasks = repository.get_subtasks(task_id, user_id)

        all_terminal = all(
            s.status in (SubTaskStatus.DONE, SubTaskStatus.FAILED) for s in subtasks
        )
        if all_terminal:
            break

        terminal_ids = {
            s.id for s in subtasks
            if s.status in (SubTaskStatus.DONE, SubTaskStatus.FAILED)
        }
        ready = [
            s for s in subtasks
            if s.status == SubTaskStatus.TODO
            and all(dep_id in terminal_ids for dep_id in s.depends_on)
        ]

        if not ready:
            # DAG is stuck — guard against infinite loop (should never happen)
            break

        task = repository.get_task(task_id, user_id)  # refresh refined_request

        # API-lane models: run up to 2 independent ready subtasks concurrently.
        # Local models: always serial (GPU bottleneck).
        from werkbank.llm_lane import _sem_for, _LOCAL_SEM
        _parallel_limit = 1 if _sem_for(model, user_id, token) is _LOCAL_SEM else 2
        batch = ready[:_parallel_limit]

        if len(batch) == 1:
            await _run_subtask(
                batch[0], task, subtasks, model=model, user_id=user_id, token=token
            )
        else:
            await asyncio.gather(*(
                _run_subtask(s, task, subtasks, model=model, user_id=user_id, token=token)
                for s in batch
            ))

    # ── Synthesize ────────────────────────────────────────────────────
    subtasks = repository.get_subtasks(task_id, user_id)
    task = repository.get_task(task_id, user_id)
    subtask_results = [
        {
            "instruction": s.instruction,
            "status": s.status.value,
            "result_compacted": s.result_compacted,
            "result_raw": s.result_raw,
        }
        for s in subtasks
    ]

    llm = create_llm(model, user_id, token)
    try:
        result_md = await synthesizer.run(
            task.refined_request or task.original_request,
            subtask_results,
            llm=llm,
            language=task.language,
        )
    except Exception as exc:
        # Fallback: manual assembly
        lines = [f"# Synthesis error\n\n{exc}\n"]
        for r in subtask_results:
            lines.append(f"## {r['instruction']}\n{r.get('result_compacted', '[no result]')}\n")
        result_md = "\n".join(lines)

    repository.update_task_result(task_id, user_id, result_md)
    repository.update_task_status(task_id, user_id, TaskStatus.AWAITING_REVIEW)
