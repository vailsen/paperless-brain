"""werkbank/ui/task_dialog.py — task detail dialog with full state machine UI."""

from __future__ import annotations

import asyncio
import re as _re
from datetime import datetime, timezone

from nicegui import app as ng_app
from nicegui import ui

from i18n import get_translator
from services.session_auth import get_session_token
from werkbank import repository
from werkbank.models import SubTaskStatus, TaskStatus

def _md_filename(task: repository.Task) -> str:
    raw = (task.short_title or f"auftrag_{task.id}")[:60]
    name = _re.sub(r"[^\w\s-]", "", raw).strip().replace(" ", "_")
    return f"{name or f'auftrag_{task.id}'}.md"


_SUB_ICON: dict[SubTaskStatus, tuple[str, str]] = {
    SubTaskStatus.TODO:    ("radio_button_unchecked", "text-gray-500"),
    SubTaskStatus.RUNNING: ("pending",                "text-purple-400"),
    SubTaskStatus.DONE:    ("check_circle",           "text-green-400"),
    SubTaskStatus.FAILED:  ("cancel",                 "text-red-400"),
}

_STATUS_BORDER: dict[SubTaskStatus, str] = {
    SubTaskStatus.TODO:    "#6b7280",
    SubTaskStatus.RUNNING: "#a855f7",
    SubTaskStatus.DONE:    "#22c55e",
    SubTaskStatus.FAILED:  "#ef4444",
}

_CARD_BG  = "background:#1f2937"
_COL_CARD = "background:#111827; border:1px solid #374151; border-radius:6px; padding:10px; min-height:80px"
_DLG_W    = "width:min(98vw,1100px);max-height:90vh;overflow-y:auto"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _elapsed_str(task: repository.Task) -> str:
    """Elapsed time since task was queued. Works for QUEUED/RUNNING/PAUSED."""
    if task.status not in (TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.PAUSED):
        return ""
    start = getattr(task, "started_at", None)
    if start is None:
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


def _truncate(text: str, n: int) -> str:
    text = text.strip()
    return text if len(text) <= n else text[:n - 1] + "…"


# ── Subtask detail (fills pre-created card) ───────────────────────────────────

def _fill_subtask_detail(
    card: ui.card,
    s: repository.SubTask,
    arch_name: str,
    close_fn,
    id_to_idx: dict | None = None,
) -> None:
    _ = get_translator()
    id_to_idx = id_to_idx or {}
    icon_name, icon_cls = _SUB_ICON.get(s.status, ("help", "text-gray-400"))
    card.clear()
    with card:
        with ui.row().classes("w-full items-center justify-between mb-2"):
            with ui.row().classes("items-center gap-2 flex-1 flex-wrap"):
                ui.icon(icon_name, size="sm").classes(icon_cls)
                ui.label(f"#{s.order_index + 1}").classes(
                    "text-xs text-gray-400 font-mono font-bold"
                )
                if arch_name:
                    ui.badge(arch_name, color="purple").classes("text-xs")
                if s.depends_on:
                    dep_labels = ", ".join(f"#{id_to_idx.get(d, d)}" for d in s.depends_on)
                    ui.badge(f"← {dep_labels}", color="blue").classes("text-xs")
            ui.button(icon="close", on_click=close_fn).props("flat dark dense").classes("text-gray-500")
        ui.separator()

        with ui.column().classes("w-full gap-2 pt-2"):
            ui.label(_("Instruction")).classes("text-xs text-gray-500 uppercase tracking-wide")
            ui.label(s.instruction).classes("text-sm text-gray-200 whitespace-pre-wrap")

            if s.success_criteria:
                ui.label(_("Success criterion")).classes(
                    "text-xs text-gray-500 uppercase tracking-wide mt-2"
                )
                ui.label(s.success_criteria).classes("text-sm text-gray-400")

            if s.retry_count > 0:
                ui.label(_("Attempts: {n}").format(n=s.retry_count + 1)).classes("text-xs text-orange-400 mt-1")

            if s.critic_verdict and s.critic_verdict != "":
                _passed = s.status == SubTaskStatus.DONE
                _sc_text = s.critic_verdict
                if _sc_text.strip().lower() == "ok":
                    _sc_text = _("Success criteria met.")
                if _passed:
                    _sc_label = _("Self-check passed")
                    _sc_icon, _sc_icon_cls, _sc_text_cls = "check_circle", "text-green-400", "text-green-300"
                else:
                    _sc_label = _("Self-check failed")
                    _sc_icon, _sc_icon_cls, _sc_text_cls = "error", "text-orange-400", "text-orange-300"
                with ui.row().classes("items-center gap-1 mt-2"):
                    ui.icon(_sc_icon, size="xs").classes(_sc_icon_cls)
                    ui.label(_sc_label).classes("text-xs text-gray-500 uppercase tracking-wide")
                ui.label(_truncate(_sc_text, 400)).classes(f"text-sm {_sc_text_cls}")

            if s.result_compacted:
                ui.separator()
                ui.label(_("Result (compacted)")).classes(
                    "text-xs text-gray-500 uppercase tracking-wide"
                )
                ui.markdown(s.result_compacted).classes("text-sm text-gray-300")

            if s.result_raw:
                with ui.expansion(_("Raw result")).classes("w-full text-gray-500 mt-1"):
                    ui.label(s.result_raw).classes(
                        "text-xs text-gray-500 whitespace-pre-wrap font-mono"
                    )


