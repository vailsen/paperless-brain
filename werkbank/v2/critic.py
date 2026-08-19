"""Werkbank v2 — FACT_CRITIC and the revision loop.

Order is not negotiable: **the deterministic checks run first**, and what they
rejected the critic never sees. A model asked to review a fact it just wrote
tends to agree with it; the checks are the part of the verdict that does not
depend on that.

The critic runs on the same model as the runner, because in a single-model
setup there is no other. Everything here is a countermeasure to that:

- it is given the facts and the retrieved snippets, and **not the narrative** —
  the reasoning is the main transport for self-approval;
- the question presupposes a defect ("which part does *not* support this")
  rather than inviting agreement;
- it runs at temperature 0.1 while the runner explores at 0.4;
- a criterion it calls met without naming a fact is set to `unmet` by code.
"""

from __future__ import annotations

import logging
from pathlib import Path

from werkbank.v2 import checks
from werkbank.v2.llm import LLMContext, PromptLog, call_structured
from werkbank.v2.models import (
    CriterionVerdict,
    CriticDecision,
    CriticVerdict,
    Subtask,
    SubtaskResult,
    SubtaskStatus,
)
from werkbank.v2.registry import AgentSpec, Registry
from werkbank.v2.runner import run_subtask
from werkbank.v2.tools import ToolBelt, has_retrieval
from werkbank.v2 import prompts

_log = logging.getLogger(__name__)

CRITIC_PROMPT = Path(__file__).parent / "prompts" / "fact_critic.md"
SNIPPET_CHARS = 1200


def _facts_block(result: SubtaskResult) -> str:
    lines = []
    for fact in result.facts:
        quotes = "; ".join(f'"{s.quote}" ({s.id})' for s in fact.sources if s.quote)
        lines.append(
            f"[{fact.id}] evidence={fact.evidence.value} kind={fact.kind.value}\n"
            f"  claim: {fact.claim}\n"
            f"  quoted: {quotes or '(none)'}"
        )
    return "\n".join(lines) or "(no facts survived the automatic checks)"


def critic_input(
    result: SubtaskResult, original_request: str, raw_texts: dict[str, str]
) -> str:
    """Everything the critic gets — and nothing else.

    Notably absent: `result.narrative`. Phase 4's acceptance criterion is that
    the prompt log proves it was never in the context, so it must not be
    assembled here in the first place.
    """
    snippets = "\n\n".join(
        f"--- {source_id} (excerpt: first {SNIPPET_CHARS} characters of "
        f"{len(text)}) ---\n{text[:SNIPPET_CHARS]}"
        for source_id, text in raw_texts.items()
    )
    criteria = "\n".join(f"[{i}] {c}" for i, c in enumerate(result.acceptance_criteria))
    return (
        f"Original user request (verbatim):\n{original_request}\n\n"
        f"Question of this subtask:\n{result.question}\n\n"
        f"Acceptance criteria:\n{criteria or '(none)'}\n\n"
        f"Facts to check:\n{_facts_block(result)}\n\n"
        "Retrieved source text (excerpts — quotes were already matched against "
        "the full text in code):\n"
        f"{snippets or '(nothing retrieved)'}"
    )


def enforce_evidence(verdict: CriticVerdict, result: SubtaskResult) -> CriticVerdict:
    """A criterion called met without a fact is unmet, whatever the model said."""
    known = result.fact_ids()
    for entry in verdict.criteria:
        entry.fact_ids = [fid for fid in entry.fact_ids if fid in known]
        if entry.verdict is not CriterionVerdict.UNMET and not entry.fact_ids:
            entry.verdict = CriterionVerdict.UNMET
    return verdict


async def review(
    result: SubtaskResult,
    raw_texts: dict[str, str],
    original_request: str,
    ctx: LLMContext,
    *,
    prompt_log: PromptLog | None = None,
) -> CriticVerdict:
    """The LLM half of the review. The deterministic half has already run."""
    verdict = await call_structured(
        "fact_critic",
        prompts.system_prompt("fact_critic"),
        critic_input(result, original_request, raw_texts),
        CriticVerdict,
        ctx,
        prompt_log=prompt_log,
    )
    return enforce_evidence(verdict, result)


