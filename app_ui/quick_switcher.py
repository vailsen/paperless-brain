"""Go anywhere: one search over the whole app (Ctrl/Cmd+K, or the header's
search button on a phone).

Modules, documents (title or `#id`), notes, findings, dossiers, experts,
conversations and the settings sections — each a row that opens its page.
Everything comes from the user's own data with their own token; hidden
modules are left out (`app_ui/modules.py`). The findings page's own search
moved to "/" to leave Ctrl+K to this.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import quote

from nicegui import ui

from i18n import get_translator

_JS = """<script>
if(!window.__pbQuick){window.__pbQuick=1;
 document.addEventListener('keydown',function(e){
   if((e.ctrlKey||e.metaKey)&&!e.shiftKey&&e.key.toLowerCase()==='k'){e.preventDefault();emitEvent('pb_quick');}
 });}
</script>"""

MAX_PER_GROUP = 8


def _fold(s: str) -> str:
    from vault.search import fold

    return fold(s or "")


def _hit(q: str, *texts: str) -> int | None:
    """0 = starts with, 1 = contains every word, None = no match."""
    hay = " ".join(_fold(t) for t in texts if t)
    if not q:
        return 1
    if hay.startswith(q):
        return 0
    return 1 if all(w in hay for w in q.split()) else None


async def _sources(user: str, token: str) -> dict:
    """Everything searchable, gathered once per opening."""
    from app_ui.modules import is_hidden, visible_modules

    out: dict = {"modules": visible_modules(), "docs": [], "notes": [], "findings": [], "dossiers": [],
                 "experts": [], "convs": []}

    async def docs():
        if is_hidden("browser"):
            return
        from services.clients import get_session_paperless

        try:
            out["docs"] = await get_session_paperless().list_catalogue()
        except Exception:  # noqa: BLE001 — Paperless down: no document rows
            pass

    async def notes():
        if is_hidden("vault"):
            return
        from vault.scan import note_paths

        out["notes"] = await asyncio.to_thread(note_paths, user)

    async def proactive():
        from config.proactive import is_enabled

        if not is_enabled() or is_hidden("findings"):
            return
        from proactive import dossiers, findings

        out["findings"] = await asyncio.to_thread(findings.list_findings, user)
        out["dossiers"] = await asyncio.to_thread(dossiers.list_dossiers, user)

    async def experts():
        if is_hidden("experts"):
            return
        from services.experts import list_experts

        out["experts"] = await asyncio.to_thread(list_experts, user)

    async def convs():
        import services.chat_history_service as hist

        out["convs"] = await asyncio.to_thread(hist.list_conversations, user)

    await asyncio.gather(docs(), notes(), proactive(), experts(), convs(), return_exceptions=True)
    return out


def _rows(src: dict, text: str, _) -> list[tuple[str, str, str, str]]:
    """(group, icon, label, target url)."""
    q = _fold(text.strip())
    rows: list[tuple[int, str, str, str, str]] = []

    def add(group: str, icon: str, label: str, url: str, rank: int | None, sub: str = "") -> None:
        if rank is not None:
            rows.append((rank, group, icon, label + (f"  ·  {sub}" if sub else ""), url))

    for m in src["modules"]:
        add(_("Modules"), m.icon, _(m.label), m.path, _hit(q, _(m.label), m.key))
    if q.startswith("#") and q[1:].isdigit():
        add(_("Documents"), "description", _("Document #{id}").format(id=q[1:]), f"/browser?open={q[1:]}", 0)
    elif q:
        n = 0
        for d in src["docs"]:
            r = _hit(q, d.title, d.correspondent or "", str(d.id))
            if r is not None:
                add(_("Documents"), "description", d.title or f"#{d.id}", f"/browser?open={d.id}", r,
                    d.correspondent or "")
                n += 1
                if n >= 40:
                    break
        for rel in src["notes"]:
            add(_("Notes"), "note_alt", Path(rel).stem, f"/brain?open={quote(rel)}",
                _hit(q, Path(rel).stem, rel), str(Path(rel).parent) if "/" in rel else "")
        for f in src["findings"]:
            add(_("Findings"), "lightbulb", f.title, f"/findings/{quote(f.finding_id)}", _hit(q, f.title, f.topic_label))
        for d in src["dossiers"]:
            add(_("Dossiers"), "folder_open", d.display, f"/findings?d={quote(d.pbrain_id)}", _hit(q, d.display))
        for e in src["experts"]:
            add(_("Experts"), e.icon or "psychology", e.name, f"/experts?e={quote(e.expert_id)}", _hit(q, e.name))
        for c in src["convs"][:300]:
            add(_("Conversations"), "forum", c.get("title") or _("Untitled"), f"/chat?conv={quote(str(c['id']))}",
                _hit(q, c.get("title") or ""))
        for key, label in (("general", _("General")), ("ai", _("AI")), ("docs", _("Documents")),
                           ("memory", _("Search & memory")), ("voice", _("Voice")),
                           ("connect", _("Connections")), ("data", _("Backup"))):
            add(_("Settings"), "settings", label, f"/settings?s={key}", _hit(q, label, _("Settings")))
    per: dict[str, int] = {}
    out = []
    for rank, group, icon, label, url in sorted(rows, key=lambda r: r[0]):
        if per.get(group, 0) >= MAX_PER_GROUP:
            continue
        per[group] = per.get(group, 0) + 1
        out.append((group, icon, label, url))
    order = [_("Modules"), _("Documents"), _("Notes"), _("Findings"), _("Dossiers"), _("Experts"),
             _("Conversations"), _("Settings")]
    out.sort(key=lambda r: order.index(r[0]) if r[0] in order else 99)
    return out


def install(username: str, token: str) -> None:
    """Once per page: the key binding and the handler."""
    ui.add_head_html(_JS)
    ui.on("pb_quick", lambda _e: open_switcher(username, token))


async def open_switcher(username: str, token: str) -> None:
    _ = get_translator()
    with ui.dialog().props("position=top") as dlg, ui.card().classes("gap-1").style(
            "background:var(--c-surface); width:600px; max-width:94vw; margin-top:7vh; padding:10px;"):
        q = ui.input(placeholder=_("Go to … documents, notes, findings, settings")).props(
            "outlined dense autofocus clearable").classes("w-full")
        with q.add_slot("prepend"):
            ui.icon("search", size="xs")
        box = ui.column().classes("w-full gap-0").style("max-height:60vh; overflow-y:auto")
        state: dict = {"src": None, "rows": []}

        def fill() -> None:
            box.clear()
            if state["src"] is None:
                with box:
                    ui.spinner(size="sm")
                return
            state["rows"] = _rows(state["src"], q.value or "", _)
            last = ""
            with box:
                for group, icon, label, url in state["rows"]:
                    if group != last:
                        ui.label(group).classes("text-xs pt-2 px-2").style("color:var(--c-text-muted)")
                        last = group
                    row = ui.row().classes("w-full items-center gap-2 no-wrap px-2 py-1 pb-quick-row").style(
                        "border-radius:6px; cursor:pointer; min-width:0")
                    row.on("click", lambda _e, u=url: dlg.submit(u))
                    with row:
                        ui.icon(icon, size="xs").style("color:var(--c-text-muted)")
                        ui.label(label).classes("text-sm flex-1 min-w-0 w-full").style(
                            "color:var(--c-text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap")
                if not state["rows"]:
                    ui.label(_("Nothing found.")).classes("text-xs px-2 py-2").style("color:var(--c-text-muted)")

        q.on("update:model-value", lambda _e: fill())
        q.on("keydown.enter", lambda: dlg.submit(state["rows"][0][3] if state["rows"] else None))
        fill()
    ui.add_css(".pb-quick-row:hover{background:var(--c-surface-2)}")
    dlg.open()

    async def load() -> None:
        state["src"] = await _sources(username, token)
        fill()

    asyncio.create_task(load())
    url = await dlg
    dlg.delete()
    if url:
        ui.navigate.to(url)
