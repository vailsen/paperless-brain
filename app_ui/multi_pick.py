"""A multi-select for long lists that works on a phone.

A Quasar `q-select` with hundreds of options opens a menu that covers the
screen; on a phone the only way out was a tap outside it, and the back
gesture left the page instead. This picker shows the chosen values as chips
and opens a dialog — search field, checkboxes, a "Done" button — that the
back gesture closes like any app sheet (a `history.pushState` entry while it
is open, popped on close).
"""

from __future__ import annotations

from collections.abc import Callable

from nicegui import context, ui

from i18n import get_translator

_BACK_EVENT = "pb_pick_back"
_JS = """<script>
if(!window.__pbPickBack){window.__pbPickBack=1;
 window.addEventListener('popstate',function(){emitEvent('""" + _BACK_EVENT + """');});}
</script>"""

_CSS = """
.pb-pick-chips{display:flex;flex-wrap:wrap;gap:6px;align-items:center;min-width:0}
.pb-pick-chip{display:inline-flex;align-items:center;gap:2px;max-width:100%;font-size:.78rem;
  padding:2px 4px 2px 10px;border-radius:999px;border:1px solid var(--c-border);color:var(--c-text-2);
  background:var(--c-surface-2)}
.pb-pick-chip .t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.pb-pick-chip.is-not{text-decoration:line-through;text-decoration-color:var(--c-text-muted)}
.pb-pick-list{flex:1;min-height:0;overflow-y:auto;overflow-x:hidden;width:100%}
.pb-pick-row{display:flex;align-items:center;gap:8px;padding:2px 4px;border-radius:6px;min-width:0}
.pb-pick-row:hover{background:var(--c-surface-2)}
.pb-pick-row .q-checkbox__label{overflow-wrap:anywhere}
"""


def _install() -> None:
    """Once per page: the popstate → server event, and the one handler that
    closes whichever picker is open."""
    client = context.client
    if getattr(client, "_pb_pick_installed", False):
        return
    client._pb_pick_installed = True
    client._pb_pick_open = None
    ui.add_head_html(_JS)
    ui.add_css(_CSS)

    def _back(_e=None) -> None:
        dlg = getattr(client, "_pb_pick_open", None)
        client._pb_pick_open = None
        if dlg is not None:
            dlg.close()

    ui.on(_BACK_EVENT, _back)


class MultiPick:
    """Chips + an "Edit" button; `value` is the list of chosen keys."""

    def __init__(self, options: dict, value: list, *, title: str, empty: str,
                 on_change: Callable[[list], None] | None = None, negative: bool = False, label: str = ""):
        _install()
        self._ = get_translator()
        self.options = options
        self.value = [v for v in value if v in options]
        self.title = title
        self.empty = empty
        self.on_change = on_change
        self.negative = negative
        with ui.row().classes("w-full items-center gap-2 no-wrap"):
            if label:
                ui.label(label).classes("pbx-hint flex-shrink-0").style("width:5.5rem;")
            self._chips = ui.element("div").classes("pb-pick-chips flex-1")
            ui.button(icon="edit", on_click=self.open).props("flat dense round size=sm").classes(
                "card-action-btn flex-shrink-0").tooltip(self._("Choose"))
        self._render_chips()

    # ── chips ────────────────────────────────────────────────────────────────
    def _render_chips(self) -> None:
        self._chips.clear()
        with self._chips:
            if not self.value:
                ui.label(self.empty).classes("pbx-hint").style("cursor:pointer").on("click", lambda _e: self.open())
            for v in self.value:
                with ui.element("span").classes("pb-pick-chip" + (" is-not" if self.negative else "")):
                    ui.label(str(self.options.get(v, v))).classes("t")
                    ui.button(icon="close", on_click=lambda v=v: self._remove(v)).props(
                        "flat dense round size=xs").classes("card-action-btn")

    def _remove(self, v) -> None:
        self.set_value([x for x in self.value if x != v])

    def set_value(self, value: list) -> None:
        self.value = [v for v in value if v in self.options]
        self._render_chips()
        if self.on_change:
            self.on_change(self.value)

    # ── dialog ───────────────────────────────────────────────────────────────
    def open(self) -> None:
        _ = self._
        client = context.client
        chosen = set(self.value)
        state = {"q": ""}
        with ui.dialog() as dlg, ui.card().classes("gap-2").style(
                "background:var(--c-surface); width:min(520px,100vw); max-width:100vw; height:min(640px,85vh);"
                "display:flex; flex-direction:column; flex-wrap:nowrap; padding:12px;"):
            with ui.row().classes("w-full items-center no-wrap gap-2"):
                ui.label(self.title).classes("text-base font-semibold flex-1 min-w-0 truncate").style(
                    "color:var(--c-text)")
                count = ui.label("").classes("pbx-hint flex-shrink-0")
                ui.button(_("Done"), on_click=lambda: dlg.close()).props("unelevated dense no-caps").classes(
                    "bg-primary text-white flex-shrink-0")
            search = ui.input(placeholder=_("Search")).props("outlined dense clearable debounce=150").classes("w-full")
            with search.add_slot("prepend"):
                ui.icon("search", size="xs")
            host = ui.element("div").classes("pb-pick-list")

            def _count() -> None:
                count.set_text(_("{n} chosen").format(n=len(chosen)))

            def _toggle(k, on: bool) -> None:
                chosen.add(k) if on else chosen.discard(k)
                _count()

            def _rows() -> None:
                host.clear()
                q = state["q"].casefold()
                keys = [k for k, label in self.options.items() if not q or q in str(label).casefold()]
                keys.sort(key=lambda k: (k not in chosen, str(self.options[k]).casefold()))
                with host:
                    if not keys:
                        ui.label(_("Nothing matches.")).classes("pbx-hint")
                    for k in keys[:400]:
                        with ui.element("div").classes("pb-pick-row"):
                            ui.checkbox(str(self.options[k]), value=k in chosen,
                                        on_change=lambda e, k=k: _toggle(k, e.value)).props("dense").classes(
                                "text-sm min-w-0")
                    if len(keys) > 400:
                        ui.label(_("{n} more — narrow the search.").format(n=len(keys) - 400)).classes("pbx-hint")

            def _search(e) -> None:
                state["q"] = (e.value or "").strip()
                _rows()

            search.on_value_change(_search)
            with ui.row().classes("w-full justify-between items-center"):
                ui.button(_("Clear all"), on_click=lambda: (chosen.clear(), _count(), _rows())).props(
                    "flat dense no-caps").classes("text-xs")
                ui.label(_("Back closes this list.")).classes("pbx-hint")
            _rows()
            _count()

        def _closed(e) -> None:
            if e.value:
                return
            if getattr(client, "_pb_pick_open", None) is dlg:
                # Closed by Done or a tap outside: drop the history entry the
                # dialog pushed (closed by Back, it is already gone).
                client._pb_pick_open = None
                ui.run_javascript("if(history.state&&history.state.pbPick)history.back();")
            new = [k for k in self.options if k in chosen]
            if new != self.value:
                self.set_value(new)
            dlg.delete()

        dlg.on_value_change(_closed)
        client._pb_pick_open = dlg
        ui.run_javascript("history.pushState({pbPick:1},'');")
        dlg.open()
