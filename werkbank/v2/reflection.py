"""Werkbank v2 — the self-reflection block, generated from the run state.

No model touches this. Asked to reflect on its own work, a model writes three
well-formed sentences about "the limitations of this analysis" and does not
mention that subtask 4 found nothing — not out of malice, but because the
summary it produces is a summary of the *report*, and the report is where the
holes are already missing.

So the block is assembled from what actually happened: which subtasks ended
unresolved, which questions were left open, which claims rest on the model's own
knowledge, where sources contradict each other, which criteria were not met.
The writer may comment on it. It may not shorten it, and `writer.py` checks.

Deterministic honesty beats performed honesty.
"""

from __future__ import annotations

from collections import Counter

from werkbank.v2.models import (
    CriterionVerdict,
    CoverageVerdict,
    Evidence,
    RunState,
    SubtaskStatus,
)

HEADING = "## Selbstreflexion"
# The marker the writer's output is checked against. Prose may follow it, but
# what stands between the markers has to be byte-identical.
BEGIN = "<!-- reflection:begin -->"
END = "<!-- reflection:end -->"


def _fact_label(state: RunState, fact_id: str) -> str:
    fact = state.fact_by_id(fact_id)
    if fact is None:
        return fact_id
    claim = fact.claim.replace("\n", " ")
    return f"[{fact_id}] {claim[:110]}{'…' if len(claim) > 110 else ''}"


def build(state: RunState) -> str:
    """The complete block, wrapped in its markers."""
    lines: list[str] = [HEADING, ""]
    empty = True

    # 1. Subtasks that ended without an answer.
    unresolved = [
        r for r in state.results.values() if r.status is SubtaskStatus.UNRESOLVABLE
    ]
    if unresolved:
        empty = False
        lines += ["**Nicht beantwortete Teilfragen**", ""]
        for result in sorted(unresolved, key=lambda r: r.subtask_id):
            capped = " (Revisionslimit erreicht)" if result.subtask_id in state.capped_subtasks else ""
            lines.append(f"- `{result.subtask_id}` ({result.agent}){capped}: {result.question}")
        lines.append("")

    # 2. Everything explicitly declared missing.
    gaps = [(r.subtask_id, g) for r in state.results.values() for g in r.gaps]
    if gaps:
        empty = False
        lines += ["**Offene Lücken**", ""]
        for subtask_id, gap in gaps:
            source = f", vorgeschlagene Quelle: {gap.suggested_source}" if gap.suggested_source else ""
            lines.append(f"- `{subtask_id}`: {gap.question} — Grund: `{gap.reason.value}`{source}")
        lines.append("")

    # 3. Contradictions, with the trust levels spelled out.
    pairs: list[tuple[str, str]] = []
    seen: set[frozenset[str]] = set()
    for fact in state.all_facts():
        for other in fact.contradicts:
            key = frozenset({fact.id, other})
            if other and key not in seen:
                seen.add(key)
                pairs.append((fact.id, other))
    if pairs:
        empty = False
        lines += ["**Widersprüche zwischen Quellen**", ""]
        for left, right in pairs:
            lines.append(f"- {_fact_label(state, left)}")
            lines.append(f"  widerspricht {_fact_label(state, right)}")
        lines.append("")

    # 4. Claims with no source at all.
    from_memory = [
        f for f in state.all_facts()
        if f.evidence in (Evidence.MODEL_KNOWLEDGE, Evidence.NONE)
    ]
    if from_memory:
        empty = False
        lines += ["**Aussagen ohne Quelle (Modellwissen)**", ""]
        for fact in from_memory:
            lines.append(f"- {_fact_label(state, fact.id)}")
        lines.append("")

    # 5. Criteria the critic did not see satisfied.
    unmet: list[str] = []
    for subtask_id, verdict in state.verdicts.items():
        for entry in verdict.criteria:
            if entry.verdict is not CriterionVerdict.MET:
                unmet.append(
                    f"- `{subtask_id}`: {entry.criterion} — Urteil: `{entry.verdict.value}`"
                )
    if unmet:
        empty = False
        lines += ["**Nicht vollständig erfüllte Abnahmekriterien**", "", *unmet, ""]

    # 6. Criteria the plan never covered.
    weak_plan = [
        c for c in state.plan_coverage if c.verdict is not CoverageVerdict.COVERED
    ]
    if weak_plan and state.brief:
        empty = False
        lines += ["**Vom Plan nicht abgedeckte Kriterien**", ""]
        for entry in weak_plan:
            if entry.criterion_index < len(state.brief.acceptance_criteria):
                lines.append(
                    f"- {state.brief.acceptance_criteria[entry.criterion_index]} "
                    f"— `{entry.verdict.value}`"
                )
        lines.append("")

    # 7. Paragraphs the writer left without evidence.
    if state.flagged_paragraphs:
        empty = False
        lines += ["**Absätze ohne Fact-Beleg**", ""]
        lines += [f"- {p[:120]}…" for p in state.flagged_paragraphs]
        lines.append("")

    # 8. What the whole report rests on.
    trust_counts: Counter[str] = Counter()
    for fact in state.all_facts():
        for source in fact.sources:
            trust_counts[source.trust.value] += 1
        if not fact.sources:
            trust_counts["ohne Quelle"] += 1
    if trust_counts:
        empty = False
        lines += ["**Quellenverteilung**", ""]
        for trust, count in trust_counts.most_common():
            lines.append(f"- `{trust}`: {count}")
        lines.append("")

    if empty:
        lines += [
            "Alle Teilfragen wurden beantwortet, jede Aussage ist belegt, und es "
            "wurden keine Widersprüche zwischen den Quellen gefunden.",
            "",
        ]

    return f"{BEGIN}\n" + "\n".join(lines).rstrip() + f"\n{END}"


def extract(report: str) -> str:
    """The block as it stands in a report, empty when the markers are gone."""
    start = report.find(BEGIN)
    end = report.find(END)
    if start == -1 or end == -1 or end < start:
        return ""
    return report[start:end + len(END)]
