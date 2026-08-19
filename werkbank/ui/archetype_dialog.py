"""werkbank/ui/archetype_dialog.py — CRUD dialog for worker archetypes.

Lists the user's archetypes, allows editing name / description / soul_text
and toggling tool subsets from the real TOOL_DEFINITIONS registry.
"""

from __future__ import annotations

from nicegui import ui

from i18n import get_translator
from werkbank import repository
from werkbank.archetypes import validate_tool_subset

_CARD_BG = "background:var(--c-surface-2)"

# The dialog is two columns on a desktop and one on a phone. Below Tailwind's
# `md` the fixed-width list beside a `flex-1` form left the form about 100 px
# wide: every input and every tool name was cut off after the first character.
_DIALOG_CSS = """
<style>
.wb-arch-tools .q-scrollarea__content { max-width: 100%; }
/* Tool names are single unbreakable words, so they need an explicit break. */
.wb-arch-tools .q-checkbox__label { overflow-wrap: anywhere; font-size: .75rem; }
</style>
"""
# Tools that should not appear in worker subsets (UI dialogs, not useful in background)
_EXCLUDED_TOOLS = {"trigger_docx_generation", "create_email", "generate_chat_pdf"}


def _get_tool_registry() -> list[dict]:
    from services.chat_service import TOOL_DEFINITIONS
    return [t for t in TOOL_DEFINITIONS if t["name"] not in _EXCLUDED_TOOLS]


def _is_edited_default(arch) -> bool:
    """A shipped archetype the user has changed — the only case with a way back."""
    try:
        from werkbank.archetypes import differs_from_default

        return differs_from_default(arch)
    except Exception:
        return False


def _archetype_list(user_id: str, list_area, edit_area) -> None:
    """Render the left-hand list of archetypes."""
    _ = get_translator()
    archetypes = repository.get_archetypes(user_id)
    list_area.clear()
    with list_area:
        for arch in archetypes:
            with ui.card().classes("w-full cursor-pointer mb-1").style(_CARD_BG):
                with ui.row().classes("w-full items-center gap-2"):
                    with ui.column().classes("flex-1"):
                        ui.label(arch.name).classes("text-sm font-semibold text-gray-200")
                        ui.label(arch.description[:50]).classes("text-xs text-gray-500")

                    def _edit(a=arch):
                        _show_edit_form(user_id, a, list_area, edit_area)

                    def _del(a=arch):
                        repository.delete_archetype(a.id, user_id)
                        _archetype_list(user_id, list_area, edit_area)
                        edit_area.clear()

                    ui.button(icon="edit", color=None, on_click=_edit).props(
                        "flat dense"
                    ).classes("card-action-btn")
                    def _restore(a=arch):
                        from werkbank.archetypes import restore_default

                        if restore_default(a.name, user_id):
                            _archetype_list(user_id, list_area, edit_area)
                            edit_area.clear()
                            ui.notify(_("«{name}» restored to default.").format(name=a.name))

                    if _is_edited_default(arch):
                        # Only offered when it would actually change something —
                        # a button that does nothing teaches people to distrust
                        # the ones that do.
                        ui.button(icon="restart_alt", color=None, on_click=_restore).props(
                            "flat dense"
                        ).classes("card-action-btn").tooltip(_("Restore default"))

                    # Neutral like every other card action: colour marks state the
                    # user must react to, and a row of red bins is not that.
                    ui.button(icon="delete_outline", color=None, on_click=_del).props(
                        "flat dense"
                    ).classes("card-action-btn")

        # "Neuer Archetyp" button
        def _new():
            _show_edit_form(user_id, None, list_area, edit_area)

        ui.button(_("New archetype"), icon="add", on_click=_new).props("flat dark").classes(
            "text-purple-400 mt-2"
        )


