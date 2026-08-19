"""Werkbank v2 — the scheduler.

Level-wise walk over the DAG: everything whose dependencies are done runs
together, against a semaphore. The limiting resource is the model, not the
task — a local Ollama instance is VRAM-bound and does not get faster by being
asked four things at once, while an API backend does.

Two policies that shape the whole thing:

- **A failed subtask does not stop the run.** It ends `unresolvable`, the run
  continues, and the gap is named in the report. Aborting would throw away the
  subtasks that did work, and a report that is honest about one missing branch
  is worth more than no report.
- **Resume never redoes finished work.** Every result is persisted the moment
  it exists, and a subtask in a terminal state is skipped on restart. Each one
  costs model calls and real retrieval.

A dependent subtask inherits **only accepted facts** from its predecessors —
never their narrative, never raw tool output. That is what keeps provenance
intact and context bounded as the DAG deepens.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone

from werkbank.v2 import critic, store
from werkbank.v2.llm import LLMContext, PromptLog
from werkbank.v2.models import (
    CriticDecision,
    CriticVerdict,
    Fact,
    RunState,
    Subtask,
    SubtaskResult,
    SubtaskStatus,
)
from werkbank.v2.plan_checks import topological_order
from werkbank.v2.registry import Registry

_log = logging.getLogger(__name__)

# Local: VRAM-bound, two in flight is already optimistic. Cloud: latency-bound.
CONCURRENCY = {"local": 2, "cloud": 6}
TERMINAL = {SubtaskStatus.OK, SubtaskStatus.PARTIAL, SubtaskStatus.UNRESOLVABLE}


def concurrency_for(model: str, user_id: str, token: str) -> int:
    """How many subtasks may run at once, decided by the backend."""
    try:
        from werkbank.llm_lane import _LOCAL_SEM, _sem_for

        lane = "local" if _sem_for(model, user_id, token) is _LOCAL_SEM else "cloud"
    except Exception:
        lane = "local"
    return CONCURRENCY[lane]


def accepted_facts(state: RunState, subtask_ids: list[str]) -> list[Fact]:
    """The facts a dependent subtask inherits. Facts only, by construction."""
    facts: list[Fact] = []
    for sid in subtask_ids:
        result = state.results.get(sid)
        if result and result.status is not SubtaskStatus.UNRESOLVABLE:
            facts.extend(result.facts)
    return facts


def _placeholder(subtask: Subtask, reason: str) -> SubtaskResult:
    """What a crashed subtask leaves behind: a named hole, not silence."""
    from werkbank.v2.models import Gap, GapReason

    return SubtaskResult(
        subtask_id=subtask.subtask_id,
        status=SubtaskStatus.UNRESOLVABLE,
        question=subtask.question,
        agent=subtask.agent,
        acceptance_criteria=subtask.acceptance_criteria,
        covers_criteria=subtask.covers_criteria,
        depends_on=subtask.depends_on,
        gaps=[Gap(question=subtask.question, reason=GapReason.NOT_FOUND,
                  suggested_source=reason[:200])],
    )


async def run_plan(
    state: RunState,
    registry: Registry,
    ctx: LLMContext,
    *,
    prompt_log: PromptLog | None = None,
    persist: bool = True,
    progress: Callable[[str, SubtaskStatus], None] | None = None,
) -> RunState:
    """Execute the whole plan. Returns the updated state.

    Safe to call again on a state that is partly done: finished subtasks are
    skipped, which is what makes an interrupted run resumable rather than
    restartable.
    """
    if not state.subtasks:
        return state
    if not state.started_at:
        state.started_at = datetime.now(timezone.utc).isoformat()

    budget = state.brief.budget if state.brief else None
    max_revisions = budget.max_revisions if budget else 1
    original_request = state.brief.original_request if state.brief else ""
    by_id = {s.subtask_id: s for s in state.subtasks}
    semaphore = asyncio.Semaphore(concurrency_for(ctx.model, ctx.user_id, ctx.token))

    async def run_one(subtask: Subtask) -> None:
        async with semaphore:
            spec = registry.agents.get(subtask.agent)
            if spec is None:
                # The agent vanished between planning and execution — a
                # credential removed mid-run. That is a gap, not a crash.
                state.results[subtask.subtask_id] = _placeholder(
                    subtask, f"agent '{subtask.agent}' is not available"
                )
                return

            # Announce the start, not only the finish. A subtask can take
            # minutes; without this the board says "waiting" for all of them and
            # then jumps to done, which reads as a hang.
            state.statuses[subtask.subtask_id] = SubtaskStatus.RUNNING
            if persist and ctx.run_id:
                store.set_status(
                    ctx.run_id, ctx.user_id, subtask.subtask_id, SubtaskStatus.RUNNING
                )
            if progress:
                progress(subtask.subtask_id, SubtaskStatus.RUNNING)

            inherited = accepted_facts(state, subtask.depends_on)
            known = {f.id for f in state.all_facts()}
            try:
                result, verdict, capped = await critic.run_with_review(
                    subtask, spec, registry, ctx,
                    original_request=original_request,
                    max_revisions=max_revisions,
                    inherited_facts=inherited,
                    known_fact_ids=known,
                    prompt_log=prompt_log,
                    persist=persist,
                )
            except Exception as exc:                       # never take the run down
                _log.warning("werkbank v2 %s failed: %s", subtask.subtask_id, exc)
                result = _placeholder(subtask, str(exc))
                verdict = CriticVerdict(
                    decision=CriticDecision.UNRESOLVABLE, defects=[str(exc)]
                )
                capped = False

            state.results[subtask.subtask_id] = result
            state.statuses.pop(subtask.subtask_id, None)
            state.verdicts[subtask.subtask_id] = verdict
            if capped and subtask.subtask_id not in state.capped_subtasks:
                state.capped_subtasks.append(subtask.subtask_id)

            if persist and ctx.run_id:
                store.save_result(ctx.run_id, ctx.user_id, result)
                store.save_verdict(
                    ctx.run_id, ctx.user_id, subtask.subtask_id, result.revision, verdict
                )
            if progress:
                progress(subtask.subtask_id, result.status)

    for level in topological_order(state.subtasks):
        pending = [
            by_id[sid]
            for sid in level
            if sid in by_id
            and (sid not in state.results or state.results[sid].status not in TERMINAL)
        ]
        if not pending:
            continue
        _log.info(
            "werkbank v2: level with %d subtask(s): %s",
            len(pending), ", ".join(s.subtask_id for s in pending),
        )
        await asyncio.gather(*(run_one(s) for s in pending))

    state.finished_at = datetime.now(timezone.utc).isoformat()
    if persist and ctx.run_id:
        store.save_state(ctx.run_id, ctx.user_id, state)
    return state
