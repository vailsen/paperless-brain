"""Werkbank v2 — the confirmation step. No model call, pure UI.

This is the cheapest place in the whole pipeline to catch a wrong run, which is
why `assumptions` sits at the top and not in a details pane: a reformulation
narrows a task silently, and that is the most common way a run answers the
wrong question perfectly. The user sees the reading the model chose *before*
any work is paid for.

`original_request` is displayed but never editable here — editing it would mean
the run is measured against something the user did not type, and both critics
are held against exactly that text.
"""

from __future__ import annotations

from nicegui import ui

from i18n import get_translator
from werkbank.v2.briefer import criterion_problem
from werkbank.v2.models import DEPTH_BUDGETS, Brief, DepthBudget


def budget_label(budget: DepthBudget, _) -> str:
    """The consequences, not the name — 'deep' means nothing on its own."""
    cfg = DEPTH_BUDGETS[budget]
    revisions = (
        _("no revision") if cfg.max_revisions == 0
        else _("1 revision") if cfg.max_revisions == 1
        else _("{n} revisions").format(n=cfg.max_revisions)
    )
    return _("{name} — max. {subtasks} subtasks, {revisions}").format(
        name=budget.value, subtasks=cfg.max_subtasks, revisions=revisions
    )


class BriefDialog:
    """Shows a brief for confirmation. `result` is the edited Brief, or None."""

    def __init__(self, brief: Brief) -> None:
        self._ = get_translator()
        self.brief = brief.model_copy(deep=True)
        self.dialog: ui.dialog | None = None

    # ── list editing ─────────────────────────────────────────────────────────
    def _edit_list(
        self, values: list[str], *, placeholder: str, warn_vague: bool = False
    ) -> None:
        """One editable row per entry, each removable. Refreshes in place."""
        _ = self._

        @ui.refreshable
        def rows() -> None:
            for index, value in enumerate(values):
                with ui.row().classes("w-full items-start gap-2 no-wrap"):
                    field = ui.textarea(value=value).props("outlined dense autogrow").classes(
                        "flex-1 min-w-0"
                    )
                    field.on(
                        "blur",
                        lambda _e=None, i=index, f=field: (
                            values.__setitem__(i, f.value or ""), rows.refresh()
                        ),
                    )
                    ui.button(
                        icon="close",
                        on_click=lambda i=index: (values.pop(i), rows.refresh()),
                    ).props("flat dense round size=sm").classes("card-action-btn")
                if warn_vague and (problem := criterion_problem(value)):
                    # Shown, not enforced: the user may know better than the
                    # heuristic, and a blocked dialog over a phrasing is worse
                    # than a warning they can overrule.
                    ui.label(_("Not checkable — {problem}").format(problem=problem)).classes(
                        "text-xs ml-1 mb-1"
                    ).style("color:var(--c-warn)")
            ui.button(
                _("Add"), icon="add",
                on_click=lambda: (values.append(""), rows.refresh()),
            ).props("flat dense size=sm")

        rows()

    def build(self) -> ui.dialog:
        _ = self._
        with ui.dialog().props("persistent") as dialog, ui.card().style(
            "background:var(--c-surface); width:min(96vw, 780px); max-height:90vh; overflow-y:auto;"
        ):
            self.dialog = dialog
            ui.label(_("Confirm the assignment")).classes("text-lg font-semibold").style(
                "color:var(--c-text)"
            )

            ui.label(_("Your request")).classes("text-xs mt-2").style(
                "color:var(--c-text-muted)"
            )
            ui.label(self.brief.original_request).classes("text-sm").style(
                "color:var(--c-text-2); white-space:pre-wrap"
            )

            # Assumptions first: this is where a silently narrowed task shows.
            ui.label(_("Assumptions the AI made")).classes(
                "text-sm font-semibold mt-4"
            ).style("color:var(--c-text)")
            ui.label(
                _("Your request left these points open, so the AI decided them. "
                  "Correct anything that is wrong — otherwise the run researches "
                  "the wrong thing and still looks right.")
            ).classes("text-xs mb-1").style("color:var(--c-text-muted)")
            self._edit_list(self.brief.assumptions, placeholder=_("Assumption"))

            ui.label(_("Acceptance criteria")).classes(
                "text-sm font-semibold mt-4"
            ).style("color:var(--c-text)")
            ui.label(
                _("The report has to fulfil these. Write them so that you can "
                  "tell from the finished report whether each one was met.")
            ).classes("text-xs mb-1").style("color:var(--c-text-muted)")
            self._edit_list(
                self.brief.acceptance_criteria, placeholder=_("Criterion"), warn_vague=True
            )

            with ui.expansion(_("Goal, format, out of scope")).classes(
                "w-full mt-3"
            ) as details:
                # Autogrow measures the field when it is built, and inside a
                # collapsed expansion that is zero — so every field stayed one
                # line tall no matter how long its text. Quasar only re-measures
                # on input, so opening the section has to nudge it. (`rows` does
                # not help: autogrow overrides it.)
                details.on("show", lambda: ui.run_javascript(f"""
                    const box = getHtmlElement({details.id});
                    if (!box) return;
                    requestAnimationFrame(() => box.querySelectorAll('textarea')
                        .forEach(t => t.dispatchEvent(
                            new Event('input', {{bubbles: true}}))));
                """))
                goal = ui.textarea(_("Goal"), value=self.brief.goal).props(
                    "outlined dense autogrow"
                ).classes("w-full")
                goal.on("blur", lambda: setattr(self.brief, "goal", goal.value or ""))
                fmt = ui.textarea(
                    _("Deliverable format"), value=self.brief.deliverable_format
                ).props("outlined dense autogrow").classes("w-full")
                fmt.on("blur", lambda: setattr(
                    self.brief, "deliverable_format", fmt.value or ""))
                ui.label(_("Out of scope")).classes("text-xs mt-2").style(
                    "color:var(--c-text-muted)"
                )
                self._edit_list(self.brief.out_of_scope, placeholder=_("Not part of the task"))

            ui.select(
                {b: budget_label(b, _) for b in DepthBudget},
                value=self.brief.depth_budget,
                label=_("Depth"),
                on_change=lambda e: setattr(self.brief, "depth_budget", e.value),
            ).props("outlined dense").classes("w-full mt-3")

            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button(_("Cancel"), on_click=lambda: dialog.submit(None)).props("flat dense")
                ui.button(_("Start"), on_click=self._submit).props(
                    "unelevated dense color=purple"
                )
        return dialog

    def _submit(self) -> None:
        # Drop the empty rows an "Add" click leaves behind rather than shipping
        # blank criteria into the plan.
        self.brief.assumptions = [a.strip() for a in self.brief.assumptions if a.strip()]
        self.brief.acceptance_criteria = [
            c.strip() for c in self.brief.acceptance_criteria if c.strip()
        ]
        self.brief.out_of_scope = [o.strip() for o in self.brief.out_of_scope if o.strip()]
        self.dialog.submit(self.brief)


async def confirm_brief(brief: Brief, host=None) -> Brief | None:
    """Show the brief, wait for the user. None when they cancelled.

    `host` is a page-level element to build the dialog in. Without one the
    dialog belongs to whatever slot happened to be open at the call site — and
    when that is a refreshable list on a poll timer, the dialog is deleted a few
    seconds after it appears, mid-sentence.
    """
    if host is not None:
        with host:
            dialog = BriefDialog(brief)
            result = await dialog.build()
    else:
        dialog = BriefDialog(brief)
        result = await dialog.build()
    dialog.dialog.delete()
    return result
