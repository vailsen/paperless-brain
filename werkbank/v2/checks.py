"""Werkbank v2 — deterministic checks D1–D9.

These run **before** the LLM critic and cannot be overruled by it. They cover
exactly the failure class where self-evaluation is least reliable: a model
judging whether its own citation says what it claims.

Pure functions, no side effects, no model calls — which is the point: the
checks have to be testable without an LLM, and they have to produce the same
verdict every time for the same input.

On fuzzy matching: the tasks document names `rapidfuzz.partial_ratio`.
Implemented here on the standard library instead (`difflib`), because the
comparison a quote check needs is "does this text occur in the retrieved text,
allowing for whitespace and typography" — which normalisation plus a substring
test answers exactly, with the fuzzy pass only for OCR-level noise. Swapping in
rapidfuzz means replacing `partial_ratio()` alone.
"""

from __future__ import annotations

import ast
import difflib
import operator
import re
import unicodedata
from dataclasses import dataclass, field

from werkbank.v2.models import (
    MARKER_RE,
    Evidence,
    Fact,
    SubtaskResult,
    SubtaskStatus,
)

QUOTE_MATCH_THRESHOLD = 0.90
# Shorter than this a "quote" carries no evidential weight — and matches
# almost any text by accident.
MIN_QUOTE_CHARS = 12


@dataclass
class CheckReport:
    """What a check found. `passed` is about the subtask, not about each fact."""

    passed: bool = True
    rejected_fact_ids: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)

    def merge(self, other: CheckReport) -> CheckReport:
        return CheckReport(
            passed=self.passed and other.passed,
            rejected_fact_ids=self.rejected_fact_ids + [
                i for i in other.rejected_fact_ids if i not in self.rejected_fact_ids
            ],
            flags=self.flags + other.flags,
            messages=self.messages + other.messages,
        )


# ── Text normalisation ────────────────────────────────────────────────────────

_QUOTE_CHARS = dict.fromkeys(map(ord, "«»„“”‟‘’‚‛\"'"), '"')
_DASHES = dict.fromkeys(map(ord, "–—‑‒−"), "-")


def normalize(text: str) -> str:
    """Fold away the differences that are never meaningful in a citation.

    Line breaks from a PDF, typographic quotes, non-breaking spaces and case
    are artefacts of how the text was extracted, not of what it says. Leaving
    them in would make honest quotes fail the match.
    """
    text = unicodedata.normalize("NFKC", text or "")
    text = text.translate(_QUOTE_CHARS).translate(_DASHES)
    text = re.sub(r"\s+", " ", text)
    return text.strip().casefold()


