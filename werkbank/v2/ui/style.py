"""Werkbank v2 — shared styling.

Follows the house rules in CLAUDE.md, which the v1 board predates:

- **Colour encodes state, not identity.** v1 gave every status its own hue —
  eight badges in eight colours, so nothing stood out and the one status that
  actually wants the user ("to review") looked like the seven that do not.
  Here: neutral by default, accent while something is running, warn only for
  "you have to do something", and a muted red for a genuine failure.
- **Theme tokens, never hex.** The board follows light/dark like the rest.
- **Sentence case**, no uppercase labels, no coloured icons.
"""

from __future__ import annotations

from nicegui import ui

from werkbank.v2.models import CriterionVerdict, SubtaskStatus

BOARD_CSS = """
<style>
.wb-card {
  background: var(--c-surface); border: 1px solid var(--c-border);
  border-radius: 8px; padding: 10px 12px; min-width: 0;
}
.wb-card + .wb-card { margin-top: 8px; }
.wb-card:hover { border-color: var(--c-border-strong); }
/* The one subtask currently working. Accent = active, the same meaning it has
   everywhere else in the app. */
.wb-card.is-running { box-shadow: inset 3px 0 0 0 var(--c-accent); }
.wb-card.is-attention { box-shadow: inset 3px 0 0 0 var(--c-warn); }

.wb-pill {
  display: inline-flex; align-items: center; gap: 4px;
  font-size: 0.68rem; padding: 1px 8px; border-radius: 999px;
  border: 1px solid var(--c-border-strong); color: var(--c-text-2);
  white-space: nowrap;
}
.wb-pill.is-running { border-color: var(--c-accent); color: var(--c-accent); }
.wb-pill.is-attention { border-color: var(--c-warn); color: var(--c-warn); }
.wb-pill.is-muted { color: var(--c-text-muted); }

.wb-meta { font-size: 0.72rem; color: var(--c-text-muted); }
.wb-question {
  font-size: 0.85rem; color: var(--c-text); min-width: 0;
  overflow-wrap: anywhere;
}
.wb-agent { font-family: ui-monospace, monospace; font-size: 0.7rem; }

/* A fact marker in the report: a footnote, not a button. */
.wb-marker {
  font-family: ui-monospace, monospace; font-size: 0.72rem;
  color: var(--c-accent); cursor: pointer; text-decoration: none;
  border-bottom: 1px dotted var(--c-accent);
}
.wb-marker:hover { background: var(--c-surface-2); }

.wb-report { color: var(--c-text-2); font-size: 0.9rem; line-height: 1.65;
             max-width: 100%; min-width: 0; overflow-wrap: anywhere; }
.wb-report h1, .wb-report h2, .wb-report h3, .wb-report h4 {
             color: var(--c-text); font-weight: 600; margin: 1rem 0 .4rem 0; }
.wb-report h1 { font-size: 1.2rem; } .wb-report h2 { font-size: 1.05rem; }
/* h3/h4 need an explicit size too: the source list is built from h3s, and at
   browser default they dwarf the report they belong to. */
.wb-report h3 { font-size: .95rem; } .wb-report h4 { font-size: .875rem; }
.wb-report table { display: block; overflow-x: auto; width: max-content;
             max-width: 100%; border-collapse: collapse; font-size: .85em; }
.wb-report th { background: var(--c-border); color: var(--c-text-muted);
             padding: 4px 8px; text-align: left; }
.wb-report td { border-top: 1px solid var(--c-border); padding: 4px 8px; }
.wb-report td, .wb-report th { overflow-wrap: normal; min-width: 5rem; }
.wb-report blockquote { border-left: 3px solid var(--c-border-strong);
             padding-left: .75rem; color: var(--c-text-muted); margin: .6rem 0; }
.wb-report p { margin: 0 0 .6rem 0; }
.wb-report ul, .wb-report ol { padding-left: 1.4rem; margin: 0 0 .6rem 0; }
.wb-report code { background: var(--c-border); border-radius: 3px;
             padding: .1rem .3rem; font-family: ui-monospace, monospace; font-size: .85em; }
/* The generated block is the part of the report that is not up for
   negotiation — set apart so it reads as a finding, not as a closing remark. */
.wb-reflection {
  border-left: 3px solid var(--c-warn); background: var(--c-warn-bg);
  padding: .6rem .9rem; margin: 1rem 0; border-radius: 0 6px 6px 0;
}
.wb-reflection h2 { margin-top: 0; font-size: 1rem; }

.wb-scroll .q-scrollarea__content { max-width: 100%; }

@media (max-width: 700px) {
  .wb-hide-narrow { display: none !important; }
}
</style>
"""


def status_pill(status: SubtaskStatus, _) -> None:
    """One subtask's state. Only two states earn a colour."""
    labels = {
        SubtaskStatus.TODO: _("waiting"),
        SubtaskStatus.RUNNING: _("running"),
        SubtaskStatus.OK: _("done"),
        SubtaskStatus.PARTIAL: _("partial"),
        SubtaskStatus.UNRESOLVABLE: _("unanswered"),
    }
    # `partial` means "answered, with the holes named" — the outcome this whole
    # design is built to produce, so it is not marked as something to react to.
    # Only "nothing came back" is.
    tone = {
        SubtaskStatus.RUNNING: "is-running",
        SubtaskStatus.UNRESOLVABLE: "is-attention",
        SubtaskStatus.TODO: "is-muted",
    }.get(status, "")
    ui.html(
        f'<span class="wb-pill {tone}">{labels.get(status, status.value)}</span>',
        sanitize=False,
    )


def verdict_pill(
    verdict: CriterionVerdict, criterion: str, _, *, index: int | None = None
) -> None:
    """One acceptance criterion's verdict.

    Numbered, because a row of bare "met / not met" says nothing about *what*
    was met. The criterion itself is the tooltip, and the board prints it in
    full underneath when it was not met — which is the case worth reading.
    """
    labels = {
        CriterionVerdict.MET: _("met"),
        CriterionVerdict.PARTIAL: _("partly met"),
        CriterionVerdict.UNMET: _("not met"),
    }
    tone = "" if verdict is CriterionVerdict.MET else "is-attention"
    number = f"{index + 1}. " if index is not None else ""
    ui.html(
        f'<span class="wb-pill {tone}" title="{_escape(criterion)}">'
        f"{number}{labels[verdict]}</span>",
        sanitize=False,
    )


def _escape(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;").replace("<", "&lt;")
        .replace(">", "&gt;").replace('"', "&quot;")
    )
