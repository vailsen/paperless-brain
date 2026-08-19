"""Werkbank v2 — the contradiction pass.

Runs once, at the end, over every accepted fact. Separate from the synthesizer
because its stance is adversarial and the two jobs fight each other: an agent
asked to consolidate will smooth a conflict away, which is precisely the
information worth keeping.

The pair that matters most is `authoritative` against `user_asserted` — a note
saying "notice period is three months" against a contract saying six weeks. The
note is a remembered guess; the contract is the fact. A report that treats them
as equally true is dishonest even when every individual step was clean, and the
user is the one who pays for that later.

The model only *names* pairs. Writing the `contradicts` references into both
facts is done here, in code, so a conflict cannot be recorded on one side only.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from werkbank.v2.llm import LLMContext, PromptLog, call_structured
from werkbank.v2.models import Fact, RunState
from werkbank.v2 import prompts

_log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "prompts" / "agents" / "contradiction_checker.md"


class ContradictionPair(BaseModel):
    fact_a: str
    fact_b: str
    nature: str = ""
    note: str = ""


class Contradictions(BaseModel):
    pairs: list[ContradictionPair] = Field(default_factory=list)


def facts_block(facts: list[Fact]) -> str:
    """Every fact with its trust — the trust is the point of the exercise."""
    lines = []
    for fact in facts:
        trust = ", ".join(sorted({s.trust.value for s in fact.sources})) or "model"
        lines.append(f"[{fact.id}] (trust: {trust}) {fact.claim}")
    return "\n".join(lines)


def apply_pairs(state: RunState, pairs: list[ContradictionPair]) -> list[ContradictionPair]:
    """Write each conflict into both facts. Unknown ids are dropped.

    Symmetry is the reason this is code: a reference recorded on one side only
    means the report can show the contradiction next to one claim and not the
    other, depending on which the writer happened to cite.
    """
    known = {f.id: f for f in state.all_facts()}
    applied: list[ContradictionPair] = []
    for pair in pairs:
        left, right = known.get(pair.fact_a), known.get(pair.fact_b)
        if left is None or right is None or left.id == right.id:
            _log.info("werkbank v2: dropping contradiction %s/%s", pair.fact_a, pair.fact_b)
            continue
        if right.id not in left.contradicts:
            left.contradicts.append(right.id)
        if left.id not in right.contradicts:
            right.contradicts.append(left.id)
        applied.append(pair)
    return applied


async def find(
    state: RunState,
    ctx: LLMContext,
    *,
    prompt_log: PromptLog | None = None,
) -> list[ContradictionPair]:
    """Ask once, then write the references. Never raises — a failed pass means
    no pairs, not a lost run."""
    facts = state.all_facts()
    if len(facts) < 2:
        return []

    user = (
        f"Original request:\n{state.brief.original_request if state.brief else ''}\n\n"
        f"All accepted facts of this run:\n{facts_block(facts)}"
    )
    try:
        found = await call_structured(
            "contradiction_checker",
            prompts.system_prompt("contradiction_checker"),
            user,
            Contradictions,
            ctx,
            prompt_log=prompt_log,
        )
    except Exception as exc:
        _log.warning("werkbank v2: contradiction pass failed: %s", exc)
        return []
    return apply_pairs(state, found.pairs)


def trust_conflicts(state: RunState) -> list[tuple[Fact, Fact]]:
    """Recorded pairs where a document and a personal note disagree.

    Surfaced separately because this is the one the reader most needs to see
    and most easily misses.
    """
    out: list[tuple[Fact, Fact]] = []
    known = {f.id: f for f in state.all_facts()}
    seen: set[frozenset[str]] = set()
    for fact in state.all_facts():
        for other_id in fact.contradicts:
            other = known.get(other_id)
            if other is None or frozenset({fact.id, other.id}) in seen:
                continue
            trusts_a = {s.trust.value for s in fact.sources}
            trusts_b = {s.trust.value for s in other.sources}
            if "authoritative" in trusts_a and "user_asserted" in trusts_b:
                out.append((fact, other))
                seen.add(frozenset({fact.id, other.id}))
            elif "authoritative" in trusts_b and "user_asserted" in trusts_a:
                out.append((other, fact))
                seen.add(frozenset({fact.id, other.id}))
    return out