# ── Sub-task cards ────────────────────────────────────────────────────────────

def _synth_running_card() -> None:
    _ = get_translator()
    border = _STATUS_BORDER[SubTaskStatus.RUNNING]
    with ui.card().classes("w-full mb-1").style(
        f"{_CARD_BG}; border-left:3px solid {border}"
    ):
        with ui.column().classes("w-full gap-1"):
            with ui.row().classes("items-center gap-1"):
                ui.spinner(size="xs", color="purple")
                ui.label(_("Synthesis")).classes("text-xs font-mono font-bold text-purple-400")
            ui.label(_("Compiling result …")).classes("text-xs text-gray-400 leading-snug")


def _synth_done_card() -> None:
    _ = get_translator()
    border = _STATUS_BORDER[SubTaskStatus.DONE]
    with ui.card().classes("w-full mb-1").style(
        f"{_CARD_BG}; border-left:3px solid {border}"
    ):
        with ui.column().classes("w-full gap-1"):
            with ui.row().classes("items-center gap-1"):
                ui.icon("summarize", size="xs").classes("text-green-400 flex-shrink-0")
                ui.label(_("Synthesis")).classes("text-xs font-mono font-bold text-green-400")
            ui.label(_("Result compiled")).classes("text-xs text-gray-400 leading-snug")


def _subtask_card(
    s: repository.SubTask,
    arch_name: str = "",
    id_to_idx: dict | None = None,
    on_detail_click=None,
) -> None:
    _ = get_translator()
    icon_name, icon_cls = _SUB_ICON.get(s.status, ("help", "text-gray-400"))
    id_to_idx = id_to_idx or {}
    border = _STATUS_BORDER.get(s.status, "#6b7280")

    with ui.card().classes("w-full mb-1").style(
        f"{_CARD_BG}; border-left:3px solid {border}"
    ):
        with ui.column().classes("w-full gap-1"):
            # Header: icon + #N + agent badge + expand button
            with ui.row().classes("items-center gap-1 w-full"):
                ui.icon(icon_name, size="xs").classes(f"flex-shrink-0 {icon_cls}")
                ui.label(f"#{s.order_index + 1}").classes(
                    "text-xs font-mono font-bold text-gray-400 flex-shrink-0"
                )
                if arch_name:
                    ui.badge(arch_name, color="purple").classes("text-xs flex-shrink-0")
                ui.element("div").classes("flex-1")
                if on_detail_click:
                    ui.button(
                        icon="open_in_full",
                        on_click=lambda _s=s, _a=arch_name: on_detail_click(_s, _a),
                    ).props("flat dark dense").classes("text-gray-600 flex-shrink-0")

            # Instruction (truncated, word-wrap for long tokens)
            ui.label(_truncate(s.instruction, 85)).classes(
                "text-xs text-gray-200 leading-snug"
            ).style("word-break:break-word;overflow-wrap:break-word")

            # Dependencies
            if s.depends_on:
                dep_labels = ", ".join(f"#{id_to_idx.get(d, d)}" for d in s.depends_on)
                ui.label(f"← {dep_labels}").classes("text-xs text-blue-400 opacity-70")

            # Retry + error hint
            if s.retry_count > 0:
                ui.label(_("Attempt {n}").format(n=s.retry_count + 1)).classes("text-xs text-orange-400")
            if s.status == SubTaskStatus.FAILED and s.critic_verdict:
                ui.label(_truncate(s.critic_verdict, 55)).classes("text-xs text-red-400 italic")
            elif s.status == SubTaskStatus.DONE and s.critic_verdict:
                with ui.row().classes("items-center gap-1"):
                    ui.icon("check_circle", size="14px").classes("text-green-400 flex-shrink-0")
                    ui.label(_("Self-check passed")).classes("text-xs text-green-400 italic")


