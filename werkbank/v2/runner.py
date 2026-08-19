"""Werkbank v2 — RUNNER: one subtask, one agent.

Two calls, deliberately separate:

1. **Gather.** The model works with its tools; every retrieval is logged with
   its raw text and its trust.
2. **Report.** A second call, against the `SubtaskResult` schema, with the
   catalogue of retrieved sources in front of it.

Mixing the two produces prose with a JSON block stapled on. Splitting them also
means the schema call sees the source ids it is allowed to cite, which is what
makes "do not invent a source id" checkable rather than aspirational.

A dependent subtask receives **only the facts** of its predecessors, never
their narrative or raw tool output. That keeps provenance intact and keeps
context growth bounded on deep DAGs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from werkbank.v2 import agent_loop
from werkbank.v2 import store
from werkbank.v2.llm import LLMContext, PromptLog, call_structured
from werkbank.v2.models import (
    GapReason,
    Fact,
    SelfCheck,
    Subtask,
    SubtaskResult,
    SubtaskStatus,
    apply_derived_confidence,
    relabel_mislabelled_evidence,
    strip_llm_controlled,
)
from werkbank.v2.registry import AgentSpec, Registry
from werkbank.v2.tools import ToolBelt, apply_tool_trust

_log = logging.getLogger(__name__)

PROMPT_ROOT = Path(__file__).parent


def renumber_facts(payload: dict, subtask_id: str) -> dict:
    """Rewrite fact ids onto this subtask before the payload is validated.

    A model that labels its facts `st7.f1` inside subtask `st1` has made a
    bookkeeping error, not a claim about the world — but the model layer
    rejects the inconsistency outright, so the repair has to happen at the
    parse boundary or the whole result is thrown away over a prefix. References
    inside `derived_from` and the narrative are rewritten with it.
    """
    facts = payload.get("facts")
    if not isinstance(facts, list):
        return payload

    mapping: dict[str, str] = {}
    for index, fact in enumerate(facts, start=1):
        if not isinstance(fact, dict):
            continue
        wanted = f"{subtask_id}.f{index}"
        current = str(fact.get("id") or "")
        if current != wanted:
            if current:
                mapping[current] = wanted
            fact["id"] = wanted

    if mapping:
        for fact in facts:
            if isinstance(fact, dict):
                fact["derived_from"] = [
                    mapping.get(ref, ref) for ref in (fact.get("derived_from") or [])
                ]
        narrative = payload.get("narrative")
        if isinstance(narrative, str):
            for old, new in mapping.items():
                narrative = narrative.replace(f"[{old}]", f"[{new}]")
            payload["narrative"] = narrative
    return payload


def agent_prompt(spec: AgentSpec) -> str:
    if spec.prompt_text.strip():
        return spec.prompt_text                 # the user's edited version wins
    path = spec.prompt_path()
    if path and path.is_file():
        return path.read_text(encoding="utf-8")
    # A user-defined archetype has no prompt file — its description is its brief.
    return (
        f"You are the agent '{spec.label}'. {spec.description}\n\n"
        "Answer the question strictly from what your tools return, cite verbatim, "
        "and record anything you could not answer as a gap."
    )


def facts_block(facts: list[Fact]) -> str:
    """What a dependent subtask inherits: claims and ids, no prose."""
    if not facts:
        return ""
    lines = ["Facts established by earlier subtasks (cite by id where you build on them):"]
    for fact in facts:
        lines.append(f"- [{fact.id}] ({fact.evidence.value}) {fact.claim}")
    return "\n".join(lines)


# How much retrieved text the reporting call is given back, in characters, and
# the floor each source keeps however many there are.
REPORT_EVIDENCE_BUDGET = 20_000
MIN_SOURCE_EXCERPT = 800


def evidence_block(belt: ToolBelt) -> str:
    """The retrieved text, handed back to the call that writes the facts.

    Without this the two calls are joined only by the model's own closing notes:
    everything a tool returned is gone by the time the facts are written, so a
    fact can only be as good as the paraphrase the model happened to leave
    behind — and a `quote` can only be as verbatim as its memory of it. The
    observed failure is stark: five e-mail searches, 299 hits, and a single fact
    saying "searches were carried out".

    Bounded on purpose. Excerpts, labelled as excerpts, with the full length
    named so the model can say "the first 50 of 143" instead of implying it saw
    everything.
    """
    from werkbank.v2.tools import OUTCOME_FAILED, _short

    if not belt.records:
        return "(nothing retrieved)"

    usable = [r for r in belt.records if r.outcome != OUTCOME_FAILED]
    per_source = (
        max(MIN_SOURCE_EXCERPT, REPORT_EVIDENCE_BUDGET // len(usable))
        if usable else 0
    )
    blocks = []
    for record in belt.records:
        head = f"--- {record.source_id}: {record.tool}({_short(record.args)})"
        if record.outcome == OUTCOME_FAILED:
            # Named, but with no text: an error message is not evidence, and a
            # fact must never be able to quote one.
            blocks.append(f"{head} — FAILED, this call did not run ---")
            continue
        text = record.raw_text or ""
        cut = " (excerpt)" if len(text) > per_source else ""
        hits = f", {record.hits} hits" if record.hits is not None else ""
        blocks.append(
            f"{head}{hits}, {len(text)} characters{cut} ---\n{text[:per_source]}"
        )
    return "\n\n".join(blocks)


def task_block(subtask: Subtask, inherited: list[Fact], belt: ToolBelt) -> str:
    parts = [
        f"Question: {subtask.question}",
        "Acceptance criteria for this subtask:\n"
        + "\n".join(f"- {c}" for c in subtask.acceptance_criteria),
    ]
    if subtask.sources_restrict:
        parts.append(
            "Restricted to these sources: " + ", ".join(subtask.sources_restrict)
        )
    if block := facts_block(inherited):
        parts.append(block)
    return "\n\n".join(parts)


async def run_subtask(
    subtask: Subtask,
    spec: AgentSpec,
    registry: Registry,
    ctx: LLMContext,
    *,
    inherited_facts: list[Fact] | None = None,
    defects: list[str] | None = None,
    revision: int = 0,
    prompt_log: PromptLog | None = None,
    persist: bool = True,
) -> tuple[SubtaskResult, ToolBelt]:
    """Execute one subtask. Returns the result and the belt that produced it.

    The belt goes back to the caller because the deterministic checks need what
    it recorded — the raw text for D2 and the call count for D5. Neither can be
    reconstructed afterwards.
    """
    inherited = inherited_facts or []
    started = datetime.now(timezone.utc)

    belt = ToolBelt(
        registry=registry,
        allowed_tools=spec.tools,
        run_id=ctx.run_id,
        user_id=ctx.user_id,
        token=ctx.token,
        subtask_id=subtask.subtask_id,
        sources_restrict=subtask.sources_restrict,
        persist=persist,
        prior_queries=(
            store.tool_queries(ctx.run_id, ctx.user_id, subtask.subtask_id)
            if persist and ctx.run_id else set()
        ),
    )

    system = agent_prompt(spec)
    task = task_block(subtask, inherited, belt)
    if defects:
        task += (
            "\n\nA review of your previous attempt found these defects. "
            "Fix exactly these:\n" + "\n".join(f"- {d}" for d in defects)
        )

    scratch = await agent_loop.gather(
        system, task, belt,
        model=ctx.model, user_id=ctx.user_id, token=ctx.token,
    )

    report_task = (
        f"{task}\n\n"
        f"Sources you retrieved (cite only these ids):\n{belt.catalogue()}\n\n"
        f"What those sources returned (quote only from this text):\n"
        f"{evidence_block(belt)}\n\n"
        f"Your working notes:\n{scratch or '(none)'}\n\n"
        f"Now report the result as facts and gaps. Every fact id starts with "
        f"'{subtask.subtask_id}.f'."
    )
    def _sanitize(payload: dict) -> dict:
        payload = relabel_mislabelled_evidence(strip_llm_controlled(payload))
        return renumber_facts(payload, subtask.subtask_id)

    result = await call_structured(
        "runner", system, report_task, SubtaskResult, ctx,
        prompt_log=prompt_log, sanitize=_sanitize,
    )

    # Everything the model does not get to decide, decided here.
    finished = datetime.now(timezone.utc)
    result.subtask_id = subtask.subtask_id
    result.question = subtask.question
    result.acceptance_criteria = subtask.acceptance_criteria
    result.covers_criteria = subtask.covers_criteria
    result.depends_on = subtask.depends_on
    result.agent = subtask.agent
    result.sources_restrict = subtask.sources_restrict
    result.model = ctx.model
    result.revision = revision
    result.started_at = started.isoformat()
    result.finished_at = finished.isoformat()
    result.duration_s = (finished - started).total_seconds()
    result.self_check = SelfCheck(
        claims_without_source=sum(1 for f in result.facts if not f.sources),
        sources_fetched=belt.call_count,
        tool_calls=belt.call_count,
    )

    result = apply_tool_trust(result, belt)
    for fact in result.facts:
        apply_derived_confidence(fact)

    _mark_dead_sources(result, belt)

    if not result.facts and result.gaps:
        result.status = SubtaskStatus.PARTIAL
    return result, belt


def _mark_dead_sources(result: SubtaskResult, belt: ToolBelt) -> None:
    """A source that answered nothing every time did not establish an absence.

    A throttled search host returns HTTP 200 with zero results, so "the web has
    nothing on this" and "the search was refusing me" reach the model as the
    same string. Recording that as `not_found` turns an outage into a finding.
    The gap stays, its reason changes to what actually happened.
    """
    failed = belt.failed_tools()
    dead = [t for t in belt.dead_tools() if t not in failed]
    if not (failed or dead):
        return
    reasons = []
    if failed:
        # The stronger case, and the one that used to slip through: the tool
        # never ran. "IMAP not configured" is not a search result.
        reasons.append(f"{', '.join(failed)} could not run at all")
    if dead:
        reasons.append(f"{', '.join(dead)} returned nothing on every attempt")
    note = f"({'; '.join(reasons)} — unavailable, not evidence of absence)"
    for gap in result.gaps:
        if gap.reason is GapReason.NOT_FOUND:
            gap.reason = GapReason.SOURCE_UNAVAILABLE
            gap.note = (gap.note + " " if gap.note else "") + note
