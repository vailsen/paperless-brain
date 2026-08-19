"""Werkbank v2 — the run board and the report view.

Two things the v1 board could not show, both of which are the point of v2:

- **why a subtask ended the way it did** — which agent, which revision, which
  acceptance criterion the critic did not see satisfied;
- **where a sentence in the report comes from** — every `[st3.f1]` is
  clickable and opens the fact behind it, with its evidence, its trust level
  and the verbatim quote.

A report you cannot interrogate is a report you have to take on faith, which is
the thing this whole rebuild exists to avoid.
"""

from __future__ import annotations

import html
import re

from nicegui import ui

from i18n import get_translator
from werkbank.v2 import reflection
from werkbank.v2.models import (
    MARKER_RE,
    CriterionVerdict,
    Fact,
    RunState,
    SubtaskStatus,
)
from werkbank.v2.ui.style import BOARD_CSS, status_pill, verdict_pill

# Fact dialogs opened on top of the board. The board refreshes itself while a
# run is live, and a refresh destroys everything built in its slot — including
# a dialog opened from it. The page reads this to hold the refresh back.
_open_facts: set[int] = set()


def dialogs_open() -> bool:
    return bool(_open_facts)


TRUST_WORDS = {
    "authoritative": "Dokument",
    "user_asserted": "eigene Angabe",
    "external": "Web",
    "computed": "berechnet",
    "derived": "abgeleitet",
    "model": "ohne Quelle",
}


EVIDENCE_WORDS = {
    "quote": "wörtlich belegt",
    "computed": "aus Treffern/Rechnung",
    "derived": "aus anderen Fakten",
    "model_knowledge": "Allgemeinwissen",
    "none": "ohne Beleg",
}


def fact_dialog(fact: Fact, state: RunState) -> None:
    """What a marker in the report actually stands on.

    Laid out as an answer to one question — *why should I believe this?* — so
    each block is labelled. The earlier version printed the raw enum values and
    a row of identical "ohne Quelle" chips above a list of the word "fact",
    which is a description of the data structure, not of the evidence.
    """
    _ = get_translator()
    with ui.dialog() as dialog, ui.card().style(
        "background:var(--c-surface); width:min(94vw, 620px); max-height:85vh; overflow-y:auto"
    ):
        ui.label(_("Where this comes from")).classes("text-base font-semibold").style(
            "color:var(--c-text)"
        )
        ui.label(fact.id).classes("wb-agent").style("color:var(--c-text-muted)")
        ui.label(fact.claim).classes("text-sm mt-1").style(
            "color:var(--c-text); white-space:pre-wrap"
        )

        evidence = EVIDENCE_WORDS.get(fact.evidence.value, fact.evidence.value)
        ui.label(_("Backed by: {kind}").format(kind=_(evidence))).classes(
            "wb-meta mt-3"
        )

        # A derived fact stands on other facts, not on sources of its own. Its
        # "sources" are placeholders, and showing them as if they were documents
        # is the confusing part.
        if fact.derived_from:
            ui.label(_("Built on these facts")).classes("text-sm font-semibold mt-3").style(
                "color:var(--c-text)"
            )
            for ref in fact.derived_from:
                parent = state.fact_by_id(ref)
                with ui.row().classes("w-full items-start gap-2 no-wrap mt-1"):
                    ui.label(ref).classes("wb-agent").style("color:var(--c-accent)")
                    ui.label(
                        parent.claim if parent else _("(no longer available)")
                    ).classes("text-xs flex-1 min-w-0").style(
                        "color:var(--c-text-2); overflow-wrap:anywhere"
                    )

        real_sources = [s for s in fact.sources if s.ref or s.query or s.quote]
        if real_sources:
            ui.label(_("Sources")).classes("text-sm font-semibold mt-3").style(
                "color:var(--c-text)"
            )
        for source in real_sources:
            word = TRUST_WORDS.get(source.trust.value, source.trust.value)
            with ui.column().classes("w-full gap-1 mt-2"):
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    ui.html(f'<span class="wb-pill">{_(word)}</span>', sanitize=False)
                    if source.hits is not None:
                        ui.label(_("{n} hits").format(n=source.hits)).classes("wb-meta")
                if source.query:
                    ui.label(_("Query: {q}").format(q=source.query)).classes(
                        "wb-meta w-full"
                    ).style("overflow-wrap:anywhere")
                if source.ref:
                    ui.label(source.ref).classes("wb-meta w-full").style(
                        "overflow-wrap:anywhere"
                    )
                if source.quote:
                    ui.label(f"«{source.quote}»").classes("text-xs").style(
                        "color:var(--c-text-2); border-left:2px solid var(--c-border-strong);"
                        "padding-left:8px; white-space:pre-wrap"
                    )
                if source.ref.startswith("http"):
                    ui.button(
                        _("Open source"), icon="open_in_new", color=None,
                        on_click=lambda url=source.ref: ui.navigate.to(url, new_tab=True),
                    ).props("flat dense size=sm").style("color:var(--c-text-2)")

        if fact.contradicts:
            ui.label(
                _("Contradicts: {ids}").format(ids=", ".join(fact.contradicts))
            ).classes("wb-meta mt-2").style("color:var(--c-warn)")

        ui.button(_("Close"), color=None, on_click=dialog.close).props(
            "flat dense"
        ).classes("mt-3").style("color:var(--c-text-2)")

    _open_facts.add(dialog.id)
    dialog.on("hide", lambda _e=None: _open_facts.discard(dialog.id))
    dialog.open()


