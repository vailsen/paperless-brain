# app_ui/pages/brain.py
"""Gedächtnis-Verwaltung — add, edit, delete, pin BrainFacts."""

import asyncio

from nicegui import app as ng_app
from nicegui import ui

from app_ui.layout import page_layout, require_auth
from i18n import get_translator
from services.brain_service import BrainFact
from services.clients import brain, vault_brain_writer


# ── Helpers ───────────────────────────────────────────────────────────────────

def _conf_color(c: float) -> str:
    if c >= 0.9:
        return "text-green-400"
    if c >= 0.6:
        return "text-yellow-400"
    return "text-red-400"


def _tags_str(tags: list[str]) -> str:
    return ", ".join(tags) if tags else "–"


def _tags_list(raw: str) -> list[str]:
    return [t.strip().replace(" ", "-") for t in raw.split(",") if t.strip()]


# ── Page ──────────────────────────────────────────────────────────────────────

@ui.page("/brain")
async def brain_page():
    if not require_auth():
        return
    page_layout()
    _ = get_translator()

    username = ng_app.storage.user.get("paperless_user", "")
    _state = {
        "facts": [],          # all facts from service (own + common if toggled)
        "show_common": False, # toggle: also display common-by-others
        "deadlines": [],      # manual due-dates (kind=deadline)
    }
    _dl_edit_id = [None]      # None = create, else pbrain_id being edited

    # ── Dialogs ───────────────────────────────────────────────────────────────

    async def _confirm_delete(text: str) -> bool:
        with ui.dialog() as dlg, ui.card().style(
            "background:var(--c-surface); width:min(380px,95vw);"
        ):
            ui.label(_("Really delete?")).classes(
                "text-base font-semibold text-gray-100"
            )
            ui.label(text).classes("text-sm text-gray-400")
            with ui.row().classes("justify-end gap-2 mt-3 w-full"):
                ui.button(_("Cancel"), on_click=lambda: dlg.submit(False)).props(
                    "flat dark dense"
                ).classes("text-gray-400")
                ui.button(_("Delete"), on_click=lambda: dlg.submit(True)).props(
                    "dark dense"
                ).classes("text-red-400")
        return bool(await dlg)

    with ui.dialog().props("persistent") as _add_dlg:
        with ui.card().style(
            "background:var(--c-surface); width:min(520px,95vw); max-height:90vh; overflow-y:auto;"
        ):
            with ui.row().classes("items-center justify-between mb-2 w-full"):
                _add_dlg_title = ui.label(_("Add fact")).classes(
                    "text-base font-semibold text-gray-100"
                )
                ui.button(icon="close", on_click=_add_dlg.close).props(
                    "flat dark dense"
                ).classes("text-gray-400")
            ui.separator()

            _edit_id = [None]  # None = new, str = editing existing

            _f_text = (
                ui.textarea(_("Fact"), placeholder=_("The fact in a single clear sentence…"))
                .props("outlined dark dense")
                .classes("w-full mt-3")
                .style("min-height:80px;")
            )
            _f_filename = (
                ui.input(_("Filename (topic)"), placeholder=_("e.g. Anna's birthday"))
                .props("outlined dark dense")
                .classes("w-full mt-2")
            )
            ui.label(_("Topic only — stays stable even if the content changes.")).classes(
                "text-gray-500 text-xs"
            )
            _f_tags = (
                ui.input(_("Tags (comma-separated)"), placeholder=_("insurance, deadline, family"))
                .props("outlined dark dense")
                .classes("w-full mt-2")
            )
            with ui.row().classes("w-full gap-3 mt-2"):
                _f_conf = (
                    ui.number(_("Confidence"), value=1.0, min=0.0, max=1.0, step=0.05, format="%.2f")
                    .props("outlined dark dense")
                    .classes("flex-1")
                )
                _f_src_id = (
                    ui.number(_("Doc ID"), min=1, step=1, format="%d")
                    .props("outlined dark dense")
                    .classes("w-32")
                )
                _f_src_page = (
                    ui.number(_("Page"), min=1, step=1, format="%d")
                    .props("outlined dark dense")
                    .classes("w-24")
                )
            _add_err = ui.label("").classes("text-red-400 text-xs mt-1")
            _add_err.set_visibility(False)

            async def _save_fact():
                text = _f_text.value.strip()
                if not text:
                    _add_err.set_text(_("Fact must not be empty."))
                    _add_err.set_visibility(True)
                    return
                _add_err.set_visibility(False)
                tags = _tags_list(_f_tags.value or "")
                conf = float(_f_conf.value or 1.0)
                src_id = int(_f_src_id.value) if _f_src_id.value else None
                src_pg = int(_f_src_page.value) if _f_src_page.value else None

                if _edit_id[0] is None:
                    await vault_brain_writer.create_memory(
                        text=text, tags=tags, user=username,
                        source_doc_id=src_id, source_page=src_pg, confidence=conf,
                        filename_topic=_f_filename.value.strip() or None,
                    )
                else:
                    await vault_brain_writer.update_memory(_edit_id[0], text)
                    await vault_brain_writer.update_tags(_edit_id[0], tags)
                    await vault_brain_writer.set_common(_edit_id[0], False)  # reset; user manages separately

                _add_dlg.close()
                await _reload()

            with ui.row().classes("justify-end gap-2 mt-3"):
                ui.button(_("Cancel"), on_click=_add_dlg.close).props(
                    "flat dark dense"
                ).classes("text-gray-400")
                ui.button(_("Save"), icon="save", on_click=_save_fact).props(
                    "unelevated dark"
                ).classes("text-white")

    # ── Deadline dialog ────────────────────────────────────────────────────────

    with ui.dialog().props("persistent") as _dl_dlg:
        with ui.card().style("background:var(--c-surface); width:min(460px,95vw);"):
            with ui.row().classes("items-center justify-between mb-2 w-full"):
                _dl_dlg_title = ui.label(_("Add deadline")).classes(
                    "text-base font-semibold text-gray-100"
                )
                ui.button(icon="close", on_click=_dl_dlg.close).props(
                    "flat dark dense"
                ).classes("text-gray-400")

            _dl_date = (
                ui.input(_("Due on"))
                .props('outlined dark dense type=date stack-label')
                .classes("w-full")
            )
            _dl_text = (
                ui.input(_("Description"))
                .props("outlined dark dense")
                .classes("w-full")
            )
            _dl_tags = (
                ui.input(_("Tags (comma-separated)"))
                .props("outlined dark dense")
                .classes("w-full")
            )
            _dl_err = ui.label("").classes("text-red-400 text-xs mt-1")
            _dl_err.set_visibility(False)

            async def _save_deadline():
                due = (_dl_date.value or "").strip()
                text = (_dl_text.value or "").strip()
                if not due or not text:
                    _dl_err.set_text(_("Date and description are required."))
                    _dl_err.set_visibility(True)
                    return
                _dl_err.set_visibility(False)
                tags = _tags_list(_dl_tags.value or "")
                if _dl_edit_id[0] is None:
                    await vault_brain_writer.create_deadline(
                        text=text, due=due, user=username, tags=tags or None
                    )
                else:
                    await vault_brain_writer.update_deadline(
                        _dl_edit_id[0], text=text, due=due
                    )
                    if tags:
                        await vault_brain_writer.update_tags(_dl_edit_id[0], tags)
                _dl_dlg.close()
                await _reload()

            with ui.row().classes("justify-end gap-2 mt-3"):
                ui.button(_("Cancel"), on_click=_dl_dlg.close).props(
                    "flat dark dense"
                ).classes("text-gray-400")
                ui.button(_("Save"), icon="save", on_click=_save_deadline).props(
                    "unelevated dark"
                ).classes("text-white")

    def _open_add_deadline():
        _dl_edit_id[0] = None
        _dl_dlg_title.set_text(_("Add deadline"))
        _dl_date.set_value("")
        _dl_text.set_value("")
        _dl_tags.set_value("")
        _dl_err.set_visibility(False)
        _dl_dlg.open()

    def _open_edit_deadline(dl: BrainFact):
        _dl_edit_id[0] = dl.id
        _dl_dlg_title.set_text(_("Edit deadline"))
        _dl_date.set_value(dl.due)
        _dl_text.set_value(dl.text)
        _dl_tags.set_value(", ".join(t for t in dl.tags if t != "frist"))
        _dl_err.set_visibility(False)
        _dl_dlg.open()

    def _open_add():
        _edit_id[0] = None
        _add_dlg_title.set_text(_("Add fact"))
        _f_text.set_value("")
        _f_filename.set_value("")
        _f_tags.set_value("")
        _f_conf.set_value(1.0)
        _f_src_id.set_value(None)
        _f_src_page.set_value(None)
        _add_err.set_visibility(False)
        _add_dlg.open()

    def _open_edit(fact: BrainFact):
        _edit_id[0] = fact.id
        _add_dlg_title.set_text(_("Edit fact"))
        _f_text.set_value(fact.text)
        _f_filename.set_value("")  # filename not editable on update (pbrain_id is identity)
        _f_tags.set_value(", ".join(fact.tags))
        _f_conf.set_value(fact.confidence)
        _f_src_id.set_value(fact.source_doc_id)
        _f_src_page.set_value(fact.source_page)
        _add_err.set_visibility(False)
        _add_dlg.open()

    # ── Layout ────────────────────────────────────────────────────────────────

    with ui.column().classes("w-full p-6 gap-4 bg-gray-900").style(
        "min-height:calc(100dvh - var(--q-header-height,52px));"
    ):
        # Header row
        with ui.row().classes("w-full items-center gap-3"):
            ui.icon("psychology", size="md").classes("text-gray-400")
            ui.label(_("Memory")).classes("text-xl font-bold text-gray-100 flex-1")

        # ── Manual deadlines — own bordered card, clearly separated ──────────
        with ui.element("div").style(
            "width:100%; background:var(--c-bg); border:1px solid var(--c-border);"
            "border-radius:12px; padding:14px 16px;"
        ):
            with ui.row().classes("w-full items-center gap-3"):
                ui.icon("event", size="sm").classes("text-gray-400")
                ui.label(_("Manual deadlines")).classes(
                    "text-sm font-semibold text-gray-200 flex-1"
                )
                ui.button(
                    _("Add deadline"), icon="add", on_click=_open_add_deadline
                ).props("flat dark dense").classes("text-purple-300")
            deadlines_col = ui.column().classes("w-full gap-2 mt-2")

        # ── Facts section header (own add button) ───────────────────────────
        with ui.row().classes("w-full items-center gap-3 mt-2"):
            ui.icon("lightbulb", size="sm").classes("text-gray-400")
            ui.label(_("Facts")).classes(
                "text-sm font-semibold text-gray-200 flex-1"
            )
            ui.button(_("Add fact"), icon="add", on_click=_open_add).props(
                "unelevated dark"
            ).classes("text-white")

        # Filter row
        with ui.row().classes("w-full items-center gap-3 flex-wrap"):
            _filter_input = (
                ui.input(placeholder=_("Filter…"))
                .props("outlined dark dense")
                .classes("flex-1 min-w-48")
            )
            _common_toggle = (
                ui.checkbox(_("Show shared facts from other users"))
                .props("dark dense")
                .classes("text-gray-400 text-xs")
            )
            _only_common = (
                ui.checkbox(_("Show shared only"))
                .props("dark dense")
                .classes("text-gray-400 text-xs")
            )

        ui.separator().style("border-color:var(--c-border);")

        # Facts container
        facts_col = ui.column().classes("w-full gap-3")

    # ── Render ────────────────────────────────────────────────────────────────

    def _visible_facts() -> list[BrainFact]:
        all_facts = _state["facts"]
        show_common = _state["show_common"]
        substr = (_filter_input.value or "").lower()
        only_common = _only_common.value

        result = []
        for f in all_facts:
            is_own = f.user == username
            is_foreign_common = (not is_own) and f.common

            if is_foreign_common and not show_common:
                continue
            if only_common and not f.common:
                continue
            if substr and substr not in f.text.lower() and not any(
                substr in t.lower() for t in f.tags
            ):
                continue
            result.append(f)

        return result

    def render_facts() -> None:
        facts_col.clear()
        visible = _visible_facts()
        if not visible:
            with facts_col:
                ui.label(_("No facts found.")).classes("text-gray-500 mt-4")
            return

        with facts_col:
            for fact in sorted(visible, key=lambda f: f.created_at, reverse=True):
                is_own = fact.user == username
                _render_fact_card(fact, is_own)

    def _render_fact_card(fact: BrainFact, is_own: bool) -> None:
        border = (
            "border-left: 3px solid #a78bfa;"
            if fact.common
            else "border-left: 3px solid var(--c-border);"
        )
        bg = "var(--c-surface)" if is_own else "var(--c-surface-2)"

        with ui.element("div").style(
            f"background:{bg}; border-radius:8px; padding:12px 16px;"
            f"{border} width:100%;"
        ):
            with ui.row().classes("w-full items-start gap-2"):
                # Main content
                with ui.column().classes("flex-1 gap-1"):
                    ui.label(fact.text).classes("text-sm text-gray-100 leading-relaxed")

                    with ui.row().classes("items-center gap-2 flex-wrap mt-1"):
                        if fact.tags:
                            for tag in fact.tags:
                                ui.badge(tag, color="purple").classes("text-xs")

                        ui.label(f"{fact.confidence:.0%}").classes(
                            f"text-xs font-mono {_conf_color(fact.confidence)}"
                        ).tooltip(_("Confidence"))

                        if fact.source_doc_id:
                            ui.label(_("Doc #{id}").format(id=fact.source_doc_id)).classes(
                                "text-xs text-blue-400"
                            )

                        ui.label(
                            fact.created_at.strftime("%d.%m.%Y")
                        ).classes("text-xs text-gray-600")

                        if fact.common:
                            ui.badge(_("shared"), color="amber").classes("text-xs")
                        if not is_own:
                            ui.badge(_("from {user}").format(user=fact.user), color="gray").classes("text-xs")

                # Action buttons (own facts only)
                if is_own:
                    with ui.row().classes("items-center gap-0 flex-shrink-0"):
                        ui.button(
                            icon="edit",
                            on_click=lambda f=fact: _open_edit(f),
                        ).props("flat dark dense").classes("text-gray-500").tooltip(_("Edit"))

                        async def _toggle_common(f=fact):
                            await vault_brain_writer.set_common(f.id, not f.common)
                            await _reload()

                        ui.button(
                            icon="public" if fact.common else "public_off",
                            on_click=_toggle_common,
                        ).props("flat dark dense").classes(
                            "text-amber-400" if fact.common else "text-gray-500"
                        ).tooltip(
                            _("Make private") if fact.common else _("Make visible to all")
                        )

                        async def _delete(f=fact):
                            if not await _confirm_delete(f.text):
                                return
                            await vault_brain_writer.delete_memory(f.id)
                            await _reload()

                        ui.button(
                            icon="delete",
                            on_click=_delete,
                        ).props("flat dark dense").classes("text-gray-500").tooltip(_("Delete"))

    # ── Deadlines render ────────────────────────────────────────────────────────

    from datetime import date as _date

    def render_deadlines() -> None:
        deadlines_col.clear()
        items = _state["deadlines"]
        with deadlines_col:
            if not items:
                ui.label(_("No manual deadlines.")).classes(
                    "text-gray-500 text-xs"
                )
                return
            today = _date.today().isoformat()
            for dl in items:
                is_past = bool(dl.due) and dl.due < today
                with ui.element("div").style(
                    "background:var(--c-surface-2); border-radius:8px; padding:8px 12px;"
                    "border-left:3px solid #a78bfa; width:100%;"
                ):
                    with ui.row().classes("w-full items-center gap-2"):
                        _d = dl.due
                        try:
                            _d = _date.fromisoformat(dl.due).strftime("%d.%m.%Y")
                        except ValueError:
                            pass
                        ui.label(_d).classes(
                            "text-xs font-mono " + ("text-red-400" if is_past else "text-gray-300")
                        ).style("white-space:nowrap;")
                        ui.label(dl.text).classes("text-sm text-gray-100 flex-1")
                        ui.button(
                            icon="edit", on_click=lambda d=dl: _open_edit_deadline(d)
                        ).props("flat dark dense").classes("text-gray-500").tooltip(
                            _("Edit")
                        )

                        async def _del_dl(d=dl):
                            if not await _confirm_delete(d.text):
                                return
                            await vault_brain_writer.delete_memory(d.id)
                            await _reload()

                        ui.button(icon="delete", on_click=_del_dl).props(
                            "flat dark dense"
                        ).classes("text-gray-500").tooltip(_("Delete"))

    # ── Reload ────────────────────────────────────────────────────────────────

    async def _reload() -> None:
        _state["facts"] = await brain.get_all(username)
        _state["deadlines"] = await brain.get_deadlines(username)
        render_facts()
        render_deadlines()

    _common_toggle.on_value_change(lambda e: (_state.update({"show_common": e.value}), render_facts()))
    _filter_input.on_value_change(lambda _: render_facts())
    _only_common.on_value_change(lambda _: render_facts())

    # Initial load
    await _reload()
