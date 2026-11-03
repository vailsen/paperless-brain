"""A multi-select for long lists that works on a phone.

A Quasar `q-select` with hundreds of options opens a menu that covers the
screen; on a phone the only way out was a tap outside it, and the back
gesture left the page instead. This picker shows the chosen values as chips
and opens a dialog — search field, checkboxes, a "Done" button — that the
back gesture closes like any app sheet (a `history.pushState` entry while it
is open, popped on close).
"""

from __future__ import annotations

import html
import json
from collections.abc import Callable

from nicegui import context, ui

from i18n import get_translator

_BACK_EVENT = "pb_pick_back"
_JS = """<script>
if(!window.__pbPickBack){window.__pbPickBack=1;
 window.addEventListener('popstate',function(){emitEvent('""" + _BACK_EVENT + """');});
 window.__pbPick={};
 window.__pbPickInit=function(uid,countText){
   function go(){
     var box=document.getElementById(uid), q=document.getElementById(uid+'-q'), n=document.getElementById(uid+'-n');
     if(!box){return setTimeout(go,50);}
     var set=new Set();
     box.querySelectorAll('input[type=checkbox]').forEach(function(c){if(c.checked)set.add(+c.dataset.i);});
     window.__pbPick[uid]=set;
     function count(){if(n)n.textContent=countText.replace('{n}',set.size);}
     box.addEventListener('change',function(e){var c=e.target;if(!c.dataset||c.dataset.i===undefined)return;
       if(c.checked)set.add(+c.dataset.i);else set.delete(+c.dataset.i);count();});
     if(q)q.addEventListener('input',function(){var t=q.value.trim().toLowerCase(),any=false;
       box.querySelectorAll('.pb-pick-row').forEach(function(r){var on=!t||r.dataset.t.indexOf(t)>=0;
         r.style.display=on?'':'none';if(on)any=true;});
       var none=box.querySelector('.pb-pick-none');if(none)none.style.display=any?'none':'';});
     count();
   }
   go();
 };
 window.__pbPickGet=function(uid){var s=window.__pbPick[uid];return s?Array.from(s):null;};
 window.__pbPickClear=function(uid){var box=document.getElementById(uid),s=window.__pbPick[uid];if(!box||!s)return;
   box.querySelectorAll('input[type=checkbox]').forEach(function(c){c.checked=false;});s.clear();
   var n=document.getElementById(uid+'-n');if(n)n.textContent=n.textContent.replace(/\\d+/,'0');};
}
</script>"""

_CSS = """
.pb-pick-chips{display:flex;flex-wrap:wrap;gap:6px;align-items:center;min-width:0}
.pb-pick-chip{display:inline-flex;align-items:center;gap:2px;max-width:100%;font-size:.78rem;
  padding:2px 4px 2px 10px;border-radius:999px;border:1px solid var(--c-border);color:var(--c-text-2);
  background:var(--c-surface-2)}
.pb-pick-chip .t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.pb-pick-chip.is-not{text-decoration:line-through;text-decoration-color:var(--c-text-muted)}
.pb-pick-list{flex:1;min-height:0;overflow-y:auto;overflow-x:hidden;width:100%}
.pb-pick-row{display:flex;align-items:center;gap:10px;padding:6px 6px;border-radius:6px;min-width:0;cursor:pointer;
  font-size:.875rem;color:var(--c-text)}
.pb-pick-row:hover{background:var(--c-surface-2)}
.pb-pick-row span{overflow-wrap:anywhere;min-width:0}
.pb-pick-row input{width:18px;height:18px;flex-shrink:0;accent-color:var(--c-accent);margin:0}
.pb-pick-q{width:100%;box-sizing:border-box;padding:8px 10px;border:1px solid var(--c-border);border-radius:6px;
  background:var(--c-surface);color:var(--c-text);font-size:.875rem;outline:none}
.pb-pick-q:focus{border-color:var(--c-accent)}
"""