MD_EXTRAS = ["fenced-code-blocks", "tables", "cuddled-lists", "break-on-newline"]


def _to_html(markdown_text: str) -> str:
    from nicegui.elements.markdown import prepare_content

    return prepare_content(markdown_text, extras=" ".join(MD_EXTRAS))


def _linkify(html_text: str, known: set[str]) -> str:
    """Turn every fact marker in rendered HTML into a clickable anchor.

    Done on the HTML rather than by splitting the markdown into pieces: one
    element per chunk turns a sentence into a stack of block elements, and the
    prose stops being prose. A marker whose fact does not exist is left as
    plain text — it is not a citation and must not look like one.
    """
    def swap(match: re.Match) -> str:
        fact_id = match.group(1)
        if fact_id not in known:
            return match.group(0)
        return f'<a class="wb-marker" data-fact="{fact_id}">[{fact_id}]</a>'

    return MARKER_RE.sub(swap, html_text)


def render_report(markdown: str, state: RunState) -> None:
    """The report, with every fact marker clickable.

    A report you cannot interrogate has to be taken on faith, which is the one
    thing this rebuild exists to avoid.
    """
    facts = {f.id: f for f in state.all_facts()}

    # The generated block is set apart so it reads as a finding rather than as
    # a closing remark. Its own markers are HTML comments and never render.
    block = reflection.extract(markdown)
    if block:
        before, _sep, after = markdown.partition(block)
        inner = block.replace(reflection.BEGIN, "").replace(reflection.END, "")
        html_text = (
            _to_html(before)
            + f'<div class="wb-reflection">{_to_html(inner)}</div>'
            + _to_html(after)
        )
    else:
        html_text = _to_html(markdown)

    element = ui.html(_linkify(html_text, set(facts)), sanitize=False).classes(
        "w-full wb-report"
    )
    # One delegated listener rather than one per marker: a long report can
    # carry a hundred of them.
    element.on("fact_click", lambda e: _open_fact(e.args, facts, state))
    ui.run_javascript(f"""
        (function() {{
            const el = getHtmlElement({element.id});
            if (!el || el.dataset.wbBound) return;
            el.dataset.wbBound = '1';
            el.addEventListener('click', function(ev) {{
                const marker = ev.target.closest('.wb-marker');
                if (marker) emitEvent('wb_fact_{element.id}', marker.dataset.fact);
            }});
        }})();
    """)
    ui.on(f"wb_fact_{element.id}", lambda e: _open_fact(e.args, facts, state))


def _open_fact(fact_id, facts: dict[str, Fact], state: RunState) -> None:
    if isinstance(fact_id, list):
        fact_id = fact_id[0] if fact_id else ""
    fact = facts.get(str(fact_id))
    if fact is not None:
        fact_dialog(fact, state)


def _split_markers(text: str) -> list[tuple[str, str]]:
    """Text broken at fact markers: [(text before, marker), …, (tail, "")]."""
    out: list[tuple[str, str]] = []
    pos = 0
    for match in MARKER_RE.finditer(text):
        out.append((text[pos:match.start()], match.group(1)))
        pos = match.end()
    out.append((text[pos:], ""))
    return out