def _show_edit_form(
    user_id: str,
    arch: repository.Archetype | None,
    list_area,
    edit_area,
) -> None:
    """Render the right-hand edit form for an archetype."""
    _ = get_translator()
    tools_all = _get_tool_registry()
    existing_tools: set[str] = set(arch.enabled_tools) if arch else set()

    edit_area.clear()
    with edit_area:
        heading = arch.name if arch else _("New archetype")
        ui.label(heading).classes("text-sm font-semibold text-gray-100 mb-2")

        name_inp = ui.input(
            label=_("Name"),
            value=arch.name if arch else "",
            placeholder="z.B. analyst",
        ).classes("w-full").props("dark outlined")

        desc_inp = ui.input(
            label=_("Short description"),
            value=arch.description if arch else "",
            placeholder=_("What does this agent do?"),
        ).classes("w-full").props("dark outlined")

        soul_inp = ui.textarea(
            label=_("Soul text (system prompt)"),
            value=arch.soul_text if arch else "",
            placeholder=_("You are a …"),
        ).classes("w-full").style("min-height:130px").props("dark outlined")

        # ── Tool checkboxes ───────────────────────────────────────────
        ui.label(_("Enabled tools")).classes(
            "text-xs text-gray-500 tracking-wide mt-3 mb-1"
        )
        checkboxes: dict[str, ui.checkbox] = {}
        with ui.scroll_area().classes("wb-arch-tools").style(
            "max-height:220px; width:100%; max-width:100%; "
            "border:1px solid var(--c-border); border-radius:4px; padding:4px"
        ):
            with ui.column().classes("gap-0 w-full"):
                for t in tools_all:
                    cb = ui.checkbox(
                        text=t["name"],
                        value=t["name"] in existing_tools,
                    ).props("dark").classes("text-xs")
                    checkboxes[t["name"]] = cb

        # ── Save ─────────────────────────────────────────────────────
        def _save():
            name = name_inp.value.strip()
            if not name:
                ui.notify(_("Name must not be empty."), type="warning")
                return
            enabled = [n for n, cb in checkboxes.items() if cb.value]
            valid   = validate_tool_subset(enabled)

            if arch:
                repository.update_archetype(
                    arch.id, user_id,
                    name=name,
                    description=desc_inp.value.strip(),
                    soul_text=soul_inp.value.strip(),
                    enabled_tools=valid,
                )
                ui.notify(_("Archetype '{name}' saved.").format(name=name), type="positive")
            else:
                repository.create_archetype(
                    user_id=user_id,
                    name=name,
                    description=desc_inp.value.strip(),
                    soul_text=soul_inp.value.strip(),
                    enabled_tools=valid,
                )
                ui.notify(_("Archetype '{name}' created.").format(name=name), type="positive")

            _archetype_list(user_id, list_area, edit_area)
            edit_area.clear()

        ui.button(_("Save"), icon="save", on_click=_save).props(
            "unelevated dark"
        ).classes("bg-purple-700 text-white mt-3")

    # Stacked layout: the form is below the list, so tapping "edit" would
    # otherwise change something the user cannot see.
    ui.run_javascript(
        f"getHtmlElement({edit_area.id})?.scrollIntoView("
        "{behavior: 'smooth', block: 'start'});"
    )


def open_archetype_dialog(user_id: str) -> None:
    """Create and open the archetype management dialog."""
    _ = get_translator()
    from werkbank.archetypes import seed_defaults_if_needed
    seed_defaults_if_needed(user_id)

    ui.add_head_html(_DIALOG_CSS)

    with ui.dialog() as dlg:
        with ui.card().style(
            "background:var(--c-surface);width:min(98vw,880px);"
            "max-height:88vh;overflow-y:auto;min-width:0"
        ):
            with ui.row().classes("w-full items-center justify-between mb-3"):
                ui.label(_("Agent archetypes")).classes(
                    "text-lg font-bold text-gray-100"
                )
                with ui.row().classes("items-center gap-1"):
                    def _restore_all():
                        from werkbank.archetypes import restore_all_defaults

                        count = restore_all_defaults(user_id)
                        _archetype_list(user_id, list_col, edit_col)
                        edit_col.clear()
                        ui.notify(
                            _("{n} default archetypes restored.").format(n=count)
                        )

                    ui.button(
                        _("Restore defaults"), icon="restart_alt", color=None,
                        on_click=_restore_all,
                    ).props("flat dense").style("color:var(--c-text-2)").tooltip(
                        _("Puts the shipped archetypes back. Your own are untouched.")
                    )
                    ui.button(icon="close", color=None, on_click=dlg.close).props(
                        "flat dense"
                    ).classes("card-action-btn")

            ui.separator()

            with ui.row().classes("w-full gap-4 mt-3 flex-wrap items-start"):
                # Left: list. Full width on a phone, a fixed column from `md` up.
                with ui.column().classes(
                    "w-full md:w-60 md:flex-shrink-0 min-w-0"
                ) as list_col:
                    pass  # filled by _archetype_list

                ui.separator().props("vertical").classes("hidden md:block")

                # Right: edit form
                with ui.column().classes("w-full md:flex-1 min-w-0") as edit_col:
                    ui.label(_("Select an archetype or create a new one.")).classes(
                        "text-sm text-gray-500"
                    )

            _archetype_list(user_id, list_col, edit_col)

    dlg.open()
