"""DEPRECATED — v1 execution path, retired 2026-08-18.

Nothing imports this any more: `/werkbank` is `werkbank/v2/ui/page.py`, the chat
hand-off goes to `werkbank.v2.pipeline`, and `main.py` no longer starts the v1
scheduler. The files stay on disk for exactly one release because
`docs/werkbank-tasks.md` (Phase 7) conditions removal on v2 passing a complete
run on real data, and that run has not happened yet — the first real run *is*
the test. If it fails, re-adding two imports in `main.py` brings v1 back.

Delete this module once v2 has completed a run against the live archive.

werkbank/ui/module_page.py — KI-Tiefenrecherche overview page at /werkbank.
"""

from __future__ import annotations

from datetime import datetime, timezone

from nicegui import app as ng_app
from nicegui import ui

from app_ui.layout import page_layout, require_auth
from i18n import DEFAULT_LANG, get_translator
from services.session_auth import get_session_token
from werkbank import repository
from werkbank.models import TaskStatus

def _task_elapsed(task: repository.Task) -> str:
    if task.status not in (TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.PAUSED):
        return ""
    start = getattr(task, "started_at", None)
    if not start:
        return ""
    try:
        now = datetime.now(timezone.utc)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        secs = max(0, int((now - start).total_seconds()))
        h, rem = divmod(secs, 3600)
        m, s   = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
    except Exception:
        return ""


# Colour encodes state, never identity (see CLAUDE.md). Eight hues meant the
# one status that actually wants the user — "to review" — looked exactly like
# the seven that do not. Three tones now: neutral by default, accent while
# something is running, warn when the user has to act or something broke.
_TONE_NEUTRAL = "neutral"
_TONE_ACTIVE = "active"
_TONE_ATTENTION = "attention"

_STATUS_META: dict[TaskStatus, tuple[str, str]] = {
    TaskStatus.DRAFT:           ("Draft",     _TONE_NEUTRAL),
    TaskStatus.TRIAGE:          ("Triage",    _TONE_NEUTRAL),
    TaskStatus.QUEUED:          ("Queued",    _TONE_NEUTRAL),
    TaskStatus.RUNNING:         ("Running",   _TONE_ACTIVE),
    TaskStatus.PAUSED:          ("Paused",    _TONE_NEUTRAL),
    TaskStatus.AWAITING_REVIEW: ("To review", _TONE_ATTENTION),
    TaskStatus.COMPLETED:       ("Done",      _TONE_NEUTRAL),
    TaskStatus.FAILED:          ("Error",     _TONE_ATTENTION),
}

_PAGE_CSS = """
<style>
.wb-row {
  background: var(--c-surface); border: 1px solid var(--c-border);
  border-radius: 8px; transition: border-color .15s, background .15s;
}
.wb-row:hover { background: var(--c-surface-2); border-color: var(--c-border-strong); }
.wb-row.is-active { box-shadow: inset 3px 0 0 0 var(--c-accent); }
.wb-row.is-attention { box-shadow: inset 3px 0 0 0 var(--c-warn); }
.wb-status {
  display: inline-flex; align-items: center; font-size: .68rem;
  padding: 1px 8px; border-radius: 999px;
  border: 1px solid var(--c-border-strong); color: var(--c-text-2);
}
.wb-status.active { border-color: var(--c-accent); color: var(--c-accent); }
.wb-status.attention { border-color: var(--c-warn); color: var(--c-warn); }
.wb-colhead { font-size: .7rem; color: var(--c-text-muted); }
.wb-goal { color: var(--c-text); font-size: .875rem; min-width: 0; }
@media (max-width: 700px) { .wb-hide-narrow { display: none !important; } }
</style>
"""


def _available_models(user_id: str = "", token: str = "") -> list[str]:
    """Ordered model names from registry, falling back to a default model."""
    if user_id and token:
        try:
            from services.model_registry import model_names
            names = model_names(user_id, token)
            if names:
                return names
        except Exception:
            pass

    return ["claude-sonnet-4-6"]


def _open_new_task_dialog(user_id: str, token: str, table_refresh) -> None:
    _ = get_translator()
    models = _available_models(user_id, token)

    with ui.dialog() as dlg:
        with ui.card().classes("bg-gray-800").style("width:min(92vw,520px)"):
            ui.label(_("New task")).classes("text-lg font-bold text-gray-100 mb-1")

            goal = ui.textarea(
                label=_("Task"),
                placeholder=_("What should be researched, analyzed or created?"),
            ).classes("w-full").style("min-height:110px")

            model_sel = ui.select(
                label=_("Model"),
                options=models,
                value=models[0],
            ).classes("w-full mt-2").props("dark")

            async def _create():
                if not goal.value.strip():
                    ui.notify(_("Task must not be empty."), type="warning")
                    return
                task = repository.create_task(
                    user_id, goal.value.strip(), model_sel.value,
                    language=ng_app.storage.user.get("language", DEFAULT_LANG),
                )
                dlg.close()
                table_refresh.refresh()
                from werkbank.ui.task_dialog import open_task_dialog
                open_task_dialog(task.id, user_id, token, table_refresh)

            with ui.row().classes("w-full justify-end gap-2 mt-4"):
                ui.button(_("Cancel"), on_click=dlg.close).props("flat dark").classes("text-gray-400")
                ui.button(_("Create"), icon="add", on_click=_create).props(
                    "unelevated dark"
                ).classes("bg-purple-700 text-white")

    dlg.open()


