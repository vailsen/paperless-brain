"""Werkbank v2 — WRITER, plus the post-processing that keeps it honest.

The model writes the report. Code then does three things it cannot be trusted
with about its own output:

- **restores the reflection block** if it was shortened, reworded or dropped,
- **strips fact markers that point at nothing** (D6),
- **flags paragraphs with no marker at all** (D9) and feeds them back into the
  reflection, so an unsupported passage is named in the same document it
  appears in.

The source list is built from the facts rather than asked for: a model
enumerating its own sources omits the weak ones.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, Field

from werkbank.v2 import reflection
from werkbank.v2.checks import check_d9_paragraph_markers
from werkbank.v2.llm import LLMContext, PromptLog, call_structured
from werkbank.v2.models import MARKER_RE, RunState, SubtaskStatus
from werkbank.v2 import prompts

_log = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent / "prompts" / "writer.md"

TRUST_HEADINGS = {
    "authoritative": "Dokumente (authoritative)",
    "user_asserted": "Eigene Notizen, Mail, Kalender (user_asserted)",
    "external": "Web (external)",
    "computed": "Berechnet (computed)",
    "derived": "Abgeleitet (derived)",
    "model": "Ohne Quelle (model)",
}


class Report(BaseModel):
    title: str = ""
    markdown: str = Field(default="")


def facts_block(state: RunState) -> str:
    """Every accepted fact, with what backs it — the writer's only material."""
    lines = []
    for result in sorted(state.results.values(), key=lambda r: r.subtask_id):
        if not result.facts:
            continue
        lines.append(f"### {result.subtask_id} ({result.agent}): {result.question}")
        for fact in result.facts:
            trust = ", ".join(sorted({s.trust.value for s in fact.sources})) or "model"
            conflict = f" ⚠ widerspricht {', '.join(fact.contradicts)}" if fact.contradicts else ""
            lines.append(
                f"- [{fact.id}] ({fact.evidence.value}, trust: {trust}){conflict}\n"
                f"  {fact.claim}"
            )
        lines.append("")
    return "\n".join(lines) or "(keine Facts)"


def sources_section(state: RunState) -> str:
    """Built from the facts, not requested from the model.

    A model asked to list its sources lists the good ones. The weak ones are
    exactly what the reader needs in order to judge the report.
    """
    grouped: dict[str, list[str]] = {}
    for fact in state.all_facts():
        for source in fact.sources:
            entry = f"- `{fact.id}` — {source.type}: {source.ref or source.query or '—'}"
            if source.retrieved_at:
                entry += f" (abgerufen {source.retrieved_at[:10]})"
            grouped.setdefault(source.trust.value, []).append(entry)

    if not grouped:
        return ""
    out = ["## Quellenverzeichnis", ""]
    for trust in ("authoritative", "user_asserted", "external", "computed", "derived", "model"):
        if entries := grouped.get(trust):
            out += [f"### {TRUST_HEADINGS[trust]}", "", *sorted(set(entries)), ""]
    return "\n".join(out)


def _subtask_overview(state: RunState) -> str:
    rows = ["| Teilaufgabe | Agent | Status | Frage |", "|---|---|---|---|"]
    for result in sorted(state.results.values(), key=lambda r: r.subtask_id):
        rows.append(
            f"| {result.subtask_id} | {result.agent} | {result.status.value} | "
            f"{result.question.replace('|', '/')} |"
        )
    return "\n".join(rows)


def enforce(markdown: str, original_block: str, state: RunState) -> tuple[str, list[str]]:
    """Post-processing. Returns the corrected report and the flagged paragraphs.

    Order matters: markers are cleaned first, so a paragraph is not flagged for
    carrying only an invalid marker.
    """
    known = {f.id for f in state.all_facts()}
    for marker in set(MARKER_RE.findall(markdown)):
        if marker not in known:
            markdown = markdown.replace(f"[{marker}]", "")

    current = reflection.extract(markdown)
    if current != original_block:
        # Shortened, reworded or dropped. Restore it; the block is the one part
        # of the report that is not the model's to edit.
        _log.info("werkbank v2: reflection block was altered — restoring it")
        markdown = (
            markdown.replace(current, original_block)
            if current
            else f"{markdown.rstrip()}\n\n{original_block}\n"
        )

    body = markdown.replace(original_block, "")
    flagged = check_d9_paragraph_markers(body).flags
    return markdown, flagged


def _local(iso: str) -> str:
    """Timestamps are stored in UTC and read by a human in their own timezone.

    Handing the model the raw UTC string put a time two hours off into the
    report header all summer — and a report that misstates when it ran is a
    report whose other timestamps have to be doubted too.
    """
    from datetime import datetime, timezone

    from config.settings import local_tz

    try:
        stamp = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return (iso or "")[:19]
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(local_tz()).strftime("%Y-%m-%d %H:%M:%S")


async def write_report(
    state: RunState,
    ctx: LLMContext,
    *,
    prompt_log: PromptLog | None = None,
) -> str:
    """Facts + generated reflection → the finished report.

    The reflection is generated first and handed to the model, then verified
    afterwards. Both halves are needed: giving it makes the report coherent,
    checking it makes the honesty enforceable.
    """
    block = reflection.build(state)
    brief = state.brief
    duration = ""
    if state.started_at and state.finished_at:
        duration = f"{_local(state.started_at)} → {_local(state.finished_at)}"

    user = "\n\n".join([
        f"Auftraggeber: {state.user_id}\nModell: {state.model}\nLauf: {duration}",
        f"Originalauftrag (wörtlich):\n{brief.original_request if brief else ''}",
        f"Ziel: {brief.goal if brief else ''}\n"
        f"Format: {brief.deliverable_format if brief else ''}\n"
        f"Annahmen: {brief.assumptions if brief else []}\n"
        f"Abnahmekriterien: {brief.acceptance_criteria if brief else []}",
        f"Teilaufgaben:\n{_subtask_overview(state)}",
        f"Belegte Facts:\n{facts_block(state)}",
        f"Selbstreflexion — unverändert übernehmen:\n{block}",
    ])

    report = await call_structured(
        "writer", prompts.system_prompt("writer"), user, Report, ctx,
        prompt_log=prompt_log,
    )
    markdown, flagged = enforce(report.markdown, block, state)

    if flagged:
        # A paragraph without evidence has to appear in the same document it is
        # in — so the block is rebuilt with the flags and swapped in.
        state.flagged_paragraphs = flagged
        rebuilt = reflection.build(state)
        markdown = markdown.replace(block, rebuilt)
        block = rebuilt

    if sources := sources_section(state):
        markdown = f"{markdown.rstrip()}\n\n{sources}"
    return markdown


def has_unreported_holes(state: RunState) -> bool:
    """True when the run left something the reader must not miss."""
    return bool(
        state.capped_subtasks
        or any(r.status is SubtaskStatus.UNRESOLVABLE for r in state.results.values())
        or any(r.gaps for r in state.results.values())
    )