# ── Board column ──────────────────────────────────────────────────────────────

def _board_column(
    label: str,
    items: list[repository.SubTask],
    accent: str = "text-gray-400",
    arch_names: dict | None = None,
    id_to_idx: dict | None = None,
    on_detail_click=None,
    synth_running: bool = False,
    synth_done: bool = False,
) -> None:
    arch_names = arch_names or {}
    id_to_idx  = id_to_idx  or {}
    with ui.column().classes("flex-1 min-w-40").style(_COL_CARD):
        ui.label(label).classes(
            f"text-xs font-semibold uppercase tracking-wide {accent} mb-2"
        )
        for s in items:
            _subtask_card(
                s,
                arch_name=arch_names.get(s.archetype_id, ""),
                id_to_idx=id_to_idx,
                on_detail_click=on_detail_click,
            )
        if synth_running:
            _synth_running_card()
        if synth_done:
            _synth_done_card()
        if not items and not synth_running and not synth_done:
            ui.label("—").classes("text-xs text-gray-600")


# ── Readonly board ────────────────────────────────────────────────────────────

def _render_board_readonly(
    task: repository.Task,
    subtasks: list[repository.SubTask],
    on_detail_click=None,
) -> None:
    _ = get_translator()
    user_id    = ng_app.storage.user.get("paperless_user", "")
    arch_names = {a.id: a.name for a in repository.get_archetypes(user_id)}
    id_to_idx  = {s.id: s.order_index + 1 for s in subtasks}

    total = len(subtasks)
    # In a readonly board (post-synthesis), any lingering RUNNING subtask is effectively done.
    # Treat them as abgeschlossen and include synthesis in the count.
    todo    = [s for s in subtasks if s.status == SubTaskStatus.TODO]
    abg     = [s for s in subtasks if s.status != SubTaskStatus.TODO]  # DONE + FAILED + RUNNING
    done_display  = len(abg) + 1  # +1 synthesis
    total_display = total      + 1
    ui.label(_("{done}/{total} sub-tasks processed").format(done=done_display, total=total_display)).classes("text-xs text-gray-400 mb-2")

    with ui.row().classes("w-full gap-3 overflow-x-auto"):
        with ui.column().classes("flex-1 min-w-40").style(_COL_CARD):
            ui.label(_("Task")).classes(
                "text-xs font-semibold uppercase tracking-wide text-yellow-400 mb-2"
            )
            ui.label(
                task.refined_request or task.original_request or ""
            ).classes("text-xs text-gray-300").style(
                "word-break:break-word;white-space:pre-wrap"
            )
        _board_column(
            _("To-Do"), todo,
            arch_names=arch_names, id_to_idx=id_to_idx,
            on_detail_click=on_detail_click,
        )
        # No "Läuft" column in readonly: any lingering RUNNING subtask moved to abg
        _board_column(
            _("Completed"), abg, "text-green-400",
            arch_names=arch_names, id_to_idx=id_to_idx,
            on_detail_click=on_detail_click,
            synth_done=True,
        )


# ── State renders ─────────────────────────────────────────────────────────────

