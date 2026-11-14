"""The module overview behind the "?" in the header — reachable from every
page (it used to be a fold on the dashboard only)."""

from __future__ import annotations

from nicegui import ui

from i18n import get_translator


def _entries(_) -> list[tuple[str, str, str]]:
    from config.proactive import is_enabled
    from services.experts import enabled as experts_on

    out = [
        ("dashboard", _("Dashboard"), _(
            "The state of the system at a glance: how many documents are indexed and when the last sync "
            "ran, the AI host (on, off, what it is generating), upcoming deadlines and pinned documents. "
            "Sync Paperless or the vault by hand, and let 'Dreaming' tidy up the assistant's memory.")),
        ("chat", _("Chat"), _(
            "The assistant with access to your document archive, your notes and memory, the web, email "
            "and calendar. Ask questions, have documents summarised, draft letters. Every claim is cited: "
            "tap a number to see the passage on the page. Pick the model and the expert at the top; the "
            "tool switches and quick actions are below the input.")),
    ]
    if is_enabled():
        out.append(("lightbulb", _("Findings"), _(
            "What the system noticed in your documents while you were away — deadlines, price changes, "
            "contracts worth a look — as an inbox, plus dossiers: one binder per matter with its state, "
            "timeline, open questions, documents, research and the chat answers you saved. The chat on "
            "the right (below on a phone) talks about what you are looking at.")))
    out += [
        ("note_alt", _("Note vault"), _(
            "Your notes and the assistant's long-term memory in one place — a file tree with a Markdown "
            "editor, also editable in Obsidian. The chat searches these notes and reads the facts it "
            "stored about you. Voice memos, research reports and findings land here too.")),
        ("auto_awesome", _("Deep research"), _(
            "An agent for larger questions: it plans, splits the work into sub-tasks and runs them — "
            "documents, web, email, calculations — then writes a report in which every statement points "
            "to its source and what it could not find is said plainly. The report is saved as a note.")),
    ]
    if experts_on():
        out.append(("psychology", _("Experts"), _(
            "Chat personas with their own instructions, skills and document access — an insurance expert "
            "that sees only policies, a physician that sees only medical letters. 'Study documents' gives "
            "one a briefing of its archive. The Universal expert sees everything.")))
    out.append(("grid_view", _("Browser"), _(
        "The document archive (Paperless-ngx) in full: search by text or meaning, filter by type, "
        "correspondent, tag or date, open, pin, or find similar documents.")))
    return out


def open_help() -> None:
    _ = get_translator()
    with ui.dialog() as dlg, ui.card().classes("gap-3").style(
            "background:var(--c-surface); width:min(680px,96vw); max-width:96vw; max-height:88vh;"
            "overflow-y:auto; border:1px solid var(--c-border);"):
        with ui.row().classes("w-full items-center no-wrap"):
            ui.icon("help_outline", size="sm").classes("text-gray-400")
            ui.label(_("Help — the modules")).classes("text-base font-semibold flex-1").style("color:var(--c-text)")
            ui.button(icon="close", on_click=dlg.close).props("flat dense round").classes("card-action-btn")
        for icon, title, desc in _entries(_):
            with ui.row().classes("w-full items-start gap-3 no-wrap"):
                ui.icon(icon, size="sm").classes("text-gray-400 flex-shrink-0").style("margin-top:1px")
                with ui.column().classes("gap-0.5 min-w-0 flex-1"):
                    ui.label(title).classes("text-sm font-semibold w-full").style("color:var(--c-text)")
                    ui.label(desc).classes("text-xs leading-snug w-full").style("color:var(--c-text-2)")
        ui.label(_("Settings (under your name, top right) hold the models, channels, languages and switches.")).classes(
            "text-xs").style("color:var(--c-text-muted)")
    dlg.open()
