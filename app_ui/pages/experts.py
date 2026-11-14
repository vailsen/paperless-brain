# app_ui/pages/experts.py
"""Experts — chat personas with a narrowed view of the archive (docs/experts.md).

Left: the experts (Universal first) or the skill library. Right: the editor,
in tabs — Profile, Access, Skills, Tools, Briefing. On a phone the list and
the editor are two screens.

Everything is a file in the user's vault (`services/experts.py`,
`services/skills.py`, `services/briefings.py`); this page reads and writes
those files and nothing else. Saves are hash-checked: an Obsidian edit in
between is merged or reported, never overwritten.
"""

from __future__ import annotations

import asyncio
import logging
import re

from nicegui import app as ng_app
from nicegui import ui

from app_ui.layout import page_layout, require_auth
from i18n import get_translator

log = logging.getLogger("experts_page")

_DOC_EVENT = "pb_expert_doc"

_CSS = """<style>
.pbx-root { position: fixed; top: var(--q-header-height, 52px); left: 0; right: 0; bottom: 0;
  display: grid; grid-template-columns: 300px minmax(0, 1fr); background: var(--c-bg); }
.pbx-aside { border-right: 1px solid var(--c-border); display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.pbx-main { display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.pbx-scroll { overflow-y: auto; overflow-x: hidden; flex: 1; min-height: 0; }
.pbx-item { display: flex; align-items: center; gap: 10px; padding: 9px 12px; cursor: pointer;
  border-radius: 8px; margin: 2px 6px; min-width: 0; color: var(--c-text-2); }
.pbx-item:hover { background: var(--c-surface-2); }
.pbx-item.is-active { background: var(--c-accent-soft); color: var(--c-text); }
.pbx-item .nm { font-size: 0.875rem; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pbx-item .sub { font-size: 0.72rem; color: var(--c-text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pbx-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; background: var(--c-border-strong); }
.pbx-dot.current { background: var(--c-text-2); }
.pbx-dot.outdated { background: var(--c-warn); }
.pbx-tabs { display: flex; gap: 2px; overflow-x: auto; scrollbar-width: none; border-bottom: 1px solid var(--c-border);
  padding: 0 8px; flex-shrink: 0; }
.pbx-tabs::-webkit-scrollbar { display: none; }
.pbx-tab { flex-shrink: 0; padding: 10px 12px; font-size: 0.82rem; color: var(--c-text-muted); cursor: pointer;
  border-bottom: 2px solid transparent; white-space: nowrap; }
.pbx-tab.is-active { color: var(--c-text); border-bottom-color: var(--c-accent); }
.pbx-panel { padding: 16px; max-width: 880px; width: 100%; box-sizing: border-box; }
.pbx-card { border: 1px solid var(--c-border); border-radius: 10px; padding: 12px 14px; background: var(--c-surface);
  width: 100%; box-sizing: border-box; min-width: 0; }
.pbx-label { font-size: 0.75rem; font-weight: 600; color: var(--c-text-2); }
.pbx-hint { font-size: 0.75rem; color: var(--c-text-muted); }
.pbx-icon-btn { width: 38px; height: 38px; border-radius: 8px; display: inline-flex; align-items: center;
  justify-content: center; cursor: pointer; color: var(--c-text-muted); border: 1px solid transparent; }
.pbx-icon-btn:hover { color: var(--c-text); background: var(--c-surface-2); }
.pbx-icon-btn.is-active { color: var(--c-text); border-color: var(--c-accent-border); background: var(--c-accent-soft); }
.pbx-md { font-size: 0.85rem; color: var(--c-text-2); overflow-wrap: anywhere; }
.pbx-md a { color: var(--c-accent-text); text-decoration: none; }
.pbx-bars { display: flex; align-items: flex-end; gap: 3px; height: 34px; }
.pbx-bars div { background: var(--c-text-muted); opacity: .55; border-radius: 2px 2px 0 0; width: 10px; }
.pbx-chip { display: inline-flex; gap: 4px; font-size: 0.75rem; padding: 2px 8px; border-radius: 999px;
  border: 1px solid var(--c-border); color: var(--c-text-2); margin: 2px; white-space: nowrap; }
.pbx-chip .n { color: var(--c-text-muted); }
.pbx-back { display: none; }
@media (max-width: 1023px) {
  .pbx-root { grid-template-columns: minmax(0, 1fr); }
  .pbx-root.show-main .pbx-aside { display: none; }
  .pbx-root:not(.show-main) .pbx-main { display: none; }
  .pbx-back { display: inline-flex; }
  .pbx-panel { padding: 12px; }
}
</style>"""

# (#241) in a briefing opens the document dialog.
_DOC_JS = (
    "<script>document.addEventListener('click',function(e){"
    "var a=e.target.closest&&e.target.closest('.pbx-md a[href^=\"#pbdoc-\"]');if(!a)return;"
    "e.preventDefault();emitEvent('" + _DOC_EVENT + "',parseInt(a.getAttribute('href').slice(7),10));"
    "});</script>"
)


def _doc_links(md: str) -> str:
    return re.sub(r"\(#(\d+)\)", r"[(#\1)](#pbdoc-\1)", md or "")


def _tool_groups() -> list[tuple[str, str]]:
    from app_ui.pages.chat import _TOOL_GROUPS

    return [(name, icon) for name, icon, _tools in _TOOL_GROUPS]