def _render_draft(task: repository.Task, dlg, table_refresh, content_refresh=None, title_refresh=None) -> None:
    _ = get_translator()
    user_id = ng_app.storage.user.get("paperless_user", "")

    ui.label(_("Task (editable)")).classes("text-xs text-gray-500 uppercase tracking-wide mb-1")
    text_area = ui.textarea(value=task.original_request).classes("w-full").style(
        "min-height:120px; background:#111827; color:#e5e7eb;"
    ).props("dark outlined")

    async def _start():
        token = get_session_token()
        edited = text_area.value.strip() or task.original_request
        if edited != task.original_request:
            repository.update_task_refined_request(task.id, user_id, "")
            with repository._conn() as conn:
                conn.execute(
                    "UPDATE agent_tasks SET original_request = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                    (edited, repository._now(), task.id, user_id),
                )
        repository.update_task_refined_request(task.id, user_id, "")
        repository.update_task_status(task.id, user_id, TaskStatus.TRIAGE)
        if content_refresh:
            content_refresh()
        try:
            from werkbank.llm_lane import create_llm
            from werkbank.roles import planner as planner_mod
            llm = create_llm(task.model, user_id, token)
            refined, title = await planner_mod.run(edited, llm=llm)
            repository.update_task_refined_request(task.id, user_id, refined)
            repository.update_task_title(task.id, user_id, title)
            if title_refresh:
                title_refresh()
        except Exception as exc:
            ui.notify(_("Planner error: {err}").format(err=exc), type="negative")
            repository.update_task_status(task.id, user_id, TaskStatus.DRAFT)
        finally:
            if content_refresh:
                content_refresh()
        if table_refresh:
            table_refresh.refresh()

    def _delete():
        repository.delete_task(task.id, user_id)
        dlg.close()
        if table_refresh:
            table_refresh.refresh()

    with ui.row().classes("gap-2 mt-3"):
        ui.button(_("Start"), icon="play_arrow", on_click=_start).props("unelevated dark").classes(
            "bg-purple-700 text-white"
        )
        ui.button(_("Delete"), icon="delete", on_click=_delete).props("flat dark").classes("text-red-500")


def _render_triage_loading() -> None:
    _ = get_translator()
    with ui.column().classes("items-center w-full py-8 gap-2"):
        ui.spinner(size="lg", color="purple")
        ui.label(_("Planner is rephrasing the task …")).classes("text-sm text-gray-400")


def _render_triage_ready(task: repository.Task, table_refresh) -> None:
    _ = get_translator()
    user_id = ng_app.storage.user.get("paperless_user", "")
    token   = get_session_token()

    ui.label(_("Revised task (editable)")).classes(
        "text-xs text-gray-500 uppercase tracking-wide mb-1"
    )
    text_area = ui.textarea(value=task.refined_request).classes("w-full").style(
        "min-height:120px; background:#111827; color:#e5e7eb;"
    ).props("dark outlined")

    def _confirm():
        refined = text_area.value.strip() or task.refined_request
        repository.update_task_refined_request(task.id, user_id, refined)
        from werkbank.scheduler import register_token
        register_token(task.id, token)
        repository.update_task_status(task.id, user_id, TaskStatus.QUEUED)
        if table_refresh:
            table_refresh.refresh()

    def _revert():
        repository.update_task_status(task.id, user_id, TaskStatus.DRAFT)
        if table_refresh:
            table_refresh.refresh()

    with ui.row().classes("gap-2 mt-3"):
        ui.button(_("Confirm & start"), icon="check", on_click=_confirm).props(
            "unelevated dark"
        ).classes("bg-green-700 text-white")
        ui.button(_("Discard"), icon="close", on_click=_revert).props("flat dark").classes("text-gray-400")


