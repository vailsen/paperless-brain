"""werkbank/ui/module_page.py — KI-Tiefenrecherche overview page at /werkbank."""

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


_STATUS_META: dict[TaskStatus, tuple[str, str]] = {
    TaskStatus.DRAFT:           ("Draft",     "gray"),
    TaskStatus.TRIAGE:          ("Triage",    "yellow"),
    TaskStatus.QUEUED:          ("Queued",    "blue"),
    TaskStatus.RUNNING:         ("Running",   "purple"),
    TaskStatus.PAUSED:          ("Paused",    "orange"),
    TaskStatus.AWAITING_REVIEW: ("To review", "amber"),
    TaskStatus.COMPLETED:       ("Done",      "green"),
    TaskStatus.FAILED:          ("Error",     "red"),
}


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
            ui.label(_("No tasks yet. Create your first one with \"New task\".")).classes(
                "text-gray-500 text-sm mt-6"
            )
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

                with ui.row().classes(
                    "w-full items-center px-4 py-3 rounded-lg bg-gray-800 "
                    "hover:bg-gray-750 cursor-pointer gap-3"
                ):
                    ui.label(goal_text).classes("flex-1 text-sm text-gray-200").style(
                        "overflow:hidden;white-space:nowrap;text-overflow:ellipsis"
                    )
                    ui.label(date_str).classes("w-28 text-xs text-gray-500 mobile-hidden")
                    with ui.column().classes("w-28 items-start gap-0"):
                        ui.badge(label, color=color)
                        if elapsed:
                            ui.label(elapsed).classes("text-xs font-mono text-purple-400")

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
                ui.icon("auto_awesome", size="md").classes("text-purple-400")
                ui.label(_("AI deep research")).classes("text-2xl font-bold text-gray-100")

            with ui.row().classes("gap-2"):
                def _open_archetypes():
                    from werkbank.ui.archetype_dialog import open_archetype_dialog
                    open_archetype_dialog(user_id)

                ui.button(_("Agents"), icon="psychology", on_click=_open_archetypes).props(
                    "flat dark"
                ).classes("text-gray-300")
                ui.button(
                    _("New task"), icon="add",
                    on_click=lambda: _open_new_task_dialog(user_id, token, task_table),
                ).props("unelevated dark").classes("bg-purple-700 text-white")

        # ── Column headers ────────────────────────────────────────────
        with ui.row().classes(
            "w-full px-4 py-1 text-xs text-gray-500 uppercase tracking-wide"
        ):
            ui.label(_("Task")).classes("flex-1")
            ui.label(_("Created")).classes("w-28 mobile-hidden")
            ui.label(_("Status / time")).classes("w-28")
            ui.label("").classes("w-10")

        task_table()

    ui.timer(2.5, lambda: None if _dlg_open[0] else task_table.refresh())
