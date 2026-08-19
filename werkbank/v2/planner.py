"""Werkbank v2 — PLANNER and PLAN_CRITIC.

The planner produces the whole DAG in one call. Code then checks everything
mechanically decidable (`plan_checks`), and only what needs judgement goes to
the critic: which acceptance criterion the assigned agents cannot actually
deliver.

One replanning round, then the plan runs as it stands. Without a cap there is
ping-pong; and a weakness that survives is recorded in the run state so it
appears in the report's reflection rather than being quietly dropped.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import BaseModel, Field

from werkbank.v2.llm import LLMContext, PromptLog, call_structured
from werkbank.v2.models import Brief, CoverageCheck, CoverageVerdict, Subtask
from werkbank.v2.plan_checks import (
    PlanReport,
    ensure_contradiction_checker,
    validate_plan,
)
from werkbank.v2.registry import Registry
from werkbank.v2 import prompts

_log = logging.getLogger(__name__)

PLANNER_PROMPT = Path(__file__).parent / "prompts" / "planner.md"
CRITIC_PROMPT = Path(__file__).parent / "prompts" / "plan_critic.md"


class Plan(BaseModel):
    subtasks: list[Subtask] = Field(default_factory=list)


class Coverage(BaseModel):
    criteria: list[CoverageCheck] = Field(default_factory=list)


def _brief_block(brief: Brief) -> str:
    numbered = "\n".join(
        f"  [{i}] {c}" for i, c in enumerate(brief.acceptance_criteria)
    )
    return (
        f"Original request (verbatim):\n{brief.original_request}\n\n"
        f"Goal: {brief.goal}\n"
        f"Deliverable: {brief.deliverable_format}\n"
        f"Out of scope: {brief.out_of_scope or '—'}\n"
        f"Assumptions: {brief.assumptions or '—'}\n"
        f"Acceptance criteria (index in brackets):\n{numbered}\n"
        f"Depth: {brief.depth_budget.value} "
        f"(max. {brief.budget.max_subtasks} subtasks)"
    )


def _registry_block(registry: Registry) -> str:
    return json.dumps(registry.planner_view(), ensure_ascii=False, indent=2)


async def build_plan(
    brief: Brief,
    registry: Registry,
    ctx: LLMContext,
    *,
    prompt_log: PromptLog | None = None,
) -> tuple[list[Subtask], PlanReport]:
    """Brief → validated DAG. One replanning round on mechanical defects.

    Returns the plan and the last report; a plan that still has defects is
    returned anyway with `ok=False`, so the caller can show the reason instead
    of failing silently.
    """
    system = prompts.system_prompt("planner")
    user = (
        f"{_brief_block(brief)}\n\n"
        f"Available agents (filtered for this user — nothing else exists):\n"
        f"{_registry_block(registry)}"
    )
    known = set(registry.agents)
    report = PlanReport()
    subtasks: list[Subtask] = []

    for attempt in range(2):
        plan = await call_structured(
            "planner", system, user, Plan, ctx, prompt_log=prompt_log
        )
        subtasks = ensure_contradiction_checker(plan.subtasks)
        report = validate_plan(subtasks, brief, known)
        if report.ok or attempt == 1:
            break
        user = (
            f"{_brief_block(brief)}\n\n"
            f"Available agents:\n{_registry_block(registry)}\n\n"
            "Your previous plan had these defects:\n"
            + "\n".join(f"- {d}" for d in report.defects)
            + "\n\nPlan again, fixing exactly these points."
        )
        _log.info("werkbank v2 planner: replanning after %d defects", len(report.defects))

    return subtasks, report


async def review_coverage(
    brief: Brief,
    subtasks: list[Subtask],
    registry: Registry,
    ctx: LLMContext,
    *,
    prompt_log: PromptLog | None = None,
) -> list[CoverageCheck]:
    """PLAN_CRITIC. Skipped by the caller when the budget says so."""
    system = prompts.system_prompt("plan_critic")
    plan_block = json.dumps(
        [
            {
                "subtask_id": s.subtask_id,
                "question": s.question,
                "agent": s.agent,
                "agent_tools": registry.agents[s.agent].tools if s.agent in registry.agents else [],
                "covers_criteria": s.covers_criteria,
                "depends_on": s.depends_on,
            }
            for s in subtasks
        ],
        ensure_ascii=False,
        indent=2,
    )
    user = f"{_brief_block(brief)}\n\nThe plan:\n{plan_block}"
    coverage = await call_structured(
        "plan_critic", system, user, Coverage, ctx, prompt_log=prompt_log
    )

    # A verdict without evidence is an opinion: a criterion the critic calls
    # covered while naming no subtask is downgraded rather than believed.
    checked: list[CoverageCheck] = []
    for entry in coverage.criteria:
        if entry.verdict is CoverageVerdict.COVERED and not entry.subtask_ids:
            entry.verdict = CoverageVerdict.PARTIAL
        checked.append(entry)
    return checked


def uncovered_criteria(coverage: list[CoverageCheck]) -> list[int]:
    return [c.criterion_index for c in coverage if c.verdict is CoverageVerdict.UNCOVERED]


async def plan_with_review(
    brief: Brief,
    registry: Registry,
    ctx: LLMContext,
    *,
    prompt_log: PromptLog | None = None,
) -> tuple[list[Subtask], PlanReport, list[CoverageCheck]]:
    """The whole planning step: plan → code checks → critic → one replan.

    After the second attempt the plan runs as it is. The remaining weakness
    travels in `coverage` into the run state, and from there into the report's
    reflection — an honest plan with a named hole beats a fourth round of
    replanning that produces a different hole.
    """
    subtasks, report = await build_plan(brief, registry, ctx, prompt_log=prompt_log)
    if not brief.budget.run_plan_critic:
        _log.info("werkbank v2: plan critic skipped for depth '%s'", brief.depth_budget.value)
        return subtasks, report, []

    coverage = await review_coverage(brief, subtasks, registry, ctx, prompt_log=prompt_log)
    if not uncovered_criteria(coverage):
        return subtasks, report, coverage

    missing = "\n".join(
        f"- criterion {c.criterion_index}: {brief.acceptance_criteria[c.criterion_index]!r} "
        f"is not covered"
        for c in coverage
        if c.verdict is CoverageVerdict.UNCOVERED
        and c.criterion_index < len(brief.acceptance_criteria)
    )
    system = prompts.system_prompt("planner")
    user = (
        f"{_brief_block(brief)}\n\n"
        f"Available agents:\n{_registry_block(registry)}\n\n"
        f"A review of your plan found uncovered criteria:\n{missing}\n\n"
        "Plan again so these are covered too."
    )
    plan = await call_structured("planner", system, user, Plan, ctx, prompt_log=prompt_log)
    revised = ensure_contradiction_checker(plan.subtasks)
    revised_report = validate_plan(revised, brief, set(registry.agents))
    if revised_report.ok:
        coverage = await review_coverage(
            brief, revised, registry, ctx, prompt_log=prompt_log
        )
        return revised, revised_report, coverage

    # The revision is broken where the original was merely incomplete.
    return subtasks, report, coverage