def _render_board(
    task: repository.Task,
    subtasks: list[repository.SubTask],
    dlg,
    table_refresh,
    on_detail_click=None,
) -> None:
    _ = get_translator()
    user_id    = ng_app.storage.user.get("paperless_user", "")
    arch_names = {a.id: a.name for a in repository.get_archetypes(user_id)}
    id_to_idx  = {s.id: s.order_index + 1 for s in subtasks}

    done_count   = sum(1 for s in subtasks if s.status in (SubTaskStatus.DONE, SubTaskStatus.FAILED))
    total        = len(subtasks)
    all_terminal = total > 0 and done_count == total

    # ── Controls ──────────────────────────────────────────────────────
    with ui.row().classes("w-full items-center gap-3 mb-3"):
        with ui.row().classes("flex-1 items-center gap-2"):
            status_icon = {
                TaskStatus.RUNNING: ("pending",      "text-purple-400"),
                TaskStatus.QUEUED:  ("schedule",     "text-blue-400"),
                TaskStatus.PAUSED:  ("pause_circle", "text-orange-400"),
            }.get(task.status, ("info", "text-gray-400"))
            ui.icon(status_icon[0], size="sm").classes(status_icon[1])

            if task.status == TaskStatus.RUNNING and all_terminal:
                ui.label(_("Synthesis running…")).classes("text-sm text-purple-300")
            elif task.status == TaskStatus.RUNNING and total > 0:
                ui.label(_("Sub-task {n} of {total}").format(n=done_count + 1, total=total)).classes("text-sm text-gray-300")
            elif task.status == TaskStatus.QUEUED:
                ui.label(_("Waiting to run …")).classes("text-sm text-gray-400")
            elif task.status == TaskStatus.PAUSED:
                ui.label(_("Paused")).classes("text-sm text-orange-400")

            elapsed = _elapsed_str(task)
            if elapsed:
                ui.label(elapsed).classes("text-xs text-purple-300 font-mono ml-2")

            if task.model:
                ui.label(task.model).classes(
                    "text-xs text-gray-500 font-mono ml-2 truncate max-w-32"
                ).tooltip(_("Model: {model}").format(model=task.model))

        if task.status in (TaskStatus.RUNNING, TaskStatus.QUEUED):
            _stop_btn: list = [None]
            def _stop():
                if _stop_btn[0]:
                    _stop_btn[0].props(add="disabled")
                from werkbank import orchestrator
                orchestrator.pause_task(task.id)
            _stop_btn[0] = ui.button(_("Stop"), icon="pause", on_click=_stop).props("flat dark").classes("text-orange-400")
        elif task.status == TaskStatus.PAUSED:
            def _resume():
                from werkbank import orchestrator
                orchestrator.resume_task(task.id, user_id)
                from werkbank.scheduler import register_token
                register_token(task.id, get_session_token())
                if table_refresh:
                    table_refresh.refresh()
            ui.button(_("Resume"), icon="play_arrow", on_click=_resume).props("flat dark").classes("text-green-400")

        def _delete():
            repository.delete_task(task.id, user_id)
            dlg.close()
            if table_refresh:
                table_refresh.refresh()
        ui.button(icon="delete", on_click=_delete).props("flat dark dense").classes("text-gray-600").tooltip(_("Delete task"))

    # ── Board columns ─────────────────────────────────────────────────
    todo    = [s for s in subtasks if s.status == SubTaskStatus.TODO]
    running = [s for s in subtasks if s.status == SubTaskStatus.RUNNING]
    done    = [s for s in subtasks if s.status in (SubTaskStatus.DONE, SubTaskStatus.FAILED)]
    synth_running = task.status == TaskStatus.RUNNING and all_terminal and total > 0

    with ui.row().classes("w-full gap-3 mt-1 overflow-x-auto"):
        with ui.column().classes("flex-1 min-w-40").style(_COL_CARD):
            ui.label(_("Task")).classes(
                "text-xs font-semibold uppercase tracking-wide text-yellow-400 mb-2"
            )
            ui.label(
                task.refined_request or task.original_request or ""
            ).classes("text-xs text-gray-300").style(
                "word-break:break-word;white-space:pre-wrap"
            )

        _board_column(
            _("To-Do"), todo,
            arch_names=arch_names, id_to_idx=id_to_idx,
            on_detail_click=on_detail_click,
        )
        _board_column(
            _("Running"), running, "text-purple-400",
            arch_names=arch_names, id_to_idx=id_to_idx,
            on_detail_click=on_detail_click,
            synth_running=synth_running,
        )
        _board_column(
            _("Completed"), done, "text-green-400",
            arch_names=arch_names, id_to_idx=id_to_idx,
            on_detail_click=on_detail_click,
        )