@ui.page("/experts")
async def experts_page(e: str = "", view: str = "experts"):
    if not require_auth():
        return
    page_layout()
    _ = get_translator()
    ui.add_head_html(_CSS)
    ui.add_head_html(_DOC_JS)

    from app_ui.document_dialog import create_document_dialog
    from app_ui.model_picker import model_select
    from config.settings import settings
    from models.result_document import DocumentResult
    from services import briefings, experts, skills
    from services.expert_scope import scoped_client
    from services.model_registry import get_models
    from services.session_auth import get_session_token
    from services.user_prefs import get_pref, set_pref
    from vault.notes import NoteConflict

    user: str = ng_app.storage.user.get("paperless_user", "")
    token = get_session_token() or ""
    try:
        await experts.ensure_universal(user, token)
    except Exception:
        log.warning("could not create the Universal expert", exc_info=True)
        ui.notify(_("The vault is not writable — experts cannot be saved."), type="negative")

    models = [m for m in get_models(user, token) if m.get("enabled", True)] if token else []
    chat_model = get_pref("chat_model_name", "") or (models[0]["name"] if models else "")
    group_labels = {
        "documents": _("Documents"), "email": _("Email"), "calendar": _("Calendar"), "web": _("Web"),
        "calculate": _("Calc"), "visual": _("Visual"), "memory": _("Memory"), "vault": _("Vault"),
        "create": _("Create"), "document_notes": _("Document notes"),
    }
    open_document, _dlg = create_document_dialog()

    async def _open_doc(doc_id: int) -> None:
        ex = experts.get_or_universal(user, st["sel"])
        try:
            doc = await scoped_client(settings.paperless_url, token, None if ex.builtin else ex.scope).get_document(doc_id)
        except Exception:
            ui.notify(_("Document #{id} not found or no access.").format(id=doc_id), type="warning")
            return
        await open_document(DocumentResult(document=doc))

    ui.on(_DOC_EVENT, lambda ev: _open_doc(int(ev.args)))

    st: dict = {
        "view": view if view in ("experts", "skills") else "experts",
        "sel": e or experts.UNIVERSAL_ID,
        "skill": "",
        "tab": "profile",
        "main": bool(e),
        "counts": {},
        "total": None,
    }

    # ── Shell ─────────────────────────────────────────────────────────────────
    root = ui.element("div").classes("pbx-root" + (" show-main" if st["main"] else ""))
    with root:
        aside = ui.element("div").classes("pbx-aside")
        main = ui.element("div").classes("pbx-main")

    def _show_main(on: bool) -> None:
        st["main"] = on
        root.classes(add="show-main") if on else root.classes(remove="show-main")

    # ── Aside ────────────────────────────────────────────────────────────────
    @ui.refreshable
    def aside_body() -> None:
        with ui.row().classes("w-full items-center gap-2 no-wrap").style("padding:10px 12px 6px;"):
            ui.toggle({"experts": _("Experts"), "skills": _("Skills")}, value=st["view"],
                      on_change=lambda ev: _set_view(ev.value)).props(
                "dense no-caps unelevated toggle-color=primary").classes("flex-1")
            if st["view"] == "experts":
                ui.button(icon="add", on_click=_new_expert).props("flat dense round").classes(
                    "card-action-btn").tooltip(_("New expert"))
            else:
                ui.button(icon="add", on_click=_new_skill).props("flat dense round").classes(
                    "card-action-btn").tooltip(_("New skill"))
                ui.button(icon="auto_awesome", on_click=_wizard).props("flat dense round").classes(
                    "card-action-btn").tooltip(_("Skill wizard"))
        with ui.element("div").classes("pbx-scroll"):
            if st["view"] == "experts":
                for ex in experts.list_experts(user):
                    b = briefings.load(user, ex.expert_id)
                    dot = "current" if b else ""
                    n = st["counts"].get(ex.expert_id)
                    if ex.builtin:
                        sub = _("Built in · all documents")
                    elif ex.scope.unrestricted:
                        sub = _("All documents")
                    else:
                        sub = _("{n} documents").format(n=n) if n is not None else _("Narrowed access")  # TODO i18n-plural
                    if experts.default_id(user) == ex.expert_id:
                        sub += " · " + _("default")
                    with ui.element("div").classes("pbx-item" + (" is-active" if ex.expert_id == st["sel"] else "")).on(
                        "click", lambda _e, i=ex.expert_id: _select(i)
                    ):
                        ui.icon(ex.icon, size="sm").classes("flex-shrink-0")
                        with ui.column().classes("gap-0 flex-1").style("min-width:0;"):
                            ui.label(ex.name).classes("nm w-full")
                            ui.label(sub).classes("sub w-full")
                        ui.element("div").classes(f"pbx-dot {dot}").tooltip(
                            _("Briefing") if b else _("No briefing"))
            else:
                own = skills.list_own(user)
                ui.label(_("My skills")).classes("pbx-label").style("padding:8px 14px 2px;")
                if not own:
                    ui.label(_("No skills yet. Write one, or let the wizard help.")).classes("pbx-hint").style(
                        "padding:4px 14px;")
                for sk in own:
                    _skill_item(sk, f"own:{sk.skill_id}")
                shared = skills.list_shared_by_others(user)
                if shared:
                    ui.label(_("Shared by others")).classes("pbx-label").style("padding:12px 14px 2px;")
                    for sk in shared:
                        _skill_item(sk, f"shared:{sk.owner}/{sk.skill_id}")

    def _skill_item(sk, key: str) -> None:
        with ui.element("div").classes("pbx-item" + (" is-active" if st["skill"] == key else "")).on(
            "click", lambda _e, k=key: _select_skill(k)
        ):
            ui.icon("public" if sk.common else "lock", size="xs").classes("flex-shrink-0")
            with ui.column().classes("gap-0 flex-1").style("min-width:0;"):
                ui.label(sk.name).classes("nm w-full")
                ui.label((sk.description or "–") if key.startswith("own:") else _("by {owner}").format(owner=sk.owner)).classes(
                    "sub w-full")

    def _set_view(v: str) -> None:
        st["view"] = v
        aside_body.refresh()
        main_body.refresh()

    def _select(eid: str) -> None:
        st["sel"], st["view"] = eid, "experts"
        _show_main(True)
        aside_body.refresh()
        main_body.refresh()

    def _select_skill(key: str) -> None:
        st["skill"], st["view"] = key, "skills"
        _show_main(True)
        aside_body.refresh()
        main_body.refresh()

    # ── Main ─────────────────────────────────────────────────────────────────
    @ui.refreshable
    def main_body() -> None:
        if st["view"] == "skills":
            _skill_editor()
            return
        ex = experts.get(user, st["sel"]) or experts.get_or_universal(user, experts.UNIVERSAL_ID)
        st["sel"] = ex.expert_id
        with ui.row().classes("w-full items-center gap-2 no-wrap").style(
            "padding:10px 14px; border-bottom:1px solid var(--c-border); flex-shrink:0;"):
            ui.button(icon="arrow_back", on_click=lambda: _show_main(False)).props("flat dense round").classes(
                "card-action-btn pbx-back")
            ui.icon(ex.icon, size="md").classes("text-gray-300")
            with ui.column().classes("gap-0 flex-1").style("min-width:0;"):
                ui.label(ex.name).classes("text-base font-semibold w-full").style(
                    "color:var(--c-text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;")
                ui.label(_("Built-in expert — all documents, all tools") if ex.builtin else _("Expert")).classes(
                    "pbx-hint w-full")
            ui.button(_("Chat"), icon="chat", on_click=lambda i=ex.expert_id: ui.navigate.to(f"/chat?expert={i}")).props(
                "unelevated dense no-caps").classes("bg-primary text-white")
            if not ex.builtin:
                with ui.button(icon="more_vert").props("flat dense round").classes("card-action-btn"):
                    with ui.menu():
                        ui.menu_item(_("Delete expert"), on_click=lambda x=ex: _confirm_delete(x))
        tabs = [("profile", _("Profile")), ("skills", _("Skills")), ("briefing", _("Briefing"))]
        if not ex.builtin:
            tabs[1:1] = [("access", _("Access")), ("tools", _("Tools"))]
        if st["tab"] not in {k for k, _l in tabs}:
            st["tab"] = "profile"
        with ui.element("div").classes("pbx-tabs"):
            for key, label in tabs:
                tab = ui.element("div").classes("pbx-tab" + (" is-active" if key == st["tab"] else ""))
                tab.on("click", lambda _e, k=key: _set_tab(k))
                with tab:
                    ui.label(label)
        with ui.element("div").classes("pbx-scroll"):
            with ui.column().classes("pbx-panel gap-3"):
                {"profile": _profile, "access": _access, "tools": _tools, "skills": _skills_tab,
                 "briefing": _briefing}[st["tab"]](ex)

    def _set_tab(k: str) -> None:
        st["tab"] = k
        main_body.refresh()

    # ── Profile ──────────────────────────────────────────────────────────────
    def _profile(ex) -> None:
        sha = {"v": ex.sha}
        with ui.element("div").classes("pbx-card"):
            # One default at a time: ticking it here moves it from the other.
            def _set_default(e) -> None:
                from services.user_prefs import set_pref

                set_pref("default_expert_id", ex.expert_id if e.value else experts.UNIVERSAL_ID)
                ui.notify(_("New chats start with {name}.").format(
                    name=ex.name if e.value else "Universal"), type="positive")
                aside_body.refresh()

            ui.checkbox(_("Use as default expert"), value=experts.default_id(user) == ex.expert_id,
                        on_change=_set_default).props("dense")
            ui.label(_("New chats, and a dossier's chat without an expert of its own, start with it. "
                       "Only one expert is the default.")).classes("pbx-hint")
        with ui.element("div").classes("pbx-card"):
            ui.label(_("Name")).classes("pbx-label")
            name = ui.input(value=ex.name).props("outlined dense" + (" readonly" if ex.builtin else "")).classes("w-full")
            if not ex.builtin:
                ui.label(_("Icon")).classes("pbx-label mt-2")
                icon_state = {"v": ex.icon}
                icons_row = ui.row().classes("gap-1 flex-wrap")

                def _icons() -> None:
                    icons_row.clear()
                    with icons_row:
                        for ic in experts.ICONS:
                            el = ui.element("div").classes("pbx-icon-btn" + (" is-active" if ic == icon_state["v"] else ""))
                            el.on("click", lambda _e, i=ic: (icon_state.update(v=i), _icons()))
                            with el:
                                ui.icon(ic, size="sm")

                _icons()
            else:
                icon_state = {"v": ex.icon}
        with ui.element("div").classes("pbx-card"):
            ui.label(_("Personality and instructions")).classes("pbx-label")
            ui.label(_(
                "How this expert talks and works: tone, format, what matters in this area, your "
                "vocabulary. It shapes the answers; it cannot switch off the rules that keep the "
                "assistant from inventing document IDs, amounts or dates."
            )).classes("pbx-hint")
            pers = ui.textarea(value=ex.personality, placeholder=_(
                "Example: You are my insurance advisor. Answer briefly, always name notice "
                "periods and deductibles, compare with market prices when asked.")).props(
                "outlined autogrow").classes("w-full").style("min-height:140px;")
            count = ui.label("").classes("pbx-hint self-end")

            def _count() -> None:
                n = len(pers.value or "")
                count.set_text(_("{used} / {max} characters").format(used=n, max=experts.MAX_PERSONALITY_CHARS))
                count.style("color:var(--c-warn)" if n > experts.MAX_PERSONALITY_CHARS else "color:var(--c-text-muted)")

            _count()
            pers.on_value_change(lambda _e: _count())
        with ui.element("div").classes("pbx-card"):
            ui.label(_("Default model")).classes("pbx-label")
            ui.label(_("Preselected in the chat when you pick this expert. You can still change it per conversation.")).classes(
                "pbx-hint")
            msel = model_select(models, ex.default_model, allow_empty=True, empty_label=_("— chat's choice —")).props(
                "outlined dense").classes("w-full")

        async def _save() -> None:
            cur = experts.get(user, ex.expert_id) or ex
            cur.name = name.value or ex.name
            cur.icon = icon_state["v"]
            cur.personality = pers.value or ""
            cur.default_model = msel.value or ""
            try:
                saved = await experts.save(user, cur, expected_sha=sha["v"])
            except experts.ExpertError as err:
                ui.notify(_("Another expert already has that name.") if str(err) == "duplicate"
                          else _("Give the expert a name."), type="warning")
                return
            except NoteConflict:
                ui.notify(_("The file changed meanwhile (Obsidian?). Reload the page and try again."), type="warning")
                return
            sha["v"] = saved.sha
            if len(cur.personality) >= experts.MAX_PERSONALITY_CHARS:
                ui.notify(_("Saved, but only the first {max} characters are used.").format(
                    max=experts.MAX_PERSONALITY_CHARS), type="warning")
            else:
                ui.notify(_("Saved."), type="positive")
            aside_body.refresh()

        with ui.row().classes("w-full justify-end"):
            ui.button(_("Save"), icon="save", on_click=_save).props("unelevated no-caps").classes("bg-primary text-white")

    # ── Access ───────────────────────────────────────────────────────────────
    async def _access_async(ex, host) -> None:
        pl = scoped_client(settings.paperless_url, token, None)
        try:
            corr, tags, types = await asyncio.gather(pl.get_correspondent_map(), pl.get_tag_map(),
                                                     pl.get_document_types_map())
        except Exception:
            host.clear()
            with host:
                ui.label(_("Paperless is not reachable.")).classes("pbx-hint")
            return
        current = await experts.scope_with_names(pl, ex.scope_meta)
        host.clear()
        from app_ui.multi_pick import MultiPick

        sels: dict = {}
        with host:
            ui.label(_(
                "“Only these” narrows the view: a field left empty does not restrict, filled fields all "
                "apply together, and within one field any value matches. “Never these” takes documents "
                "out, whatever else matches."
            )).classes("pbx-hint")
            for key, label, names in (("correspondents", _("Correspondents"), corr),
                                      ("document_types", _("Document types"), types),
                                      ("tags", _("Tags"), tags)):
                with ui.element("div").classes("pbx-card gap-1"):
                    with ui.row().classes("w-full items-center no-wrap gap-2"):
                        ui.label(label).classes("pbx-label flex-1")
                        hint = ui.label("").classes("pbx-hint")
                    for mode in ("", "exclude_"):
                        k = mode + key
                        opts = {i: n for i, n in sorted(names.items(), key=lambda kv: kv[1].casefold())}
                        for item in current.get(k) or []:
                            opts.setdefault(item["id"], item["name"] + " ⚠")
                        sel = MultiPick(opts, [it["id"] for it in current.get(k) or []],
                                        title=f"{label} — " + (_("only these") if not mode else _("never these")),
                                        empty=_("any") if not mode else _("none excluded"),
                                        on_change=lambda _v: _changed(), negative=bool(mode),
                                        label=_("Only these") if not mode else _("Never these"))
                        sels[k] = (sel, names, hint if not mode else None)

            def _hints() -> None:
                for key in ("correspondents", "document_types", "tags"):
                    inc, exc = sels[key][0].value, sels["exclude_" + key][0].value
                    parts = [_("{n} only").format(n=len(inc)) if inc else _("no restriction")]
                    if exc:
                        parts.append(_("{n} never").format(n=len(exc)))
                    sels[key][2].set_text(" · ".join(parts))

            with ui.element("div").classes("pbx-card"):
                with ui.row().classes("w-full items-center no-wrap gap-2"):
                    ui.icon("visibility", size="xs").classes("text-gray-400")
                    seen = ui.label(_("Counting …")).classes("text-sm flex-1").style("color:var(--c-text)")
                    ui.button(_("Save access"), icon="save", on_click=lambda: _save_access()).props(
                        "unelevated dense no-caps").classes("bg-primary text-white")
                from app_ui.doc_library import DocLibrary

                def _draft_scope():
                    from services.expert_scope import Scope

                    return Scope.from_meta({k: list(sel.value or []) for k, (sel, _n, _h) in sels.items()})

                lib = DocLibrary(lambda: scoped_client(settings.paperless_url, token, _draft_scope()), _open_doc)
                with ui.element("div").style("height:380px; display:flex; flex-direction:column; width:100%;"):
                    lib.render()

            pending = {"t": None}

            async def _recount() -> None:
                _hints()
                await lib.reload()
                n = len(lib._docs or [])
                if st["total"] is None:
                    try:
                        from pipelines.search import _drop_no_ingest

                        st["total"] = len(_drop_no_ingest(await scoped_client(settings.paperless_url, token, None).list_documents()))
                    except Exception:
                        st["total"] = 0
                seen.set_text(_("This expert can see {n} of {total} documents").format(n=n, total=st["total"]))

            def _changed(_e=None) -> None:
                _hints()
                if pending["t"] is not None:
                    pending["t"].cancel()
                with host:  # not in a picker's dialog, which is deleted on close
                    pending["t"] = ui.timer(0.8, _recount, once=True)

            async def _save_access() -> None:
                cur = experts.get(user, ex.expert_id) or ex
                cur.scope_meta = {key: [{"id": int(i), "name": names.get(int(i), str(i))} for i in (sel.value or [])]
                                  for key, (sel, names, _h) in sels.items() if sel.value}
                try:
                    saved = await experts.save(user, cur, expected_sha=cur.sha)
                except NoteConflict:
                    ui.notify(_("The file changed meanwhile (Obsidian?). Reload the page and try again."), type="warning")
                    return
                st["counts"][saved.expert_id] = len(lib._docs or []) if lib.loaded else None
                ui.notify(_("Access saved."), type="positive")
                aside_body.refresh()

            await _recount()

    def _access(ex) -> None:
        host = ui.column().classes("w-full gap-3")
        with host:
            ui.spinner()
        ui.timer(0.01, lambda: _access_async(ex, host), once=True)

    # ── Tools ────────────────────────────────────────────────────────────────
    def _tools(ex) -> None:
        with ui.element("div").classes("pbx-card"):
            ui.label(_("Tools this expert may use")).classes("pbx-label")
            ui.label(_(
                "A switched-off group is never offered to the model, and the prompt does not mention it. "
                "Memory and notes stay on for every expert. The chat's own switches still apply on top."
            )).classes("pbx-hint")
            switches = {}
            for g, icon in _tool_groups():
                with ui.row().classes("w-full items-center no-wrap gap-2").style("padding:4px 0;"):
                    ui.icon(icon, size="xs").classes("text-gray-400")
                    ui.label(group_labels.get(g, g)).classes("text-sm flex-1").style("color:var(--c-text-2)")
                    sw = ui.switch(value=g not in ex.tools_off or g in experts.ALWAYS_ON_GROUPS).props("dense")
                    if g in experts.ALWAYS_ON_GROUPS:
                        sw.disable()
                        sw.tooltip(_("Always on"))
                    switches[g] = sw

        async def _save() -> None:
            cur = experts.get(user, ex.expert_id) or ex
            cur.tools_off = [g for g, sw in switches.items() if not sw.value and g not in experts.ALWAYS_ON_GROUPS]
            try:
                await experts.save(user, cur, expected_sha=cur.sha)
            except NoteConflict:
                ui.notify(_("The file changed meanwhile (Obsidian?). Reload the page and try again."), type="warning")
                return
            ui.notify(_("Saved."), type="positive")

        with ui.row().classes("w-full justify-end"):
            ui.button(_("Save"), icon="save", on_click=_save).props("unelevated no-caps").classes("bg-primary text-white")

    # ── Skills of an expert ──────────────────────────────────────────────────
    def _skills_tab(ex) -> None:
        assigned = list(ex.skills)
        own = {s.ref_for(user): s for s in skills.list_own(user)}
        shared = {s.ref_for(user): s for s in skills.list_shared_by_others(user)}
        catalogue = {**own, **shared}
        with ui.element("div").classes("pbx-card"):
            ui.label(_("Assigned skills")).classes("pbx-label")
            ui.label(_(
                "Each skill says when it applies. Few short skills go into the prompt as they are; "
                "more are listed and loaded on demand, so a small model's context stays free."
            )).classes("pbx-hint")
            box = ui.column().classes("w-full gap-1 mt-1")

            def _list() -> None:
                box.clear()
                with box:
                    if not assigned:
                        ui.label(_("No skills assigned.")).classes("pbx-hint")
                    for ref in list(assigned):
                        sk = catalogue.get(ref)
                        with ui.row().classes("w-full items-center no-wrap gap-2").style(
                            "border:1px solid var(--c-border); border-radius:8px; padding:6px 8px;"):
                            ui.icon("public" if (sk and sk.common) else "lock", size="xs").classes("text-gray-400")
                            with ui.column().classes("gap-0 flex-1").style("min-width:0;"):
                                ui.label(sk.name if sk else _("Skill no longer shared")).classes("text-sm w-full").style(
                                    "color:var(--c-text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;"
                                    + ("" if sk else "text-decoration:line-through;"))
                                ui.label((sk.description if sk else ref) or "–").classes("pbx-hint w-full").style(
                                    "overflow:hidden; text-overflow:ellipsis; white-space:nowrap;")
                            ui.button(icon="close", on_click=lambda r=ref: (assigned.remove(r), _list())).props(
                                "flat dense round size=sm").classes("card-action-btn")

            _list()
            options = {r: (s.name if r in own else f"{s.name} · {s.owner}") for r, s in catalogue.items()}
            with ui.row().classes("w-full items-center no-wrap gap-2 mt-2"):
                pick = ui.select(options, label=_("Add a skill"), with_input=True).props(
                    "outlined dense options-dense").classes("flex-1")

                def _add() -> None:
                    if pick.value and pick.value not in assigned:
                        assigned.append(pick.value)
                        _list()
                    pick.set_value(None)

                ui.button(icon="add", on_click=_add).props("flat dense round").classes("card-action-btn")
            ui.button(_("Open the skill library"), icon="library_books", on_click=lambda: _set_view("skills")).props(
                "flat dense no-caps").classes("pb-accent-text self-start")

        async def _save() -> None:
            cur = experts.get(user, ex.expert_id) or ex
            cur.skills = list(assigned)
            try:
                await experts.save(user, cur, expected_sha=cur.sha)
            except NoteConflict:
                ui.notify(_("The file changed meanwhile (Obsidian?). Reload the page and try again."), type="warning")
                return
            ui.notify(_("Saved."), type="positive")

        with ui.row().classes("w-full justify-end"):
            ui.button(_("Save"), icon="save", on_click=_save).props("unelevated no-caps").classes("bg-primary text-white")

    # ── Briefing ─────────────────────────────────────────────────────────────
    def _briefing(ex) -> None:
        host = ui.column().classes("w-full gap-3")
        _render_briefing(ex, host)

    def _render_briefing(ex, host) -> None:
        host.clear()
        b = briefings.load(user, ex.expert_id)
        j = briefings.job(user, ex.expert_id)
        with host:
            with ui.element("div").classes("pbx-card"):
                with ui.row().classes("w-full items-center no-wrap gap-2"):
                    ui.element("div").classes("pbx-dot " + ("current" if b else ""))
                    if b:
                        ui.label(_("Built {when} · {model} · {n} of {total} documents studied · {s} without "
                                   "extraction skipped").format(
                            when=b.built[:10], model=b.model or "?", n=len(b.studied_ids), total=b.total_docs or "?",
                            s=b.skipped)).classes(
                            "text-sm flex-1").style("color:var(--c-text)")
                    else:
                        ui.label(_("No briefing yet")).classes("text-sm flex-1").style("color:var(--c-text)")
                ui.label(_(
                    "A map, not evidence: the expert learns the parties, terms and time range of its "
                    "documents, so its first searches hit. It still reads documents before answering, "
                    "and cites them, never the briefing."
                )).classes("pbx-hint")
                stale_lbl = ui.label("").classes("text-xs").style("color:var(--c-warn)")
                if b and b.edited:
                    ui.label(_("Edited by you")).classes("pbx-hint")

                if j and j.status == "running":
                    phase = {"listing": _("Listing documents …"), "reading": _("Reading summaries …"),
                             "writing": _("Writing the briefing …"), "checking": _("Checking …")}.get(j.phase, "")
                    with ui.row().classes("w-full items-center gap-2 no-wrap mt-2"):
                        ui.spinner(size="sm")
                        ui.label(f"{phase} {j.done}/{j.total or '?'}").classes("text-sm flex-1").style(
                            "color:var(--c-text-2)")
                        ui.button(_("Stop"), icon="stop", on_click=lambda: briefings.stop(user, ex.expert_id)).props(
                            "flat dense no-caps")
                    ui.timer(1.5, lambda: _render_briefing(ex, host), once=True)
                else:
                    if j and j.status == "failed":
                        ui.label(_("The last study failed: {err}").format(err=j.error)).classes("text-xs").style(
                            "color:var(--c-warn)")
                    elif j and j.status == "stopped":
                        ui.label(_("Stopped. The previous briefing is unchanged.")).classes("pbx-hint")
                    model0 = get_pref("briefing_model", "") or chat_model
                    with ui.row().classes("w-full items-center gap-2 mt-2"):
                        msel = model_select(models, model0 if any(m["name"] == model0 for m in models) else chat_model,
                                            label=_("Model")).props("outlined dense").classes("flex-1").style(
                            "min-width:12rem;")
                        cost = ui.label("").classes("pbx-hint")

                    async def _estimate() -> None:
                        try:
                            from pipelines.search import _drop_no_ingest

                            docs = _drop_no_ingest(await scoped_client(
                                settings.paperless_url, token, None if ex.builtin else ex.scope).list_documents())
                        except Exception:
                            cost.set_text("")
                            return
                        sel_n = len(briefings.select_for_study(docs))
                        if sel_n >= len(docs):
                            cost.set_text(_("Studies all {n} documents it sees · about {c} model calls").format(
                                n=sel_n, c=briefings.estimate_calls(sel_n)))
                        else:
                            cost.set_text(_(
                                "Sees {total} documents, studies {n} of them (the newest {newest} and up to "
                                "{per} per correspondent) · about {c} model calls").format(
                                total=len(docs), n=sel_n, newest=briefings.SAMPLE_NEWEST,
                                per=briefings.SAMPLE_PER_CORRESPONDENT, c=briefings.estimate_calls(sel_n)))
                        s = briefings.staleness(b, docs, ex.scope_meta)
                        if b and s["state"] == "outdated":
                            stale_lbl.set_text(_("{n} new documents since the briefing").format(n=s["new"]))
                        elif b and s["state"] == "scope_changed":
                            stale_lbl.set_text(_("Access changed since the briefing — rebuild it"))
                        if b and s["state"] in ("outdated", "scope_changed"):
                            st["counts"][f"stale:{ex.expert_id}"] = True

                    ui.timer(0.05, _estimate, once=True)

                    def _go(incremental: bool) -> None:
                        if not msel.value:
                            ui.notify(_("No model configured — add one in Settings > AI Models."), type="negative")
                            return
                        set_pref("briefing_model", msel.value)
                        briefings.start(user, token, experts.get(user, ex.expert_id) or ex, msel.value,
                                        get_pref("language", "en"), incremental=incremental)
                        _render_briefing(ex, host)

                    with ui.row().classes("w-full justify-end gap-2 mt-1"):
                        if b:
                            ui.button(_("Update"), icon="update", on_click=lambda: _go(True)).props("flat no-caps")
                            ui.button(_("Rebuild"), icon="refresh", on_click=lambda: _go(False)).props(
                                "unelevated no-caps").classes("bg-primary text-white")
                        else:
                            ui.button(_("Study documents"), icon="school", on_click=lambda: _go(False)).props(
                                "unelevated no-caps").classes("bg-primary text-white")
            if b is None:
                return
            m = b.map or {}
            with ui.element("div").classes("pbx-card"):
                with ui.row().classes("w-full items-center no-wrap gap-2"):
                    ui.label(_("Archive map")).classes("pbx-label flex-1")
                    ui.label(_("made by code")).classes("pbx-hint")
                ui.label(_("{n} documents, {first} – {last}").format(
                    n=m.get("total", 0), first=m.get("first") or "?", last=m.get("last") or "?")).classes(
                    "text-sm").style("color:var(--c-text)")
                years = m.get("years") or []
                if years:
                    top = max(n for _y, n in years) or 1
                    with ui.row().classes("items-end gap-3 no-wrap").style("overflow-x:auto; max-width:100%;"):
                        with ui.element("div").classes("pbx-bars"):
                            for y, n in years:
                                ui.element("div").style(f"height:{max(3, int(34 * n / top))}px").tooltip(f"{y}: {n}")
                        ui.label(f"{years[0][0]}–{years[-1][0]}").classes("pbx-hint")
                for key, label in (("correspondents", _("Correspondents")), ("document_types", _("Document types")),
                                   ("tags", _("Tags"))):
                    pairs = m.get(key) or []
                    if pairs:
                        ui.label(label).classes("pbx-hint mt-1")
                        with ui.row().classes("gap-0 flex-wrap"):
                            for name, n in pairs:
                                ui.html(f'<span class="pbx-chip">{_esc(name)} <span class="n">{n}</span></span>',
                                        sanitize=False)
                for sr in m.get("series") or []:
                    ui.label(_("Recurring: {c} · {t} — {n} documents since {since}").format(
                        c=sr["correspondent"], t=sr["type"], n=sr["count"], since=sr["since"])).classes(
                        "text-xs").style("color:var(--c-text-2)")
            orient = briefings.orientation_markdown(b.orientation, _)
            with ui.element("div").classes("pbx-card"):
                with ui.row().classes("w-full items-center no-wrap gap-2"):
                    ui.label(_("Orientation")).classes("pbx-label flex-1")
                    ui.label(_("written by the model, checked by code")).classes("pbx-hint")
                if orient:
                    ui.markdown(_doc_links(orient)).classes("pbx-md w-full")
                else:
                    ui.label(_("Nothing survived the checks — the map above is all this expert gets.")).classes(
                        "pbx-hint")
            if b.dropped:
                reasons = {"unsupported_number": _("number not in the documents"), "unknown_id": _("unknown document"),
                           "no_source": _("no document cited"), "bad_date": _("date not valid"), "empty": _("empty")}
                with ui.expansion(_("Checks: {n} statements dropped").format(n=len(b.dropped)), icon="rule").classes(
                    "w-full pbx-card").props("dense"):
                    for d in b.dropped:
                        with ui.row().classes("w-full no-wrap gap-2 items-start"):
                            ui.label(reasons.get(d.get("reason"), d.get("reason", ""))).classes("pbx-hint").style(
                                "flex-shrink:0; width:11rem;")
                            ui.label(d.get("text", "")).classes("text-xs flex-1").style(
                                "color:var(--c-text-2); overflow-wrap:anywhere;")
            with ui.row().classes("w-full justify-end gap-2"):
                async def _del() -> None:
                    await briefings.delete(user, ex.expert_id)
                    _render_briefing(ex, host)
                    aside_body.refresh()

                ui.button(_("Delete briefing"), icon="delete_outline", on_click=_del).props("flat no-caps dense").classes(
                    "text-gray-400")

    # ── Skill editor ─────────────────────────────────────────────────────────
    def _skill_editor() -> None:
        key = st["skill"]
        sk = None
        if key.startswith("own:"):
            sk = skills.get_own(user, key[4:])
        elif key.startswith("shared:"):
            sk = skills.resolve(user, key)
        with ui.row().classes("w-full items-center gap-2 no-wrap").style(
            "padding:10px 14px; border-bottom:1px solid var(--c-border); flex-shrink:0;"):
            ui.button(icon="arrow_back", on_click=lambda: _show_main(False)).props("flat dense round").classes(
                "card-action-btn pbx-back")
            ui.icon("library_books", size="md").classes("text-gray-300")
            ui.label(sk.name if sk else _("Skills")).classes("text-base font-semibold flex-1 w-full").style(
                "color:var(--c-text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;")
            ui.button(_("Wizard"), icon="auto_awesome", on_click=_wizard).props("flat dense no-caps")
        with ui.element("div").classes("pbx-scroll"):
            with ui.column().classes("pbx-panel gap-3"):
                if sk is None:
                    with ui.element("div").classes("pbx-card"):
                        ui.label(_("Skills are instruction packs: when to apply them, and how. One skill can serve "
                                   "many experts. Share one and other users can use it read-only.")).classes("text-sm").style(
                            "color:var(--c-text-2)")
                        with ui.row().classes("gap-2 mt-2"):
                            ui.button(_("New skill"), icon="add", on_click=_new_skill).props("flat no-caps")
                            ui.button(_("Let the wizard help"), icon="auto_awesome", on_click=_wizard).props(
                                "unelevated no-caps").classes("bg-primary text-white")
                    return
                mine = sk.owner == user
                with ui.element("div").classes("pbx-card"):
                    ui.label(_("Name")).classes("pbx-label")
                    name = ui.input(value=sk.name).props("outlined dense" + ("" if mine else " readonly")).classes("w-full")
                    ui.label(_("When to use it")).classes("pbx-label mt-2")
                    desc = ui.input(value=sk.description, placeholder=_("Use when …")).props(
                        "outlined dense" + ("" if mine else " readonly")).classes("w-full")
                    ui.label(_("Instructions")).classes("pbx-label mt-2")
                    body = ui.textarea(value=sk.body).props("outlined autogrow" + ("" if mine else " readonly")).classes(
                        "w-full").style("min-height:180px; font-family:var(--pb-mono, monospace);")
                    lint_box = ui.column().classes("w-full gap-1")

                    def _lint() -> None:
                        lint_box.clear()
                        with lint_box:
                            _render_lint(skill_lint(name.value, desc.value, body.value))

                    _lint()
                    if mine:
                        body.on_value_change(lambda _e: _lint())
                if mine:
                    with ui.element("div").classes("pbx-card"):
                        common = ui.switch(_("Share with other users (read-only for them)"), value=sk.common).props("dense")
                        used = skills.used_by(user, sk)
                        ui.label(_("Used by: {list}").format(list=", ".join(used)) if used else _("Not assigned to an expert yet.")).classes(
                            "pbx-hint")

                    async def _save() -> None:
                        cur = skills.get_own(user, sk.skill_id) or sk
                        cur.name, cur.description, cur.body, cur.common = name.value, desc.value, body.value, bool(common.value)
                        try:
                            saved = await skills.save(user, cur, expected_sha=sk.sha)
                        except skills.SkillError as err:
                            ui.notify(_("Another skill already has that name.") if str(err) == "duplicate"
                                      else _("Give the skill a name."), type="warning")
                            return
                        except NoteConflict:
                            ui.notify(_("The file changed meanwhile (Obsidian?). Reload the page and try again."), type="warning")
                            return
                        st["skill"] = f"own:{saved.skill_id}"
                        ui.notify(_("Saved."), type="positive")
                        aside_body.refresh()
                        main_body.refresh()

                    async def _delete() -> None:
                        await skills.delete(user, sk.skill_id)
                        st["skill"] = ""
                        aside_body.refresh()
                        main_body.refresh()

                    with ui.row().classes("w-full justify-end gap-2"):
                        ui.button(_("Delete"), icon="delete_outline", on_click=_delete).props("flat no-caps").classes(
                            "text-gray-400")
                        ui.button(_("Save"), icon="save", on_click=_save).props("unelevated no-caps").classes(
                            "bg-primary text-white")
                else:
                    async def _dup() -> None:
                        new = await skills.duplicate(user, sk)
                        st["skill"] = f"own:{new.skill_id}"
                        ui.notify(_("Copied to your skills."), type="positive")
                        aside_body.refresh()
                        main_body.refresh()

                    with ui.row().classes("w-full items-center gap-2"):
                        ui.label(_("Shared by {owner}. Assign it to your experts as it is, or copy it to change it.").format(
                            owner=sk.owner)).classes("pbx-hint flex-1")
                        ui.button(_("Duplicate"), icon="content_copy", on_click=_dup).props("flat no-caps")

    def skill_lint(name: str, desc: str, body: str) -> list[dict]:
        from services.skill_wizard import lint

        return lint(name, desc, body)

    def _render_lint(findings: list[dict]) -> None:
        texts = {
            "no_name": _("The skill has no name."),
            "no_description": _("Say when to use it — that is how the model decides."),
            "description_trigger": _("Tip: start the description with \"Use when …\"."),
            "too_long": _("Too long — only the first {n} characters are used."),
            "unknown_tool": _("No such tool: {t}. The model would try to call it and fail."),
            "tool_off": _("This tool is switched off for an expert using the skill: {t}"),
        }
        for f in findings:
            with ui.row().classes("w-full items-start gap-2 no-wrap"):
                ui.icon("warning" if f["level"] == "warn" else "info", size="xs").style(
                    "color:var(--c-warn)" if f["level"] == "warn" else "color:var(--c-text-muted)")
                ui.label(texts.get(f["code"], f["code"]).format(n=f["text"], t=f["text"])).classes("text-xs").style(
                    "color:var(--c-text-2); overflow-wrap:anywhere;")

    # ── Actions ──────────────────────────────────────────────────────────────
    def _new_expert() -> None:
        with ui.dialog() as dlg, ui.card().style("background:var(--c-surface); width:min(420px,94vw);"):
            ui.label(_("New expert")).classes("text-base font-semibold").style("color:var(--c-text)")
            name = ui.input(_("Name"), placeholder=_("e.g. Insurance, Taxes 2026, Flat")).props("outlined dense autofocus").classes(
                "w-full")

            async def _ok() -> None:
                try:
                    ex = await experts.create(user, name.value or "")
                except experts.ExpertError as err:
                    ui.notify(_("Another expert already has that name.") if str(err) == "duplicate"
                              else _("Give the expert a name."), type="warning")
                    return
                dlg.close()
                st["tab"] = "access"
                _select(ex.expert_id)

            name.on("keydown.enter", _ok)
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button(_("Cancel"), on_click=dlg.close).props("flat no-caps")
                ui.button(_("Create"), on_click=_ok).props("unelevated no-caps").classes("bg-primary text-white")
        dlg.open()

    def _confirm_delete(ex) -> None:
        with ui.dialog() as dlg, ui.card().style("background:var(--c-surface); width:min(420px,94vw);"):
            ui.label(_("Delete expert {name}?").format(name=ex.name)).classes("text-base font-semibold").style(
                "color:var(--c-text)")
            ui.label(_("Its file and briefing are removed from your vault. Conversations with it stay; "
                       "they continue with the Universal expert.")).classes("pbx-hint")

            async def _ok() -> None:
                await briefings.delete(user, ex.expert_id)
                await experts.delete(user, ex.expert_id)
                dlg.close()
                st["sel"] = experts.UNIVERSAL_ID
                aside_body.refresh()
                main_body.refresh()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button(_("Cancel"), on_click=dlg.close).props("flat no-caps")
                ui.button(_("Delete"), on_click=_ok).props("unelevated no-caps color=negative")
        dlg.open()

    def _new_skill() -> None:
        with ui.dialog() as dlg, ui.card().style("background:var(--c-surface); width:min(420px,94vw);"):
            ui.label(_("New skill")).classes("text-base font-semibold").style("color:var(--c-text)")
            name = ui.input(_("Name")).props("outlined dense autofocus").classes("w-full")

            async def _ok() -> None:
                try:
                    sk = await skills.create(user, name.value or "")
                except skills.SkillError as err:
                    ui.notify(_("Another skill already has that name.") if str(err) == "duplicate"
                              else _("Give the skill a name."), type="warning")
                    return
                dlg.close()
                _select_skill(f"own:{sk.skill_id}")

            name.on("keydown.enter", _ok)
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button(_("Cancel"), on_click=dlg.close).props("flat no-caps")
                ui.button(_("Create"), on_click=_ok).props("unelevated no-caps").classes("bg-primary text-white")
        dlg.open()

    def _wizard() -> None:
        from app_ui.skill_wizard_dialog import open_wizard

        async def _saved(sk) -> None:
            _select_skill(f"own:{sk.skill_id}")

        open_wizard(user=user, token=token, models=models, default_model=chat_model, on_saved=_saved)

    # ── Build ────────────────────────────────────────────────────────────────
    with aside:
        aside_body()
    with main:
        main_body()

    async def _counts() -> None:
        """Document counts for narrowed experts, one after the other."""
        from pipelines.search import _drop_no_ingest

        for ex in experts.list_experts(user):
            if ex.builtin or ex.scope.unrestricted or ex.expert_id in st["counts"]:
                continue
            try:
                docs = await scoped_client(settings.paperless_url, token, ex.scope).list_documents()
                st["counts"][ex.expert_id] = len(_drop_no_ingest(docs))
            except Exception:
                continue
        aside_body.refresh()

    if token:
        ui.timer(0.3, _counts, once=True)


def _esc(s) -> str:
    import html

    return html.escape(str(s))