@ui.page("/werkbank")
def werkbank_page() -> None:
    if not require_auth():
        return
    page_layout()
    _ = get_translator()
    ui.add_head_html(_PAGE_CSS)

    user_id: str = ng_app.storage.user.get("paperless_user", "")
    token: str   = get_session_token()

    from werkbank.archetypes import seed_defaults_if_needed
    seed_defaults_if_needed(user_id)

    # Paused while a task dialog is open so the refresh doesn't destroy the dialog.
    _dlg_open = [False]

    @ui.refreshable
    def task_table() -> None:
        tasks = repository.get_tasks_for_user(user_id)

        if not tasks:
            with ui.column().classes("w-full items-center gap-2 mt-10"):
                ui.icon("auto_awesome", size="32px").style("color:var(--c-text-muted)")
                ui.label(_("No tasks yet. Create your first one with \"New task\".")).classes(
                    "text-sm"
                ).style("color:var(--c-text-muted)")
            return

        _status_labels = {
            TaskStatus.DRAFT:           _("Draft"),
            TaskStatus.TRIAGE:          _("Triage"),
            TaskStatus.QUEUED:          _("Queued"),
            TaskStatus.RUNNING:         _("Running"),
            TaskStatus.PAUSED:          _("Paused"),
            TaskStatus.AWAITING_REVIEW: _("To review"),
            TaskStatus.COMPLETED:       _("Done"),
            TaskStatus.FAILED:          _("Error"),
        }
        with ui.column().classes("w-full gap-2"):
            for task in tasks:
                _meta_label, color = _STATUS_META.get(task.status, ("?", "gray"))
                label = _status_labels.get(task.status, _meta_label)
                goal_text = (task.original_request or "")
                goal_text = goal_text if len(goal_text) <= 100 else goal_text[:99] + "…"
                date_str  = task.created_at.strftime("%d.%m.%Y")
                elapsed   = _task_elapsed(task)

                row_tone = (
                    "is-active" if color == _TONE_ACTIVE
                    else "is-attention" if color == _TONE_ATTENTION else ""
                )
                with ui.row().classes(
                    f"w-full items-center px-4 py-3 gap-3 cursor-pointer wb-row {row_tone}"
                ):
                    ui.label(goal_text).classes("flex-1 wb-goal w-full").style(
                        "overflow:hidden;white-space:nowrap;text-overflow:ellipsis"
                    )
                    ui.label(date_str).classes("w-24 wb-colhead wb-hide-narrow")
                    with ui.column().classes("items-start gap-0 flex-shrink-0"):
                        ui.html(f'<span class="wb-status {color}">{label}</span>',
                                sanitize=False)
                        if elapsed:
                            ui.label(elapsed).classes("text-xs font-mono").style(
                                "color:var(--c-text-muted)")

                    def _open(tid=task.id):
                        from werkbank.ui.task_dialog import open_task_dialog
                        _dlg_open[0] = True
                        def _on_dlg_close():
                            _dlg_open[0] = False
                            task_table.refresh()
                        open_task_dialog(tid, user_id, token, task_table, on_close=_on_dlg_close)

                    ui.button(icon="open_in_new", on_click=_open).props(
                        "flat dark dense"
                    ).classes("text-gray-400")

    with ui.column().classes("w-full max-w-5xl mx-auto px-4 py-6"):
        # ── Header ────────────────────────────────────────────────────
        with ui.row().classes("w-full items-center justify-between mb-5"):
            with ui.row().classes("items-center gap-2"):
                ui.icon("auto_awesome", size="md").style("color:var(--c-text-muted)")
                ui.label(_("AI deep research")).classes("text-2xl font-bold").style(
                    "color:var(--c-text)")

            with ui.row().classes("gap-2"):
                def _open_archetypes():
                    from werkbank.ui.archetype_dialog import open_archetype_dialog
                    open_archetype_dialog(user_id)

                ui.button(_("Agents"), icon="groups", on_click=_open_archetypes).props(
                    "flat dark"
                ).style("color:var(--c-text-2)")
                ui.button(
                    _("New task"), icon="add",
                    on_click=lambda: _open_new_task_dialog(user_id, token, task_table),
                ).props("unelevated dark color=purple")

        # ── Column headers ────────────────────────────────────────────
        with ui.row().classes("w-full px-4 py-1 wb-colhead"):
            ui.label(_("Task")).classes("flex-1")
            ui.label(_("Created")).classes("w-24 wb-hide-narrow")
            ui.label(_("Status")).classes("flex-shrink-0")
            ui.label("").classes("w-10")

        task_table()

    ui.timer(2.5, lambda: None if _dlg_open[0] else task_table.refresh())