def _render_review(
    task: repository.Task,
    subtasks: list[repository.SubTask],
    dlg,
    table_refresh,
    token: str = "",
    on_detail_click=None,
) -> None:
    _ = get_translator()
    user_id = ng_app.storage.user.get("paperless_user", "")
    _token  = token or get_session_token()

    with ui.row().classes("items-center justify-between w-full mb-2"):
        ui.label(_("Review and approve result")).classes("text-sm font-semibold text-amber-400")
        _edit_mode = [False]
        _toggle_btn = ui.button(_("Edit"), icon="edit").props("flat dark dense").classes(
            "text-xs text-gray-500"
        )

    editor = ui.textarea(value=task.result_md or "").classes("w-full").style(
        "min-height:280px; font-family:monospace; background:#111827; color:#e5e7eb;"
    ).props("dark outlined")
    editor.set_visibility(False)

    preview = (
        ui.markdown(task.result_md or "", sanitize=False)
        .classes("w-full text-gray-200")
        .style(
            "min-height:280px; padding:12px 14px; border:1px solid #374151;"
            "border-radius:4px; overflow-y:auto; background:#111827;"
        )
    )

    def _toggle_edit():
        if not _edit_mode[0]:
            _edit_mode[0] = True
            preview.set_visibility(False)
            editor.set_visibility(True)
            _toggle_btn.set_text(_("Preview"))
            _toggle_btn.props("icon=visibility")
        else:
            _edit_mode[0] = False
            preview.set_content(editor.value)
            editor.set_visibility(False)
            preview.set_visibility(True)
            _toggle_btn.set_text(_("Edit"))
            _toggle_btn.props("icon=edit")

    _toggle_btn.on_click(_toggle_edit)

    async def _approve():
        edited = editor.value.strip()
        if edited != task.result_md:
            repository.update_task_result(task.id, user_id, edited)
        btn_approve.props(add="loading")
        try:
            from werkbank.export import export_to_paperless
            doc_id, doc_url = await export_to_paperless(task.id, user_id, token=_token)
            if doc_id:
                ui.notify(_("Saved to Paperless (#{id})").format(id=doc_id), type="positive")
            else:
                ui.notify(_("Uploaded — document ID not yet available."), type="info")
            if table_refresh:
                table_refresh.refresh()
        except Exception as exc:
            ui.notify(_("Export error: {err}").format(err=exc), type="negative")
        finally:
            try:
                btn_approve.props(remove="loading")
            except RuntimeError:
                pass

    def _reject():
        repository.update_task_status(task.id, user_id, TaskStatus.FAILED)
        if table_refresh:
            table_refresh.refresh()

    with ui.row().classes("w-full items-center justify-between mt-3"):
        ui.button(_("Discard"), icon="close", on_click=_reject).props("flat dark").classes("text-red-400")
        with ui.row().classes("items-center gap-2"):
            if task.result_md:
                def _dl_md_review(_t=task):
                    ui.download(
                        (_t.result_md or "").encode("utf-8"),
                        _md_filename(_t),
                        media_type="text/markdown",
                    )
                ui.button(".MD", icon="download", on_click=_dl_md_review).props(
                    "unelevated dark"
                ).classes("text-white").tooltip(_("Download result as Markdown"))
            btn_approve = ui.button(
                _("Approve & send to Paperless"), icon="save", on_click=_approve
            ).props("unelevated dark").classes("bg-green-700 text-white")

    if subtasks:
        ui.separator().classes("mt-4")
        with ui.expansion(_("Show processing steps")).classes("w-full text-gray-400"):
            _render_board_readonly(task, subtasks, on_detail_click=on_detail_click)