def partial_ratio(needle: str, haystack: str) -> float:
    """Best similarity of `needle` against any window of `haystack`, 0.0–1.0.

    Exact containment short-circuits to 1.0, which is the honest-quote case.
    Everything else slides a window the length of the needle over candidate
    positions and takes the best `difflib` ratio.
    """
    if not needle or not haystack:
        return 0.0
    if needle in haystack:
        return 1.0
    if len(needle) > len(haystack):
        needle, haystack = haystack, needle

    # Anchor on the longest word of the needle: a quote that shares no long
    # word with the text is not a near-miss, it is a different sentence.
    words = sorted((w for w in needle.split() if len(w) >= 4), key=len, reverse=True)
    anchors: list[int] = []
    for word in words[:3]:
        start = 0
        while len(anchors) < 40:
            idx = haystack.find(word, start)
            if idx == -1:
                break
            anchors.append(idx)
            start = idx + 1
    if not anchors:
        # No shared anchor — fall back to a coarse scan so a paraphrase of a
        # short quote still gets a number rather than a false 0.0.
        step = max(1, len(needle) // 2)
        anchors = list(range(0, max(1, len(haystack) - len(needle) + 1), step))[:40]

    best = 0.0
    span = len(needle)
    for idx in anchors:
        start = max(0, idx - span)
        window = haystack[start:idx + 2 * span]
        matcher = difflib.SequenceMatcher(None, needle, window, autojunk=False)
        if matcher.real_quick_ratio() <= best:
            continue
        # Longest common block against the window, expressed as coverage of the
        # needle — the "partial" in partial_ratio.
        blocks = matcher.get_matching_blocks()
        covered = sum(b.size for b in blocks)
        best = max(best, covered / span)
        if best >= 1.0:
            break
    return min(best, 1.0)


_WORD_RE = re.compile(r"[^\W\d_]{4,}", re.UNICODE)
_DIGITS_RE = re.compile(r"\d[\d.,]*")
# How many of a quote's distinctive words may be missing from the source text.
# Not zero: extraction drops the odd character, and a citation is not a diff.
WORD_SUPPORT_THRESHOLD = 0.90


def unsupported_tokens(quote: str, haystack: str) -> list[str]:
    """Distinctive tokens of the quote that do not occur in the source text.

    A similarity ratio alone cannot do this job. Change one word of a long
    sentence — "drei Monate" to "sechs Monate", "1.240" to "1.420" — and the
    two strings still overlap by ninety-odd percent, while the claim has been
    inverted. That single-token swap is the most consequential hallucination
    there is, so numbers must match exactly and long words nearly all.
    """
    missing: list[str] = []
    for number in _DIGITS_RE.findall(quote):
        token = number.rstrip(".,")
        if token and token not in haystack:
            missing.append(token)

    words = _WORD_RE.findall(quote)
    absent = [w for w in words if w not in haystack]
    if words and (len(words) - len(absent)) / len(words) < WORD_SUPPORT_THRESHOLD:
        missing.extend(absent)
    return missing


# ── Safe arithmetic (D3) ──────────────────────────────────────────────────────

_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def eval_expression(expr: str) -> float | None:
    """Evaluate `1240 + 890 * 2`. None when it is not that.

    A restrictive AST walk rather than `eval`: the expression comes from a
    language model, and `eval` on model output is a remote code execution
    waiting for the right prompt.
    """
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except (SyntaxError, ValueError):
        return None

    def walk(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError("only numbers")
            return float(node.value)
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](walk(node.left), walk(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](walk(node.operand))
        raise ValueError(f"not allowed: {ast.dump(node)}")

    try:
        return float(walk(tree))
    except (ValueError, TypeError, ZeroDivisionError, OverflowError):
        return None


_NUMBER_RE = re.compile(r"-?\d[\d.,]*")


def _numbers_in(text: str) -> list[float]:
    """Numbers in a claim, tolerating both 1.234,56 and 1,234.56."""
    out: list[float] = []
    for raw in _NUMBER_RE.findall(text or ""):
        token = raw.rstrip(".,")
        if "," in token and "." in token:
            token = (token.replace(".", "").replace(",", ".")
                     if token.rfind(",") > token.rfind(".")
                     else token.replace(",", ""))
        elif "," in token:
            token = token.replace(",", ".") if len(token.split(",")[-1]) != 3 else token.replace(",", "")
        try:
            out.append(float(token))
        except ValueError:
            continue
    return out


# ── D1–D9 ─────────────────────────────────────────────────────────────────────


def check_d2_quote_grounding(
    result: SubtaskResult, raw_texts: dict[str, str]
) -> CheckReport:
    """D2 — every `evidence: quote` fact must quote text a tool actually returned.

    `raw_texts` maps source id → the text that tool call produced. This is the
    only check in the system that can catch a fabricated citation without
    asking a model's opinion, which is why the tool layer has to keep the raw
    text around at all.
    """
    report = CheckReport()
    haystacks = {k: normalize(v) for k, v in raw_texts.items()}
    joined = " ".join(haystacks.values())

    for fact in result.facts:
        if fact.evidence is not Evidence.QUOTE:
            continue
        quotes = [s for s in fact.sources if s.quote.strip()]
        if not quotes:
            report.rejected_fact_ids.append(fact.id)
            report.messages.append(f"{fact.id}: evidence=quote without a quote")
            continue

        for source in quotes:
            quote = normalize(source.quote)
            if len(quote) < MIN_QUOTE_CHARS:
                report.rejected_fact_ids.append(fact.id)
                report.messages.append(
                    f"{fact.id}: quote too short to be evidence ({source.quote!r})"
                )
                break
            # Prefer the text of the cited source; fall back to everything the
            # subtask retrieved, since a model may attribute to the wrong id.
            target = haystacks.get(source.id) or joined
            if partial_ratio(quote, target) < QUOTE_MATCH_THRESHOLD:
                report.rejected_fact_ids.append(fact.id)
                report.messages.append(
                    f"{fact.id}: quote not found in the retrieved text of {source.id}"
                )
                break
            if missing := unsupported_tokens(quote, target):
                report.rejected_fact_ids.append(fact.id)
                report.messages.append(
                    f"{fact.id}: quote contains {missing} — not in the retrieved text"
                )
                break

    report.passed = not report.rejected_fact_ids
    return report


def check_d3_computed(result: SubtaskResult) -> CheckReport:
    """D3 — `computed` needs either arithmetic that adds up, or a query + hits."""
    report = CheckReport()
    for fact in result.facts:
        if fact.evidence is not Evidence.COMPUTED:
            continue
        has_query = any(s.query.strip() and s.hits is not None for s in fact.sources)
        if fact.expression.strip():
            value = eval_expression(fact.expression)
            if value is None:
                if has_query:
                    # A metadata fact whose `expression` is prose, not arithmetic —
                    # "search_exact(ETCS)=0; search_exact(Funkmast)=0; …". The
                    # evidence for it is right there in query+hits, so rejecting
                    # it threw away a correct negative finding and left the
                    # subtask with nothing, which D8 then called unresolvable.
                    # The expression is dropped, not believed.
                    fact.expression = ""
                    report.flags.append(
                        f"{fact.id}: expression is not arithmetic — treated as a "
                        "metadata claim, backed by query and hit count"
                    )
                    continue
                report.rejected_fact_ids.append(fact.id)
                report.messages.append(f"{fact.id}: expression is not evaluable")
                continue
            claimed = _numbers_in(fact.claim)
            if claimed and not any(abs(value - c) <= max(0.01, abs(value) * 0.001) for c in claimed):
                report.rejected_fact_ids.append(fact.id)
                report.messages.append(
                    f"{fact.id}: expression evaluates to {value:g}, not stated in the claim"
                )
            continue
        if not has_query:
            report.rejected_fact_ids.append(fact.id)
            report.messages.append(
                f"{fact.id}: computed without an expression and without query+hits"
            )
    report.passed = not report.rejected_fact_ids
    return report


def check_d4_derived(result: SubtaskResult, known_fact_ids: set[str]) -> CheckReport:
    """D4 — `derived` must point at facts that exist."""
    report = CheckReport()
    available = known_fact_ids | result.fact_ids()
    for fact in result.facts:
        if fact.evidence is not Evidence.DERIVED:
            continue
        if not fact.derived_from:
            report.rejected_fact_ids.append(fact.id)
            report.messages.append(f"{fact.id}: derived without derived_from")
            continue
        missing = [ref for ref in fact.derived_from if ref not in available]
        if missing:
            report.rejected_fact_ids.append(fact.id)
            report.messages.append(
                f"{fact.id}: derived_from references unknown facts {missing}"
            )
    report.passed = not report.rejected_fact_ids
    return report


def check_d5_tool_use(result: SubtaskResult, tools_were_available: bool) -> CheckReport:
    """D5 — the answer has to come from the tools, not from the model's memory.

    Two ways to fail it. Not calling a tool at all is the obvious one. The other
    is subtler and was seen in a real run: twelve searches, three pages fetched,
    and then six facts all marked `model_knowledge` — the retrieval happened and
    the answer was written from memory anyway. The tool log makes both
    countable, which is why this is a check and not a request in a prompt.
    """
    report = CheckReport()
    if not tools_were_available:
        return report
    if result.self_check.tool_calls == 0:
        report.passed = False
        report.messages.append(
            "no tool was called although tools were available — "
            "the answer can only come from parametric knowledge"
        )
        return report
    if result.facts and all(
        fact.evidence in (Evidence.MODEL_KNOWLEDGE, Evidence.NONE)
        for fact in result.facts
    ):
        report.passed = False
        report.messages.append(
            f"{len(result.facts)} fact(s) were produced after "
            f"{result.self_check.tool_calls} tool call(s), and not one of them is "
            "backed by what the tools returned — cite the retrieved text, or "
            "record what is missing as a gap"
        )
    return report


def check_d6_narrative_markers(result: SubtaskResult) -> tuple[str, CheckReport]:
    """D6 — the narrative may only reference facts that exist.

    Returns the cleaned narrative: unknown markers are stripped rather than
    left to look like evidence.
    """
    report = CheckReport()
    known = result.fact_ids()
    unknown = {m for m in MARKER_RE.findall(result.narrative or "") if m not in known}
    cleaned = result.narrative or ""
    for marker in unknown:
        cleaned = cleaned.replace(f"[{marker}]", "")
        report.flags.append(f"unknown fact marker [{marker}] stripped from the narrative")
    if unknown:
        report.messages.append(
            f"narrative referenced {len(unknown)} fact(s) that do not exist"
        )
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip(), report


def check_d7_sources_restrict(result: SubtaskResult) -> CheckReport:
    """D7 — a subtask restricted to certain source types may not cite others."""
    report = CheckReport()
    allowed = set(result.sources_restrict or [])
    if not allowed:
        return report
    for fact in result.facts:
        offending = {s.type for s in fact.sources} - allowed
        if offending:
            report.rejected_fact_ids.append(fact.id)
            report.messages.append(
                f"{fact.id}: cites {sorted(offending)} although restricted to {sorted(allowed)}"
            )
    report.passed = not report.rejected_fact_ids
    return report


def check_d8_empty_result(result: SubtaskResult) -> CheckReport:
    """D8 — nothing survived and nothing was declared missing: unresolvable.

    The case this exists for: every fact rejected, no gaps written, and a
    confident narrative left standing on top of nothing.
    """
    report = CheckReport()
    if not result.facts and not result.gaps:
        report.passed = False
        report.messages.append(
            "no facts survived and no gap was declared — subtask is unresolvable"
        )
    return report


def check_d9_paragraph_markers(report_md: str) -> CheckReport:
    """D9 — every paragraph of the report carries at least one fact marker."""
    check = CheckReport()
    for para in [p.strip() for p in re.split(r"\n\s*\n", report_md or "") if p.strip()]:
        if para.startswith(("#", ">", "|", "- ", "* ", "1.")) or para.startswith("```"):
            continue   # headings, tables, lists and code carry their own markers
        if not MARKER_RE.search(para):
            check.flags.append(para[:120])
            check.messages.append("paragraph without a fact marker")
    check.passed = not check.flags
    return check


def run_all(
    result: SubtaskResult,
    *,
    raw_texts: dict[str, str],
    known_fact_ids: set[str],
    tools_were_available: bool,
) -> tuple[SubtaskResult, CheckReport]:
    """D2–D8 in order, returning the surviving result and the merged report.

    Rejected facts are dropped here, before the LLM critic ever sees them:
    what the checks threw out is not up for a second opinion.
    """
    report = CheckReport()
    for check in (
        check_d2_quote_grounding(result, raw_texts),
        check_d3_computed(result),
        check_d4_derived(result, known_fact_ids),
        check_d7_sources_restrict(result),
        check_d5_tool_use(result, tools_were_available),
    ):
        report = report.merge(check)

    survivors = [f for f in result.facts if f.id not in report.rejected_fact_ids]
    result = result.model_copy(update={"facts": survivors})

    narrative, d6 = check_d6_narrative_markers(result)
    report = report.merge(d6)
    result = result.model_copy(update={"narrative": narrative})

    d8 = check_d8_empty_result(result)
    report = report.merge(d8)
    if not d8.passed:
        result = result.model_copy(update={"status": SubtaskStatus.UNRESOLVABLE})

    return result, report