async def run_with_review(
    subtask: Subtask,
    spec: AgentSpec,
    registry: Registry,
    ctx: LLMContext,
    *,
    original_request: str,
    max_revisions: int,
    inherited_facts=None,
    known_fact_ids: set[str] | None = None,
    prompt_log: PromptLog | None = None,
    persist: bool = True,
) -> tuple[SubtaskResult, CriticVerdict, bool]:
    """Run, check, review, revise — up to the cap.

    Returns (result, last verdict, hit_cap). Reaching the cap ends the subtask
    as `unresolvable` rather than trying again: without a cap there is
    ping-pong, and a report that can never say "not answerable" is not honest,
    it is just quiet.
    """
    defects: list[str] = []
    verdict = CriticVerdict(decision=CriticDecision.ACCEPT)
    result: SubtaskResult | None = None
    belt: ToolBelt | None = None
    # The best attempt so far, by surviving facts. A revision can come back with
    # less than the attempt it was asked to improve — a search that worked the
    # first time gets throttled, a model wanders off — and taking the last one
    # regardless threw away the better answer that had already been paid for.
    best: tuple[SubtaskResult, CriticVerdict] | None = None

    for revision in range(max_revisions + 1):
        result, belt = await run_subtask(
            subtask, spec, registry, ctx,
            inherited_facts=inherited_facts, defects=defects, revision=revision,
            prompt_log=prompt_log, persist=persist,
        )

        # Deterministic first. Rejected facts are gone before the model looks.
        result, report = checks.run_all(
            result,
            raw_texts=belt.raw_texts(),
            known_fact_ids=known_fact_ids or set(),
            tools_were_available=has_retrieval(spec.tools),
        )

        if result.status is SubtaskStatus.UNRESOLVABLE:
            # D8 fired: nothing survived and nothing was declared missing. That
            # is a defect the agent can act on ("you produced neither facts nor
            # gaps"), not a property of the question — and a subtask that made
            # seventeen tool calls has material to work from. Retry while the
            # budget allows; only the last attempt is terminal.
            terminal = revision >= max_revisions
            verdict = CriticVerdict(
                decision=(
                    CriticDecision.UNRESOLVABLE if terminal else CriticDecision.REVISE
                ),
                defects=report.messages,
            )
        else:
            try:
                verdict = await review(
                    result, belt.raw_texts(), original_request, ctx,
                    prompt_log=prompt_log,
                )
            except Exception as exc:                        # noqa: BLE001
                # The critic is the *second* opinion; D1–D9 have already run and
                # this result passed them. Losing the subtask because the second
                # opinion could not be obtained is the wrong trade — one run
                # discarded 21 searches and 18 fetched pages when the critic
                # call came back without a tool call three times.
                _log.warning(
                    "werkbank v2 %s: critic unavailable (%s) — the deterministic "
                    "checks stand on their own", subtask.subtask_id, exc,
                )
                verdict = CriticVerdict(
                    decision=CriticDecision.ACCEPT,
                    defects=[f"Fact critic could not be reached: {exc}"],
                )
            # The checks outrank the critic: a subtask whose facts were thrown
            # out cannot be accepted because the model liked what remained.
            if not report.passed and verdict.decision is CriticDecision.ACCEPT:
                verdict.decision = CriticDecision.REVISE
                verdict.defects = list(verdict.defects) + report.messages

            # …and they outrank it in the other direction too. A subtask that
            # never called a tool has not established that its question cannot
            # be answered — it has established nothing. Seen in the wild: an
            # agent declared "no web search tool is available in this
            # environment", made zero calls, and filed four
            # `source_unavailable` gaps, while a sibling subtask was making
            # forty-four web calls in the same run with the same tools.
            # An agent that was handed facts and returns none has not answered
            # "no" — it has declined to answer. Seen in a real run: seven quoted
            # facts in, zero facts and five gaps out, so the report said nothing
            # although a great deal had been established.
            if (
                verdict.decision is CriticDecision.UNRESOLVABLE
                and revision < max_revisions
                and inherited_facts
                and not result.facts
            ):
                verdict.decision = CriticDecision.REVISE
                verdict.defects = [
                    f"You were given {len(inherited_facts)} established fact(s) and "
                    "produced none. Falling short of a criterion is a result: state "
                    "what the facts do establish, as derived facts, and record what "
                    "is still missing as gaps alongside them — not instead of them.",
                    *verdict.defects,
                ]

            if (
                verdict.decision is CriticDecision.UNRESOLVABLE
                and revision < max_revisions
                and has_retrieval(spec.tools)
                and result.self_check.tool_calls == 0
            ):
                verdict.decision = CriticDecision.REVISE
                verdict.defects = [
                    "You did not call a single tool. These are available to you "
                    f"in this subtask and they work: {', '.join(spec.tools)}. "
                    "Use them before concluding anything — a question you did "
                    "not look into is not a question without an answer.",
                    *verdict.defects,
                ]

        if best is None or len(result.facts) > len(best[0].facts):
            best = (result, verdict)

        if verdict.decision is CriticDecision.ACCEPT:
            result.status = _status_for(verdict, result)
            return result, verdict, False
        if verdict.decision is CriticDecision.UNRESOLVABLE:
            # An attempt that came back with nothing does not erase one that
            # came back with something. "Unresolvable" is a statement about the
            # question, not about the last roll of the dice.
            if best is not None and best[0].facts:
                kept, kept_verdict = best
                kept.status = SubtaskStatus.PARTIAL
                return kept, kept_verdict, False
            result.status = SubtaskStatus.UNRESOLVABLE
            return result, verdict, False

        defects = verdict.defects or report.messages
        _log.info(
            "werkbank v2 %s: revision %s requested (%d defects)",
            subtask.subtask_id, revision + 1, len(defects),
        )

    # Cap reached. Two things hold here:
    #
    # 1. The best attempt wins, not the last one. A revision that came back
    #    empty must not erase the attempt it was supposed to improve.
    # 2. What survived the deterministic checks survives the cap: facts that
    #    passed D1–D9 carry verified quotes, and discarding them because the
    #    critic wanted *more* throws away established work — in one run, five
    #    quoted facts, leaving the synthesizer nothing to build on.
    #
    # "Not everything was answered" is `partial`; `unresolvable` means nothing was.
    assert result is not None
    if best is not None and len(best[0].facts) > len(result.facts):
        result, verdict = best
    result.status = (
        SubtaskStatus.PARTIAL if result.facts else SubtaskStatus.UNRESOLVABLE
    )
    return result, verdict, True


def _status_for(verdict: CriticVerdict, result: SubtaskResult) -> SubtaskStatus:
    """Accepted, but a criterion left unmet is `partial` — not a clean pass."""
    if any(c.verdict is not CriterionVerdict.MET for c in verdict.criteria):
        return SubtaskStatus.PARTIAL
    if result.gaps:
        return SubtaskStatus.PARTIAL
    return SubtaskStatus.OK