def subtask_card(state: RunState, subtask_id: str) -> None:
    """One subtask: agent, status, revision, and what the critic said."""
    _ = get_translator()
    plan = next((s for s in state.subtasks if s.subtask_id == subtask_id), None)
    result = state.results.get(subtask_id)
    verdict = state.verdicts.get(subtask_id)
    status = state.status_of(subtask_id)

    tone = ""
    if status is SubtaskStatus.RUNNING:
        tone = "is-running"
    elif status is SubtaskStatus.UNRESOLVABLE:
        tone = "is-attention"

    with ui.element("div").classes(f"wb-card w-full {tone}"):
        with ui.row().classes("w-full items-start gap-2 no-wrap"):
            with ui.column().classes("flex-1 gap-1 min-w-0"):
                ui.label((plan.question if plan else result.question if result else "")).classes(
                    "wb-question w-full"
                )
                with ui.row().classes("items-center gap-2 flex-wrap"):
                    ui.label(subtask_id).classes("wb-agent").style("color:var(--c-text-muted)")
                    ui.label((plan.agent if plan else result.agent if result else "")).classes(
                        "wb-agent"
                    ).style("color:var(--c-text-2)")
                    if result and result.revision:
                        ui.label(
                            _("revision {n}").format(n=result.revision)
                        ).classes("wb-meta")
                    if result and result.duration_s:
                        ui.label(f"{result.duration_s:.0f}s").classes("wb-meta wb-hide-narrow")
            status_pill(status, _)

        if subtask_id in state.capped_subtasks:
            ui.label(_("Revision limit reached — reported as unanswered.")).classes(
                "wb-meta mt-1"
            ).style("color:var(--c-warn)")

        if verdict and verdict.criteria:
            ui.label(_("Acceptance criteria")).classes("wb-meta mt-2")
            with ui.row().classes("items-center gap-1 flex-wrap"):
                for index, entry in enumerate(verdict.criteria):
                    verdict_pill(entry.verdict, entry.criterion, _, index=index)
            # The ones that were not met are the ones worth reading, so they are
            # not hidden in a tooltip a phone cannot show.
            for index, entry in enumerate(verdict.criteria):
                if entry.verdict is not CriterionVerdict.MET:
                    ui.label(f"{index + 1}. {entry.criterion}").classes(
                        "wb-meta w-full mt-1"
                    ).style("color:var(--c-text-2)")

        if result and result.gaps:
            with ui.column().classes("w-full gap-0 mt-2"):
                for gap in result.gaps:
                    ui.label(f"• {gap.question} ({gap.reason.value})").classes(
                        "wb-meta w-full"
                    )

        if result and result.facts:
            with ui.row().classes("items-center gap-1 mt-2 flex-wrap"):
                for fact in result.facts:
                    ui.html(f'<span class="wb-marker">[{fact.id}]</span>',
                            sanitize=False).on(
                        "click", lambda _e=None, f=fact: fact_dialog(f, state)
                    )


def board(state: RunState) -> None:
    """The whole run: progress, then one card per subtask in execution order."""
    _ = get_translator()
    ui.add_head_html(BOARD_CSS)

    done = sum(
        1 for r in state.results.values()
        if r.status in (SubtaskStatus.OK, SubtaskStatus.PARTIAL, SubtaskStatus.UNRESOLVABLE)
    )
    total = len(state.subtasks) or len(state.results)

    with ui.column().classes("w-full gap-2 min-w-0"):
        with ui.row().classes("w-full items-center gap-2 no-wrap"):
            ui.label(_("{done} of {total} subtasks").format(done=done, total=total)).classes(
                "wb-meta"
            )
            ui.linear_progress(
                value=(done / total) if total else 0.0, show_value=False
            ).props("rounded size=6px color=purple").classes("flex-1")

        for subtask in state.subtasks or []:
            subtask_card(state, subtask.subtask_id)
        # Results without a plan entry (a resumed run whose plan was not loaded)
        for subtask_id in state.results:
            if not any(s.subtask_id == subtask_id for s in state.subtasks):
                subtask_card(state, subtask_id)