def _render_completed(
    task: repository.Task,
    subtasks: list[repository.SubTask],
    dlg,
    table_refresh,
    on_detail_click=None,
) -> None:
    _ = get_translator()
    user_id = ng_app.storage.user.get("paperless_user", "")

    with ui.column().classes("w-full gap-3"):
        with ui.row().classes("items-center gap-2"):
            ui.icon("check_circle", size="sm").classes("text-green-400")
            ui.label(_("Task completed")).classes("text-green-400 font-semibold")

        with ui.row().classes("items-center gap-2 flex-wrap"):
            if task.paperless_id:
                async def _open_pdf(_t=task):
                    import base64
                    from services.clients import get_session_paperless
                    _bytes = await get_session_paperless().download_document(_t.paperless_id)
                    _b64 = base64.b64encode(_bytes).decode()
                    await ui.run_javascript(f"""
                        const b=atob('{_b64}'),arr=new Uint8Array(b.length);
                        for(let i=0;i<b.length;i++)arr[i]=b.charCodeAt(i);
                        window.open(URL.createObjectURL(new Blob([arr],{{type:'application/pdf'}})),'_blank');
                    """)
                ui.button("PDF", icon="picture_as_pdf", on_click=_open_pdf).props(
                    "flat dark dense"
                ).classes("text-purple-400 text-sm").tooltip(_("Open PDF in a new tab"))
            if task.result_md:
                def _dl_md_done(_t=task):
                    ui.download(
                        (_t.result_md or "").encode("utf-8"),
                        _md_filename(_t),
                        media_type="text/markdown",
                    )
                ui.button(".MD", icon="download", on_click=_dl_md_done).props(
                    "flat dark dense"
                ).classes("text-purple-400 text-sm").tooltip(_("Download result as Markdown"))
        if task.paperless_url:
            ui.link(
                _("Open document #{id} in Paperless").format(id=task.paperless_id),
                target=task.paperless_url,
            ).classes("text-purple-400 text-sm underline").props('target="_blank"')
        elif task.paperless_id and not task.paperless_url:
            ui.label(_("Paperless ID: #{id}").format(id=task.paperless_id)).classes("text-sm text-gray-400")

        if subtasks:
            ui.separator()
            _render_board_readonly(task, subtasks, on_detail_click=on_detail_click)

        if task.result_md:
            ui.separator()
            with ui.expansion(_("Show result")).classes("w-full text-gray-400"):
                ui.markdown(task.result_md).classes("text-gray-300 text-sm")

        ui.separator()

        def _delete():
            repository.delete_task(task.id, user_id)
            dlg.close()
            if table_refresh:
                table_refresh.refresh()

        ui.button(_("Delete task"), icon="delete", on_click=_delete).props("flat dark").classes("text-red-500")


def _render_failed(task: repository.Task, dlg, table_refresh) -> None:
    _ = get_translator()
    user_id = ng_app.storage.user.get("paperless_user", "")
    with ui.column().classes("w-full gap-2"):
        ui.label(_("Task failed")).classes("text-red-400 font-semibold")
        if task.result_md:
            ui.markdown(task.result_md[:500]).classes("text-gray-400 text-sm")

        def _retry():
            repository.update_task_status(task.id, user_id, TaskStatus.DRAFT)
            if table_refresh:
                table_refresh.refresh()

        def _delete():
            repository.delete_task(task.id, user_id)
            dlg.close()
            if table_refresh:
                table_refresh.refresh()

        with ui.row().classes("gap-2 mt-2"):
            ui.button(_("Reset to draft"), icon="refresh", on_click=_retry).props("flat dark").classes("text-gray-400")
            ui.button(_("Delete"), icon="delete", on_click=_delete).props("flat dark").classes("text-red-500")


# ── Public entry point ────────────────────────────────────────────────────────

