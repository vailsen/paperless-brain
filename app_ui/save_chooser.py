"""One "Save…" for an answer and for a whole conversation.

There used to be three save items in two menus ("Save to dossier", "Save as
memory" under an answer, "Save chat to Paperless" in the conversation menu),
each named after its mechanism. Now one item opens this chooser, which names
the destinations by what they are for. Where it was opened decides what is
saved: the selected text, the answer, or the whole conversation. Each choice
then opens the dialog it always had — nothing is saved from here.

- **Remember this** (memory): one fact the assistant recalls in later chats.
  Not for a whole conversation — a memory is one fact.
- **Add to a dossier**: a matter followed in Findings; only with the proactive
  system on.
- **File in Paperless**: a PDF in the archive.
"""

from __future__ import annotations

from nicegui import ui

from i18n import get_translator

MEMORY, DOSSIER, PAPERLESS = "memory", "dossier", "paperless"
#: What is being saved — where the chooser was opened.
SCOPES = ("selection", "answer", "conversation")


def destinations(scope: str, *, dossiers_on: bool, memory_on: bool = True) -> list[str]:
    """The choices for this scope, in the order shown."""
    out: list[str] = []
    if memory_on and scope != "conversation":
        out.append(MEMORY)
    if dossiers_on:
        out.append(DOSSIER)
    out.append(PAPERLESS)
    return out


async def choose(scope: str, *, dossiers_on: bool, memory_on: bool = True) -> str | None:
    """Ask where it goes; the chosen destination, or None when cancelled."""
    _ = get_translator()
    heading = {"selection": _("Save the selected text"), "answer": _("Save this answer"),
               "conversation": _("Save this conversation")}.get(scope, _("Save"))
    rows = {
        MEMORY: ("psychology", _("Remember this"),
                 _("One fact the assistant keeps and recalls in later chats.")),
        DOSSIER: ("create_new_folder", _("Add to a dossier"),
                  _("Part of a matter you follow in Findings — as an AI summary or verbatim.")),
        PAPERLESS: ("description", _("File in Paperless"),
                    _("A PDF in your archive, found and tagged like any other document.")),
    }
    with ui.dialog() as dlg, ui.card().classes("gap-1").style(
            "background:var(--c-surface); width:440px; max-width:94vw;"):
        ui.label(heading).classes("text-sm font-semibold").style("color:var(--c-text)")
        if scope == "answer":
            ui.label(_("To save only part of it, select that text first, then open “Save…”.")).classes(
                "text-xs").style("color:var(--c-text-muted)")
        with ui.list().classes("w-full").props("separator"):
            for key in destinations(scope, dossiers_on=dossiers_on, memory_on=memory_on):
                icon, title, what = rows[key]
                with ui.item(on_click=lambda k=key: dlg.submit(k)).classes("rounded-borders"):
                    with ui.item_section().props("avatar"):
                        ui.icon(icon).classes("text-gray-500")
                    with ui.item_section():
                        ui.item_label(title).classes("text-sm")
                        ui.item_label(what).props("caption")
        with ui.row().classes("justify-end w-full"):
            ui.button(_("Cancel"), on_click=dlg.close).props("flat no-caps")
    picked = await dlg
    dlg.delete()
    return picked