def _install() -> None:
    """Once per page: the popstate → server event, and the one handler that
    closes whichever picker is open."""
    client = context.client
    if getattr(client, "_pb_pick_installed", False):
        return
    client._pb_pick_installed = True
    client._pb_pick_open = None
    ui.add_css(_CSS)
    if client.has_socket_connection:
        # Built after the page loaded (the experts' Access tab renders from a
        # timer): a <script> added to the head now would never run.
        ui.run_javascript(_JS.removeprefix("<script>").removesuffix("</script>"))
    else:
        ui.add_head_html(_JS)

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
    # The list lives in the browser: a checkbox per option that the server
    # never hears about until the dialog closes. One server round trip per
    # tick made every click lag on a long list.
    def open(self) -> None:
        _ = self._
        client = context.client
        keys = list(self.options)
        chosen = {i for i, k in enumerate(keys) if k in set(self.value)}
        order = sorted(range(len(keys)), key=lambda i: (i not in chosen, str(self.options[keys[i]]).casefold()))
        with ui.dialog() as dlg, ui.card().classes("gap-2").style(
                "background:var(--c-surface); width:min(520px,100vw); max-width:100vw; height:min(640px,85vh);"
                "display:flex; flex-direction:column; flex-wrap:nowrap; padding:12px;"):
            uid = f"pbpick{dlg.id}"
            with ui.row().classes("w-full items-center no-wrap gap-2"):
                ui.label(self.title).classes("text-base font-semibold flex-1 min-w-0 truncate").style(
                    "color:var(--c-text)")
                ui.html(f'<span id="{uid}-n" class="pbx-hint"></span>', sanitize=False).classes("flex-shrink-0")
                ui.button(_("Done"), on_click=lambda: dlg.close()).props("unelevated dense no-caps").classes(
                    "bg-primary text-white flex-shrink-0")
            rows = "".join(
                f'<label class="pb-pick-row" data-t="{html.escape(str(self.options[keys[i]]).casefold())}">'
                f'<input type="checkbox" data-i="{i}"{" checked" if i in chosen else ""}>'
                f'<span>{html.escape(str(self.options[keys[i]]))}</span></label>'
                for i in order)
            ui.html(
                f'<input id="{uid}-q" class="pb-pick-q" type="search" placeholder="{html.escape(_("Search"))}" '
                'autocomplete="off">', sanitize=False).classes("w-full")
            ui.html(f'<div id="{uid}" class="pb-pick-list">{rows}'
                    f'<div class="pb-pick-none pbx-hint" style="display:none">{html.escape(_("Nothing matches."))}</div>'
                    "</div>", sanitize=False).classes("w-full").style("flex:1; min-height:0; display:flex;")
            with ui.row().classes("w-full justify-between items-center"):
                ui.button(_("Clear all"), on_click=lambda: ui.run_javascript(f"window.__pbPickClear('{uid}')")).props(
                    "flat dense no-caps").classes("text-xs")
                ui.label(_("Back closes this list.")).classes("pbx-hint")

        async def _closed(e) -> None:
            if e.value:
                return
            if getattr(client, "_pb_pick_open", None) is dlg:
                # Closed by Done or a tap outside: drop the history entry the
                # dialog pushed (closed by Back, it is already gone).
                client._pb_pick_open = None
                ui.run_javascript("if(history.state&&history.state.pbPick)history.back();")
            try:  # the browser kept the ticks; read them once
                picked = await ui.run_javascript(f"window.__pbPickGet('{uid}')", timeout=3)
            except Exception:  # noqa: BLE001 — tab gone or too slow: keep the old value
                picked = None
            if picked is not None:
                new = [keys[int(i)] for i in sorted(int(x) for x in picked) if 0 <= int(i) < len(keys)]
                if new != self.value:
                    self.set_value(new)
            dlg.delete()

        dlg.on_value_change(_closed)
        client._pb_pick_open = dlg
        dlg.open()
        ui.run_javascript(f"history.pushState({{pbPick:1}},'');window.__pbPickInit('{uid}',"
                          f"{json.dumps(_('{n} chosen'))})")
