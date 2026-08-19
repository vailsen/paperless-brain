"""Werkbank v2 — BRIEFER.

One call. Produces the `Brief` that everything downstream is measured against,
and replaces the old free-prose reformulation step: every reformulation stage is
an opportunity for semantic drift with no mechanism to detect it.

Two things are enforced here in code rather than hoped for in the prompt:

- **`original_request` is restored verbatim after the call.** The model is asked
  to copy it unchanged and will sometimes tidy it anyway. The user's own wording
  is what both critics are later held against, so it is overwritten from the
  input rather than trusted from the output.
- **Vague acceptance criteria are rejected and re-asked.** Criteria are the most
  important lever in the system: without a bar registered *before* the research,
  the critic invents one afterwards and always clears it. "Gives a comprehensive
  answer" is not a bar.
"""

from __future__ import annotations

import re
from pathlib import Path

from werkbank.v2.llm import LLMContext, PromptLog, call_structured
from werkbank.v2.models import Brief, DepthBudget
from werkbank.v2 import prompts

PROMPT_PATH = Path(__file__).parent / "prompts" / "briefer.md"

# Phrases that describe an impression rather than an artefact. A criterion built
# from one of these cannot be decided by reading the report, which is the whole
# requirement. Matched on the normalised criterion, both languages.
VAGUE_PATTERNS = [
    r"\bumfassend", r"\bausführlich", r"\bdetailliert\b", r"\bgründlich",
    r"\brelevante[rn]?\s+aspekte", r"\balle\s+relevanten\b", r"\bangemessen",
    r"\bsinnvoll", r"\bvollständige\s+antwort",
    # "gut/schlecht" as the measure: the judgement is the reader's, and the
    # criterion has to be decidable without one.
    r"\bgut\b", r"\bgute[rnms]?\b", r"\bschlecht",
    r"\bcomprehensive\b", r"\bthorough\b", r"\bin\s+detail\b", r"\bdetailed\b",
    r"\bprecise\b", r"\bgood\b", r"\ball\s+relevant\b", r"\bappropriate\b",
    r"\bwell[-\s]written\b", r"\bhigh[-\s]quality\b",
]
_VAGUE_RE = re.compile("|".join(VAGUE_PATTERNS), re.IGNORECASE)

# Below this a "criterion" is a label, not a sentence anyone could check.
MIN_CRITERION_CHARS = 15

# Words that mark a request as a comparison. Those need a criterion that fixes
# what counts as comparable, or the run compares names instead of functions —
# the failure that produced competitor "products" serving a different purpose.
COMPARISON_MARKERS = re.compile(
    r"\b(wettbewerb\w*|konkurren\w*|mitbewerber\w*|alternativ\w*|vergleich\w*|"
    r"ähnlich\w*|competitor\w*|alternative\w*|compare\w*|comparison|similar|rival\w*)\b",
    re.IGNORECASE,
)
DEFINITION_MARKERS = re.compile(
    r"\b(funktion\w*|zweck\w*|einsatzzweck\w*|verwendung\w*|kriterium|kriterien|definiert|"
    r"gilt als|z[äa]hlt als|function\w*|purpose\w*|counts? as|qualif\w*|defin\w*)\b",
    re.IGNORECASE,
)


def criterion_problem(criterion: str) -> str:
    """Why this criterion cannot be checked — empty string when it can.

    Deliberately not a grammatical verb detector: "starts with a verb" is not
    reliably decidable across German and English without a parser, and a wrong
    rejection of a good criterion is worse than a missed vague one. What is
    decidable is the vocabulary of vagueness, and that is what this catches.
    """
    text = (criterion or "").strip()
    if len(text) < MIN_CRITERION_CHARS:
        return "too short to be checkable"
    if _VAGUE_RE.search(text):
        return "describes an impression, not a checkable artefact"
    if text.endswith("?"):
        return "is a question, not a criterion"
    return ""


def missing_definition_criterion(brief: Brief) -> bool:
    """True when a comparison task has no criterion fixing what is comparable."""
    haystack = f"{brief.original_request} {brief.goal}"
    if not COMPARISON_MARKERS.search(haystack):
        return False
    return not any(DEFINITION_MARKERS.search(c) for c in brief.acceptance_criteria)


def brief_defects(brief: Brief) -> list[str]:
    """Everything wrong with a brief, in the words the model needs to fix it."""
    defects: list[str] = []
    if not brief.goal.strip():
        defects.append("goal is empty")
    if not brief.acceptance_criteria:
        defects.append("acceptance_criteria is empty — at least one is required")
    for criterion in brief.acceptance_criteria:
        if problem := criterion_problem(criterion):
            defects.append(f"acceptance criterion {criterion!r} {problem}")
    if missing_definition_criterion(brief):
        defects.append(
            "this is a comparison task: add a criterion that fixes what counts as "
            "comparable — by function, not by name"
        )
    return defects


def load_prompt() -> str:
    return prompts.system_prompt("briefer")


async def build_brief(
    request: str,
    ctx: LLMContext,
    *,
    prompt_log: PromptLog | None = None,
    max_attempts: int = 2,
) -> Brief:
    """Request → Brief. Retries once with the concrete defects named.

    Never raises for a weak brief: the user confirms and can edit it anyway, and
    a run that refuses to start because a criterion was phrased badly is worse
    than one that starts with a criterion the user then fixes. It raises only
    when no schema-valid answer comes back at all.
    """
    system = load_prompt()
    user = f"User request:\n\n{request}"
    brief: Brief | None = None

    for attempt in range(max_attempts):
        brief = await call_structured(
            "briefer", system, user, Brief, ctx, prompt_log=prompt_log
        )
        # The model was asked to copy this unchanged. Trust is unnecessary here:
        # the input is right there, and both critics are held against it later.
        brief.original_request = request
        if not isinstance(brief.depth_budget, DepthBudget):
            brief.depth_budget = DepthBudget.STANDARD

        defects = brief_defects(brief)
        if not defects or attempt == max_attempts - 1:
            break
        user = (
            f"User request:\n\n{request}\n\n"
            "Your previous Brief had these problems:\n"
            + "\n".join(f"- {d}" for d in defects)
            + "\n\nWrite the Brief again, fixing exactly these points."
        )

    assert brief is not None
    return brief
