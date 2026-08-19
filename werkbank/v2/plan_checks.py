"""Werkbank v2 — plan validation.

Assignment rules 1–7 are mechanically decidable, so they run as code. Only one
question about a plan needs judgement — *which acceptance criterion do the
assigned agents not actually cover* — and that one goes to the plan critic.

Asking a model to check "is this DAG acyclic" wastes a call and gets it wrong
occasionally; asking it "does this plan feel complete" gets a yes every time.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from werkbank.v2.models import Brief, Subtask

# Agents whose input is facts rather than sources. They are the only ones
# allowed to exist without a source of their own — and the only ones that must
# depend on something.
FACT_ONLY_AGENTS = {"synthesizer", "contradiction_checker"}
CONTRADICTION_CHECKER = "contradiction_checker"


@dataclass
class PlanReport:
    ok: bool = True
    defects: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.ok = False
        self.defects.append(message)


def find_cycle(subtasks: list[Subtask]) -> list[str]:
    """The subtask ids forming a dependency cycle, empty when acyclic."""
    edges = {s.subtask_id: list(s.depends_on) for s in subtasks}
    state: dict[str, int] = {}     # 0 = open, 1 = on stack, 2 = done
    path: list[str] = []

    def walk(node: str) -> list[str]:
        if state.get(node) == 1:
            return path[path.index(node):] + [node]
        if state.get(node) == 2:
            return []
        state[node] = 1
        path.append(node)
        for dep in edges.get(node, []):
            if dep in edges and (cycle := walk(dep)):
                return cycle
        path.pop()
        state[node] = 2
        return []

    for node in edges:
        if cycle := walk(node):
            return cycle
    return []


def topological_order(subtasks: list[Subtask]) -> list[list[str]]:
    """Execution levels: everything in one level may run in parallel."""
    remaining = {s.subtask_id: set(s.depends_on) for s in subtasks}
    done: set[str] = set()
    levels: list[list[str]] = []
    while remaining:
        ready = sorted(sid for sid, deps in remaining.items() if deps <= done)
        if not ready:
            break                        # cycle; find_cycle reports it
        levels.append(ready)
        done |= set(ready)
        for sid in ready:
            remaining.pop(sid)
    return levels


def ensure_contradiction_checker(subtasks: list[Subtask]) -> list[Subtask]:
    """Append the contradiction pass if the planner did not, or fix its deps.

    Appended by code rather than demanded from the model: it must run exactly
    once, over everything, and a planner that forgets it produces a report that
    silently picks one side of a conflict.
    """
    others = [s for s in subtasks if s.agent != CONTRADICTION_CHECKER]
    existing = [s for s in subtasks if s.agent == CONTRADICTION_CHECKER]
    if not others:
        return subtasks

    depended_on = {dep for s in others for dep in s.depends_on}
    leaves = [s.subtask_id for s in others if s.subtask_id not in depended_on]

    checker = existing[0] if existing else Subtask(
        subtask_id=f"st{max(int(s.subtask_id[2:]) for s in others) + 1}",
        question="Welche der gefundenen Aussagen widersprechen einander?",
        agent=CONTRADICTION_CHECKER,
        acceptance_criteria=["benennt jeden Widerspruch mit beiden Fact-IDs"],
    )
    checker.depends_on = leaves          # always over the whole run, not a subset
    return others + [checker]


def validate_plan(
    subtasks: list[Subtask], brief: Brief, known_agents: set[str]
) -> PlanReport:
    """Rules 1–6 as code. Defects are phrased for a replanning call."""
    report = PlanReport()
    ids = [s.subtask_id for s in subtasks]

    if not subtasks:
        report.fail("the plan has no subtasks")
        return report

    if len(set(ids)) != len(ids):
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        report.fail(f"subtask ids are not unique: {duplicates}")

    budget = brief.budget
    if len(subtasks) > budget.max_subtasks:
        report.fail(
            f"{len(subtasks)} subtasks exceed the budget of {budget.max_subtasks} "
            f"for depth '{brief.depth_budget.value}'"
        )

    for sub in subtasks:
        if sub.agent not in known_agents:
            # Rule 3: a tool the user does not have does not exist. Assigning
            # such an agent is how a run ends up answering from memory.
            report.fail(
                f"{sub.subtask_id}: agent '{sub.agent}' is not available for this run "
                f"(available: {sorted(known_agents)})"
            )
        if not sub.question.strip():
            report.fail(f"{sub.subtask_id}: has no question")
        if not sub.acceptance_criteria:
            # Rule 4: without a bar of its own a subtask cannot be judged.
            report.fail(f"{sub.subtask_id}: has no acceptance_criteria")

        unknown_deps = [d for d in sub.depends_on if d not in ids]
        if unknown_deps:
            report.fail(f"{sub.subtask_id}: depends on unknown subtasks {unknown_deps}")
        if sub.subtask_id in sub.depends_on:
            report.fail(f"{sub.subtask_id}: depends on itself")

        if sub.agent in FACT_ONLY_AGENTS and not sub.depends_on:
            report.fail(
                f"{sub.subtask_id}: '{sub.agent}' works on facts and must depend on "
                f"at least one other subtask"
            )

        for index in sub.covers_criteria:
            if not 0 <= index < len(brief.acceptance_criteria):
                report.fail(
                    f"{sub.subtask_id}: covers_criteria references criterion {index}, "
                    f"which does not exist"
                )

    if cycle := find_cycle(subtasks):
        report.fail(f"the dependencies form a cycle: {' → '.join(cycle)}")

    # Rule 6: every brief criterion must be addressed by at least one subtask.
    covered = {i for s in subtasks for i in s.covers_criteria}
    for index, criterion in enumerate(brief.acceptance_criteria):
        if index not in covered:
            report.fail(
                f"no subtask covers acceptance criterion {index}: {criterion!r}"
            )

    checkers = [s for s in subtasks if s.agent == CONTRADICTION_CHECKER]
    if len(checkers) > 1:
        report.fail("contradiction_checker appears more than once; it runs exactly once")

    return report