def open_task_dialog(
    task_id: int,
    user_id: str,
    token: str,
    table_refresh=None,
    on_close=None,
) -> None:
    _ = get_translator()
    _timer_ref:    list = [None]
    _last_status:  list = [None]
    _sub_dlg_open: list = [False]

    with ui.dialog().props("persistent") as dlg:
        with ui.card().classes("bg-gray-900").style(_DLG_W):

            # ── Dialog header ──────────────────────────────────────────
            task_init = repository.get_task(task_id, user_id)
            orig_req  = (task_init.original_request if task_init else "") or ""

            # Pre-derive title only for tasks that already have refined_request (past TRIAGE).
            # DRAFT tasks get their title from _start() after the planner runs.
            if task_init and not task_init.short_title and task_init.refined_request:
                from werkbank.roles import planner as _planner_mod
                _t = _planner_mod.derive_title(task_init.refined_request, task_init.original_request)
                if _t:
                    repository.update_task_title(task_id, user_id, _t)
                    task_init = repository.get_task(task_id, user_id)

            with ui.row().classes("w-full items-center justify-between mb-2"):
                with ui.row().classes("items-center gap-2 flex-1 min-w-0"):
                    ui.icon("auto_awesome", size="xs").classes("text-purple-400 flex-shrink-0")

                    @ui.refreshable
                    def _header_title() -> None:
                        t = repository.get_task(task_id, user_id)
                        label = (t.short_title if t and t.short_title else None) or orig_req
                        ui.label(label).classes("text-sm font-semibold text-gray-200").style(
                            "overflow:hidden;white-space:nowrap;text-overflow:ellipsis;min-width:0"
                        )

                    _header_title()
                ui.button(icon="close", on_click=dlg.close).props("flat dark dense").classes(
                    "text-gray-500 flex-shrink-0"
                )
            ui.separator()
            # Full original request — small, italic, grey, below the line
            if orig_req:
                ui.label(orig_req).classes("text-xs text-gray-500 italic px-1 pb-1").style(
                    "word-break:break-word"
                )

            # ── Pre-created subtask detail dialog ─────────────────────
            # Sibling of content() refreshable — survives content.refresh().
            with ui.dialog().props("persistent") as sub_dlg:
                sub_detail_card = ui.card().classes("bg-gray-900").style(
                    "width:min(95vw,680px);max-height:85vh;overflow-y:auto;padding:16px"
                )

            def _on_sub_hide():
                _sub_dlg_open[0] = False

            sub_dlg.on("hide", _on_sub_hide)

            def show_subtask_detail(s: repository.SubTask, arch_name: str = "") -> None:
                all_subs  = repository.get_subtasks(task_id, user_id)
                id_to_idx = {st.id: st.order_index + 1 for st in all_subs}
                _fill_subtask_detail(sub_detail_card, s, arch_name, sub_dlg.close, id_to_idx)
                _sub_dlg_open[0] = True
                sub_dlg.open()

            # ── Main refreshable content ───────────────────────────────
            @ui.refreshable
            def content() -> None:
                task     = repository.get_task(task_id, user_id)
                if not task:
                    ui.label(_("Task not found.")).classes("text-red-400")
                    return
                subtasks = repository.get_subtasks(task_id, user_id)

                with ui.column().classes("w-full gap-2 pt-2"):
                    if task.status == TaskStatus.DRAFT:
                        _render_draft(task, dlg, table_refresh, content.refresh, _header_title.refresh)

                    elif task.status == TaskStatus.TRIAGE:
                        if task.refined_request:
                            _render_triage_ready(task, table_refresh)
                        else:
                            _render_triage_loading()

                    elif task.status in (TaskStatus.RUNNING, TaskStatus.QUEUED, TaskStatus.PAUSED):
                        _render_board(task, subtasks, dlg, table_refresh, show_subtask_detail)

                    elif task.status == TaskStatus.AWAITING_REVIEW:
                        _render_review(task, subtasks, dlg, table_refresh, token, show_subtask_detail)

                    elif task.status == TaskStatus.COMPLETED:
                        _render_completed(task, subtasks, dlg, table_refresh, show_subtask_detail)

                    else:
                        _render_failed(task, dlg, table_refresh)

            content()

            def _maybe_refresh():
                try:
                    task = repository.get_task(task_id, user_id)
                    if not task:
                        return

                    # Always keep header title fresh (cheap DB read)
                    _header_title.refresh()

                    if _sub_dlg_open[0]:
                        return

                    prev = _last_status[0]
                    _last_status[0] = task.status

                    if task.status in (TaskStatus.QUEUED, TaskStatus.RUNNING, TaskStatus.PAUSED):
                        content.refresh()
                    elif task.status != prev:
                        content.refresh()
                except RuntimeError:
                    if _timer_ref[0]:
                        _timer_ref[0].deactivate()

            _timer_ref[0] = ui.timer(2.5, _maybe_refresh)

            def _on_hide():
                if _timer_ref[0]:
                    _timer_ref[0].deactivate()
                sub_dlg.close()
                if on_close:
                    on_close()

            dlg.on("hide", _on_hide)

    dlg.open()
