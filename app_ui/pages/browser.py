# app_ui/pages/browser.py
"""The document browser: the whole archive as facets, filtered live.

docs/ux-overhaul-plan.md, Phase 3. The list is the user's catalogue
(`PaperlessClient.list_catalogue`: metadata only, their own token, fetched
once per page); every facet click filters it in memory (`app_ui/doc_facets.py`),
so nothing waits for Paperless. The search field has three modes:

- **Meaning** — the semantic search, run over the filtered documents only
  (`search(candidates=…)`): filter first, then rank.
- **Title** — an instant filter on title, correspondent, type and `#id`.
- **Full text** — Paperless' own full-text search, intersected with the filter.

The facet panel and the list view are rendered as HTML in one piece and talk
back by events: an element per value or row made every click re-render
hundreds of components.
"""

import html
import json
from datetime import date
from urllib.parse import urlencode

from nicegui import ui

from app_ui import doc_facets as fx
from app_ui.cluster_dialog import create_cluster_dialog
from app_ui.document_dialog import create_document_dialog
from app_ui.layout import page_layout, require_auth
from app_ui.similar_dialog import create_similar_dialog
from app_ui.tag_style import render_tag_chips, tag_chips_html
from config.settings import settings
from i18n import get_translator
from models.result_document import DocumentResult
from pipelines.search import search
from services.clients import cross_ref_index, get_session_paperless
from services.user_prefs import get_pref, set_pref

PAGE = 60  # results per "show more"
MODES = ("meaning", "title", "fulltext")
VIEWS = ("cards", "list", "grouped")
SORTS = ("created", "added", "title", "correspondent", "document_type", "relevance")
SORT_DESC = {"created", "added"}

_CSS = """<style>
html, body { overflow: hidden !important; }
.q-page { min-height: 0 !important; overflow: hidden !important; }
.nicegui-content { padding: 0 !important; }
.pbb-root{position:fixed;top:var(--q-header-height,52px);left:0;right:0;bottom:0;display:grid;
  grid-template-columns:280px minmax(0,1fr);background:var(--c-bg)}
.pbb-facets{border-right:1px solid var(--c-border);overflow-y:auto;overflow-x:hidden;min-width:0;background:var(--c-surface)}
.pbb-main{display:flex;flex-direction:column;min-width:0;min-height:0}
.pbb-bar{padding:10px 14px 8px;border-bottom:1px solid var(--c-border);display:flex;flex-direction:column;gap:8px;min-width:0}
.pbb-results{flex:1;min-height:0;overflow-y:auto;overflow-x:hidden}
.pbb-count{font-size:.75rem;color:var(--c-text-muted);white-space:nowrap}
/* Facets */
.pbf{padding:4px 0 10px}
.pbf details{border-bottom:1px solid var(--c-border)}
.pbf summary{list-style:none;cursor:pointer;padding:9px 14px;font-size:.78rem;font-weight:600;color:var(--c-text-2);
  display:flex;align-items:center;gap:6px;user-select:none}
.pbf summary::-webkit-details-marker{display:none}
.pbf summary .n{margin-left:auto;font-weight:400;color:var(--c-text-muted)}
.pbf summary::before{content:'';width:6px;height:6px;border-right:1.5px solid currentColor;border-bottom:1.5px solid currentColor;
  transform:rotate(-45deg);transition:transform .12s;margin-right:2px}
.pbf details[open] summary::before{transform:rotate(45deg)}
.pbf-vals{max-height:280px;overflow-y:auto;padding:0 6px 8px}
.pbf-q{width:calc(100% - 16px);margin:0 8px 6px;box-sizing:border-box;padding:5px 8px;border:1px solid var(--c-border);
  border-radius:6px;background:var(--c-bg);color:var(--c-text);font-size:.8rem;outline:none}
.pbf-q:focus{border-color:var(--c-accent)}
.pbf-row{display:flex;align-items:center;gap:8px;padding:4px 8px;border-radius:6px;cursor:pointer;font-size:.82rem;
  color:var(--c-text);min-width:0}
.pbf-row:hover{background:var(--c-surface-2)}
.pbf-row .t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pbf-row .n{color:var(--c-text-muted);font-size:.72rem;font-variant-numeric:tabular-nums}
.pbf-row.is-zero{opacity:.45}
.pbf-row.sub{padding-left:26px}
.pbf-row .mk{width:15px;height:15px;border-radius:4px;border:1.5px solid var(--c-border-strong);flex-shrink:0;
  display:flex;align-items:center;justify-content:center;font-size:11px;line-height:1;color:#fff}
.pbf-row.is-in .mk{background:var(--c-accent);border-color:var(--c-accent)}
.pbf-row.is-in .mk::after{content:'✓'}
.pbf-row.is-out .mk{border-color:var(--c-warn)}
.pbf-row.is-out .mk::after{content:'−';color:var(--c-warn);font-size:13px}
.pbf-row.is-out .t{text-decoration:line-through;color:var(--c-text-muted)}
.pbf-mode{display:flex;gap:4px;padding:0 14px 6px;font-size:.72rem;color:var(--c-text-muted);align-items:center}
.pbf-mode a{cursor:pointer;padding:1px 6px;border-radius:4px;border:1px solid var(--c-border)}
.pbf-mode a.on{color:var(--c-text);border-color:var(--c-accent)}
.pbf-hint{padding:8px 14px;font-size:.72rem;color:var(--c-text-muted)}
.pbf-head{display:none}
.pbf-done{cursor:pointer;color:var(--c-accent-text);font-weight:600;font-size:.85rem;padding:4px 6px}
.pbb-view{display:flex;gap:0}
.pbb-view .q-btn{color:var(--c-text-muted)}
.pbb-view .q-btn.on{color:var(--c-text);background:var(--c-surface-2)}
/* List view */
.pbl-row{display:grid;grid-template-columns:minmax(0,1fr) 150px 110px 90px;gap:12px;align-items:center;padding:7px 16px;
  border-bottom:1px solid var(--c-border);cursor:pointer;min-width:0;font-size:.84rem}
.pbl-row:hover{background:var(--c-surface-2)}
.pbl-row .t{min-width:0;display:flex;flex-direction:column;gap:3px}
.pbl-row .t b{font-weight:500;color:var(--c-text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pbl-row .chips{display:flex;gap:4px;flex-wrap:nowrap;overflow:hidden;align-items:center}
.pbl-row .c,.pbl-row .d,.pbl-row .y{color:var(--c-text-2);font-size:.78rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pbl-row .d{font-variant-numeric:tabular-nums}
.pbl-row .flag{color:var(--c-warn);font-size:.7rem;white-space:nowrap}
.pbl-row .snip{font-size:.75rem;color:var(--c-text-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pbl-head{position:sticky;top:0;z-index:1;background:var(--c-bg);cursor:default;font-size:.72rem;color:var(--c-text-muted)}
.pbl-head:hover{background:var(--c-bg)}
.pbl-head span{cursor:pointer}
.pbl-head span.on{color:var(--c-text)}
.doc-card{border:1px solid var(--c-border);transition:transform .15s ease,box-shadow .15s ease,border-color .15s ease}
.doc-card:hover{transform:translateY(-3px);box-shadow:0 10px 28px rgba(0,0,0,.25);border-color:var(--c-accent-border)}
.doc-thumb{cursor:pointer}
.pbb-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(208px,1fr));gap:14px;padding:14px;align-items:start}
.pbb-cards .doc-card{width:100%!important}
.pbb-more{display:flex;justify-content:center;padding:10px 0 24px}
.pbb-facet-btn{display:none!important}
@media (max-width:767px){
  .pbb-root{grid-template-columns:minmax(0,1fr)}
  .pbb-facets{display:none;position:fixed;inset:var(--q-header-height,52px) 0 0 0;z-index:30;border-right:0}
  body.pbb-facets-open .pbb-facets{display:block}
  .pbf-head{display:flex;align-items:center;gap:8px;padding:10px 14px;border-bottom:1px solid var(--c-border);
    position:sticky;top:0;background:var(--c-surface);z-index:1}
  .pbb-facet-btn{display:inline-flex!important}
  .pbb-cards{grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px;padding:10px}
  .pbl-row{grid-template-columns:minmax(0,1fr) 78px;padding:8px 12px}
  .pbl-row .c,.pbl-row .y{display:none}
  .pbl-head{display:none}
  .pbb-bar{padding:8px 10px}
}
</style>"""

_JS = """<script>
(function(){
  if(window.__pbbInit)return;window.__pbbInit=1;
  document.addEventListener('click',function(e){
    var r=e.target.closest&&e.target.closest('.pbf-row');
    if(r){emitEvent('pbf_click',{f:r.dataset.f,v:r.dataset.v});return;}
    var m=e.target.closest&&e.target.closest('.pbf-mode a');
    if(m){emitEvent('pbf_tagsall',m.dataset.all==='1');return;}
    var l=e.target.closest&&e.target.closest('.pbl-row[data-id]');
    if(l){emitEvent('pbb_open',+l.dataset.id);return;}
    var h=e.target.closest&&e.target.closest('.pbl-head span[data-s]');
    if(h){emitEvent('pbb_sort',h.dataset.s);return;}
  });
  // Which facets are open survives the re-render after each click.
  document.addEventListener('toggle',function(e){var d=e.target;
    if(d.tagName==='DETAILS'&&d.dataset.f)emitEvent('pbf_open',{f:d.dataset.f,open:d.open});},true);
  // A facet's own quick filter: client-side, like the multi-pickers.
  document.addEventListener('input',function(e){var q=e.target;if(!q.classList||!q.classList.contains('pbf-q'))return;
    var t=q.value.trim().toLowerCase();q.parentElement.querySelectorAll('.pbf-row').forEach(function(r){
      r.style.display=!t||(r.dataset.l||'').indexOf(t)>=0?'':'none';});});
  // Phone: the facets are a sheet over the page; back closes it.
  window.pbbFacets=function(open){
    var on=document.body.classList.contains('pbb-facets-open');
    if(open&&!on){document.body.classList.add('pbb-facets-open');history.pushState({pbb:1},'',location.href);}
    else if(!open&&on){document.body.classList.remove('pbb-facets-open');if(history.state&&history.state.pbb)history.back();}
  };
  // Back closes the sheet — and restores the address of the filtered view: the
  // entry the sheet pushed carried the filter changes made while it was open.
  window.addEventListener('popstate',function(){
    if(!document.body.classList.contains('pbb-facets-open'))return;
    document.body.classList.remove('pbb-facets-open');emitEvent('pbb_sheet_closed');});
})();
</script>"""


def _action_ids() -> set[int]:
    """Documents with actions, from the sidecar index the sync builds — one
    file instead of one per document."""
    try:
        with open(f"{settings.extraction_sidecar_path}/index.json") as f:
            data = json.load(f)
        return {int(a["paperless_id"]) for a in data.get("actions", []) if a.get("paperless_id")}
    except (OSError, ValueError, TypeError):
        return set()


def _sort_key(sort: str):
    if sort == "title":
        return lambda d: (d.title or "").casefold()
    if sort == "correspondent":
        return lambda d: ((d.correspondent or "~").casefold(), (d.title or "").casefold())
    if sort == "document_type":
        return lambda d: ((d.document_type or "~").casefold(), (d.title or "").casefold())
    if sort == "added":
        return lambda d: d.added
    return lambda d: d.created


@ui.page("/browser")
async def browser():
    if not require_auth():
        return
    page_layout()
    _ = get_translator()
    ui.add_head_html(_CSS)
    ui.add_head_html(_JS)
    request = ui.context.client.request

    F, extra = fx.from_query(request.query_params)
    st: dict = {
        "docs": None,  # the catalogue, once loaded
        "mode": extra["m"] if extra["m"] in MODES else "meaning",
        "q": extra["q"],
        "view": extra["v"] if extra["v"] in VIEWS else "",
        "sort": extra["s"] if extra["s"] in SORTS else "",
        "limit": PAGE,
        "ranked": None,  # DocumentResult list of the last meaning search, or None
        "fulltext": None,  # ids of the last full-text search, or None
        "busy": False,
        "open": {"tags", "correspondent", "document_type"},
        "group": "correspondent",
    }
    seq = [0]
    flags: dict[int, set[str]] = {}

    # ── Dialogs (the shared document dialog, cluster, similar) ───────────────
    open_document, _doc_dlg = create_document_dialog(
        open_cluster_fn=lambda doc_id: open_cluster(doc_id),
        open_similar_fn=lambda doc_id: open_similar(doc_id),
        pin_fn=lambda r: _pin(r),
        get_pin_state_fn=lambda doc_id: doc_id in _pinned(),
    )

    def _pinned() -> set[int]:
        return set(get_pref("pinned_doc_ids") or [])

    def _pin(result: DocumentResult) -> None:
        ids = _pinned()
        cache: list[dict] = list(get_pref("pinned_docs_cache") or [])
        if result.document.id in ids:
            ids.discard(result.document.id)
            cache = [c for c in cache if c["id"] != result.document.id]
            ui.notify(_("#{id} unpinned").format(id=result.document.id), timeout=1500)
        else:
            ids.add(result.document.id)
            cache.append({"id": result.document.id, "title": result.document.title})
            ui.notify(_("#{id} pinned").format(id=result.document.id), type="positive", timeout=1500)
        set_pref("pinned_doc_ids", list(ids))
        set_pref("pinned_docs_cache", cache)
        _flags_refresh()
        render()

    def _flags_refresh() -> None:
        flags.clear()
        for i in _action_ids():
            flags.setdefault(i, set()).add("actions")
        for i in _pinned():
            flags.setdefault(i, set()).add("pinned")

    # ── State → address ──────────────────────────────────────────────────────
    def _push_url() -> None:
        q = fx.to_query(F, mode=st["mode"] if st["mode"] != "meaning" else "", q=st["q"],
                        view=st["view"], sort=st["sort"])
        url = "/browser" + ("?" + urlencode(q) if q else "")
        ui.run_javascript(f"history.replaceState(history.state,'',{json.dumps(url)})")

    def _view() -> str:
        return st["view"] or ("cards" if not st["q"] else "list")

    def _sort() -> str:
        if st["sort"]:
            return st["sort"]
        return "relevance" if st["ranked"] is not None else "created"

    # ── The filtered result ──────────────────────────────────────────────────
    def _filtered() -> list:
        docs = fx.apply(st["docs"] or [], F, flags=flags)
        if st["mode"] == "fulltext" and st["fulltext"] is not None:
            docs = [d for d in docs if d.id in st["fulltext"]]
        return docs

    def _results() -> list[DocumentResult]:
        if st["ranked"] is not None and st["mode"] == "meaning" and st["q"].strip():
            res = list(st["ranked"])
            if _sort() != "relevance":
                res.sort(key=lambda r: _sort_key(_sort())(r.document), reverse=_sort() in SORT_DESC)
            return res
        key = _sort() if _sort() != "relevance" else "created"
        docs = sorted(_filtered(), key=_sort_key(key), reverse=key in SORT_DESC)
        acts = {i for i, f in flags.items() if "actions" in f}
        return [DocumentResult(document=d, has_actions=d.id in acts) for d in docs]

    # ── Facet panel ──────────────────────────────────────────────────────────
    def _label(facet: str, v: str) -> str:
        if v == fx.NONE:
            return _("(none)")
        if facet == fx.FLAG_FACET:
            return {"actions": _("Action required"), "pinned": _("Pinned")}.get(v, v)
        if facet == "created" and len(v) == 7:
            from i18n import month_name
            from services.user_prefs import get_pref as _gp

            try:
                return month_name(date(int(v[:4]), int(v[5:]), 1), _gp("language", "en"))
            except (ValueError, TypeError):
                return v
        return v

    def _row(facet: str, v: str, n: int, sub: bool = False) -> str:
        s = F.state(facet, v)
        cls = "pbf-row" + (" is-in" if s > 0 else " is-out" if s < 0 else "") + (" is-zero" if not n and not s else "") \
            + (" sub" if sub else "")
        lab = _label(facet, v)
        return (f'<div class="{cls}" data-f="{html.escape(facet)}" data-v="{html.escape(v)}" '
                f'data-l="{html.escape(lab.lower())}"><span class="mk"></span>'
                f'<span class="t">{html.escape(lab)}</span><span class="n">{n}</span></div>')

    def _facet_html(counts) -> str:
        titles = {"tags": _("Tags"), "correspondent": _("Correspondent"), "document_type": _("Document type"),
                  "owner": _("Owner"), "created": _("Created"), fx.ADDED_FACET: _("Added"),
                  fx.FLAG_FACET: "PaperlessBrain"}
        all_vals = {f: set() for f in fx.DATA_FACETS}
        for d in st["docs"] or []:
            for f in fx.DATA_FACETS:
                all_vals[f].update(fx.values_of(d, f))
        parts = [(f'<div class="pbf-head"><b style="flex:1;color:var(--c-text)">{html.escape(_("Filters"))}</b>'
                  f'<span class="pbb-count">{html.escape(_("{n} documents").format(n=len(_filtered())))}</span>'
                  f'<a class="pbf-done" onclick="pbbFacets(false)">{html.escape(_("Done"))}</a></div>'),
                 '<div class="pbf">']
        for facet in (*fx.DATA_FACETS, fx.ADDED_FACET, fx.FLAG_FACET):
            c = counts.get(facet, {})
            if facet == "created":
                vals = sorted({v for v in all_vals[facet] if len(v) == 4}, reverse=True)
            elif facet == fx.ADDED_FACET:
                vals = list(fx.ADDED_PRESETS)
            elif facet == fx.FLAG_FACET:
                vals = ["actions", "pinned"]
            else:
                vals = sorted(all_vals[facet], key=lambda v: (-c.get(v, 0), v == fx.NONE, v.casefold()))
            if facet == "owner" and len(vals) < 2:
                continue
            if not vals:
                continue
            chosen = len(F.sel.get(facet, {})) + (1 if facet == fx.ADDED_FACET and F.added else 0)
            is_open = facet in st["open"] or chosen
            parts.append(f'<details data-f="{facet}"{" open" if is_open else ""}><summary>{html.escape(titles[facet])}'
                         f'<span class="n">{chosen or ""}</span></summary>')
            if facet == "tags":
                parts.append(f'<div class="pbf-mode">{html.escape(_("Match"))}'
                             f'<a data-all="0" class="{"" if F.tags_all else "on"}">{html.escape(_("any"))}</a>'
                             f'<a data-all="1" class="{"on" if F.tags_all else ""}">{html.escape(_("all"))}</a></div>')
            parts.append('<div class="pbf-vals">')
            if len(vals) > 12:
                parts.append(f'<input class="pbf-q" placeholder="{html.escape(_("Filter …"))}">')
            for v in vals:
                if facet == fx.ADDED_FACET:
                    lab = {"7d": _("Last 7 days"), "30d": _("Last 30 days"), "365d": _("Last 12 months")}[v]
                    cls = "pbf-row" + (" is-in" if F.added == v else "")
                    parts.append(f'<div class="{cls}" data-f="added" data-v="{v}" data-l="{html.escape(lab.lower())}">'
                                 f'<span class="mk"></span><span class="t">{html.escape(lab)}</span>'
                                 f'<span class="n">{c.get(v, 0)}</span></div>')
                    continue
                parts.append(_row(facet, v, c.get(v, 0)))
                if facet == "created" and (F.state("created", v) > 0 or any(
                        k.startswith(v + "-") for k in F.sel.get("created", {}))):
                    months = sorted({m for m in all_vals["created"] if len(m) == 7 and m.startswith(v)}, reverse=True)
                    parts += [_row("created", m, c.get(m, 0), sub=True) for m in months]
            parts.append("</div></details>")
        parts.append(f'<div class="pbf-hint">{html.escape(_("Tap once to include, twice to exclude, three times to clear."))}</div>')
        parts.append("</div>")
        return "".join(parts)

    # ── Results ──────────────────────────────────────────────────────────────
    def _list_html(results: list[DocumentResult]) -> str:
        sort = _sort()
        heads = [("title", _("Title")), ("correspondent", _("Correspondent")), ("document_type", _("Document type")),
                 ("created", _("Created"))]
        rows = ['<div class="pbl-row pbl-head">' + "".join(
            f'<span data-s="{k}" class="{"on" if sort == k else ""}">{html.escape(lab)}</span>' for k, lab in heads)
            + "</div>"]
        for r in results:
            d = r.document
            snip = ""
            if r.matched_chunks:
                snip = f'<span class="snip">{html.escape(" ".join(r.matched_chunks[0].split())[:160])}</span>'
            flag = f'<span class="flag">{html.escape(_("Action required"))}</span>' if r.has_actions else ""
            rows.append(
                f'<div class="pbl-row" data-id="{d.id}"><span class="t"><b>{html.escape(r.display_title)}</b>'
                f'<span class="chips">{tag_chips_html(d.tags)}{flag}</span>{snip}</span>'
                f'<span class="c">{html.escape(d.correspondent or "")}</span>'
                f'<span class="y">{html.escape(d.document_type or "")}</span>'
                f'<span class="d">{html.escape(r.display_date or "")}</span></div>')
        return "".join(rows)

    @ui.refreshable
    def results_view() -> None:
        if st["docs"] is None:
            with ui.column().classes("w-full items-center py-16 gap-2"):
                ui.spinner(size="lg")
                ui.label(_("Loading the archive …")).classes("text-sm").style("color:var(--c-text-muted)")
            return
        results = _results()
        if not results:
            with ui.column().classes("w-full items-center py-16 gap-2"):
                ui.icon("search_off", size="48px").style("color:var(--c-border-strong)")
                ui.label(_("No matches — adjust search term or filters.")).classes("text-sm").style(
                    "color:var(--c-text-muted)")
            return
        view, shown = _view(), results[: st["limit"]]
        if view == "grouped":
            from app_ui.doc_library import _CSS as _LIB_CSS
            from app_ui.doc_library import _NODE, build_nodes

            ui.add_head_html(_LIB_CSS)
            tree = ui.tree(build_nodes([r.document for r in results], st["group"], _("(none)")),
                           label_key="label", node_key="id").classes("w-full pb-lib").style("padding:8px 12px")
            tree.add_slot("default-header", _NODE)

            async def _sel(e) -> None:
                v = e.value or ""
                tree._props["selected"] = None
                tree.update()
                if v.startswith("d:"):
                    await _open_id(int(v.split(":")[1]))

            tree.on_select(_sel)
            return
        if view == "list":
            ui.html(_list_html(shown), sanitize=False).classes("w-full")
        else:
            pinned = _pinned()
            with ui.element("div").classes("pbb-cards w-full"):
                for r in shown:
                    _render_card(r, open_document, open_cluster, on_pin=_pin, is_pinned=r.document.id in pinned)
        if len(results) > len(shown):
            with ui.element("div").classes("pbb-more w-full"):
                ui.button(_("Show {n} more").format(n=min(PAGE, len(results) - len(shown))),
                          on_click=_more).props("flat no-caps")

    def _more() -> None:
        st["limit"] += PAGE
        results_view.refresh()

    async def _open_id(doc_id: int) -> None:
        r = next((x for x in (st["ranked"] or []) if x.document.id == doc_id), None)
        if r is None:
            d = next((x for x in st["docs"] or [] if x.id == doc_id), None)
            if d is None:
                return
            r = DocumentResult(document=d, has_actions="actions" in flags.get(d.id, set()))
        await open_document(r)

    def render() -> None:
        """Everything a filter change touches — at once, from memory."""
        counts = fx.counts(st["docs"] or [], F, flags=flags)
        facets_html.set_content(_facet_html(counts))
        n = len(_results()) if st["docs"] is not None else 0
        count_label.set_text("" if st["docs"] is None else _("{n} documents").format(n=n))  # TODO i18n-plural
        clear_btn.set_visibility(bool(F.active()))
        for v, b in view_btns.items():
            b.classes(add="on") if v == _view() else b.classes(remove="on")
        group_row.set_visibility(_view() == "grouped")
        facet_btn.set_text(_("Filters") + (f" ({len(F.sel) + (1 if F.added else 0)})" if F.sel or F.added else ""))
        results_view.refresh()
        _push_url()

    # ── Searching ────────────────────────────────────────────────────────────
    async def _run_query() -> None:
        """The field's search, for the modes that ask the server: meaning
        (semantic over the filtered set) and full text (Paperless)."""
        seq[0] += 1
        my = seq[0]
        q = st["q"].strip()
        st["limit"] = PAGE
        if st["mode"] == "title" or not q:
            st["ranked"], st["fulltext"] = None, None
            F.text = q if st["mode"] == "title" else ""
            render()
            return
        F.text = ""
        if q.startswith("#") and q[1:].isdigit():  # "#241" jumps to the document
            await _open_id(int(q[1:]))
            return
        search_field.props(add="loading")
        try:
            if st["mode"] == "meaning":
                ranked = await search(filters={}, semantic_query=q, paperless_client=get_session_paperless(),
                                      candidates=fx.apply(st["docs"] or [], F, flags=flags), n_results=120)
                acts = {i for i, f in flags.items() if "actions" in f}
                for r in ranked:
                    r.has_actions = r.document.id in acts
                if my == seq[0]:
                    st["ranked"], st["fulltext"] = ranked, None
            else:
                docs = await get_session_paperless().list_documents(query=q)
                if my == seq[0]:
                    st["fulltext"], st["ranked"] = {d.id for d in docs}, None
        except Exception as exc:  # noqa: BLE001 — a failed search is shown, never a dead page
            if my == seq[0]:
                ui.notify(_("Search failed: {err}").format(err=exc), type="warning")
        finally:
            if my == seq[0]:
                search_field.props(remove="loading")
        if my == seq[0]:
            render()

    async def _on_field(e) -> None:
        st["q"] = e.args if isinstance(e.args, str) else (search_field.value or "")
        await _run_query()

    async def _pick_mode(m: str, icon: str) -> None:
        mode_btn.props(f"icon={icon}")
        await _set_mode(m)

    async def _set_mode(m: str) -> None:
        st["mode"] = m
        search_field.props(f'placeholder="{_placeholder()}"')
        await _run_query()

    def _placeholder() -> str:
        return {"meaning": _("Search by meaning — e.g. 'dentist invoice 2024'"),
                "title": _("Filter titles, correspondents, #id"),
                "fulltext": _("Paperless full-text search")}[st["mode"]]

    # ── Events from the HTML parts ───────────────────────────────────────────
    async def _on_facet(e) -> None:
        a = e.args or {}
        f, v = a.get("f", ""), a.get("v", "")
        if f == "added":
            F.added = "" if F.added == v else v
        else:
            F.cycle(f, v)
        st["limit"] = PAGE
        if st["mode"] == "meaning" and st["q"].strip():
            await _run_query()  # rank again inside the new filter
        else:
            render()

    def _on_open(e) -> None:
        a = e.args or {}
        (st["open"].add if a.get("open") else st["open"].discard)(a.get("f"))

    def _on_tagsall(e) -> None:
        F.tags_all = bool(e.args)
        render()

    def _on_sort(e) -> None:
        st["sort"] = str(e.args or "")
        sort_select.set_value(st["sort"])

    ui.on("pbf_click", _on_facet)
    ui.on("pbf_open", _on_open)
    ui.on("pbf_tagsall", _on_tagsall)
    ui.on("pbb_open", lambda e: _open_id(int(e.args)))
    ui.on("pbb_sort", _on_sort)
    ui.on("pbb_sheet_closed", lambda _e: _push_url())

    # ── Layout ───────────────────────────────────────────────────────────────
    with ui.element("div").classes("pbb-root"):
        facets_html = ui.html("", sanitize=False).classes("pbb-facets")
        with ui.element("div").classes("pbb-main"):
            with ui.element("div").classes("pbb-bar"):
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    search_field = ui.input(value=st["q"]).props(
                        f'outlined dense clearable debounce=600 placeholder="{_placeholder()}"').classes("flex-1 min-w-0")
                    search_field.on("update:model-value", _on_field)
                    search_field.on("keydown.enter", lambda: _run_query())
                    with search_field.add_slot("prepend"):
                        mode_btn = ui.button(icon="auto_awesome", color=None).props("flat dense round size=sm")
                        with mode_btn, ui.menu():
                            for m, icon, lab in (("meaning", "auto_awesome", _("By meaning")),
                                                 ("title", "title", _("Title filter")),
                                                 ("fulltext", "manage_search", _("Full text"))):
                                ui.menu_item(lab, on_click=lambda m=m, i=icon: _pick_mode(m, i))
                    mode_btn.props("icon=" + {"meaning": "auto_awesome", "title": "title", "fulltext": "manage_search"}[st["mode"]])
                    facet_btn = ui.button(_("Filters"), icon="tune", color=None,
                                          on_click=lambda: ui.run_javascript("pbbFacets(true)")).props(
                        "flat dense no-caps").classes("pbb-facet-btn")
                # No pill per active filter: on a phone they filled the screen.
                # The Filters button carries the count and the panel shows them.
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    count_label = ui.label("").classes("pbb-count")
                    clear_btn = ui.button(_("Clear all"), icon="filter_alt_off",
                                          on_click=lambda: (F.clear(), _clear_q(), render())).props(
                        "flat dense no-caps size=sm")
                    ui.element("div").classes("flex-1")
                    sort_select = ui.select(
                        {"created": _("Created"), "added": _("Added"), "title": _("Title"),
                         "correspondent": _("Correspondent"), "document_type": _("Document type"),
                         "relevance": _("Relevance")},
                        value=_sort(), on_change=lambda e: (st.update(sort=e.value or ""), render()),
                    ).props("dense outlined options-dense").classes("w-36")
                    with ui.element("div").classes("pbb-view"):
                        view_btns = {}
                        for v, icon, tip in (("cards", "grid_view", _("Cards")), ("list", "view_list", _("List")),
                                             ("grouped", "account_tree", _("Grouped"))):
                            view_btns[v] = ui.button(icon=icon, color=None, on_click=lambda v=v: _set_view(v)).props(
                                "flat dense size=sm").tooltip(tip)
                group_row = ui.row().classes("w-full items-center gap-2")

            with ui.element("div").classes("pbb-results"):
                results_view()

    def _clear_q() -> None:
        st["q"] = ""
        st["ranked"] = st["fulltext"] = None
        search_field.set_value("")

    with group_row:
        ui.label(_("Group by")).classes("pbb-count")
        ui.toggle({"correspondent": _("Correspondent"), "tag": _("Tag"), "document_type": _("Type")},
                  value=st["group"], on_change=lambda e: (st.update(group=e.value), results_view.refresh())).props(
            "dense flat no-caps toggle-color=primary").style("font-size:.75rem")

    def _set_view(v: str) -> None:
        st["view"], st["limit"] = v, PAGE
        render()

    open_cluster = create_cluster_dialog(
        open_document_fn=open_document, pin_fn=lambda r: _pin(r),
        get_pinned_ids_fn=lambda: _pinned(), render_card_fn=_render_card)
    open_similar = create_similar_dialog(
        open_document_fn=open_document, pin_fn=lambda r: _pin(r),
        get_pinned_ids_fn=lambda: _pinned(), render_card_fn=_render_card)
    _doc_dlg.on("hide", lambda _e: results_view.refresh())

    # ── Load the catalogue ───────────────────────────────────────────────────
    async def _load() -> None:
        from pipelines.search import _drop_no_ingest

        try:
            st["docs"] = _drop_no_ingest(await get_session_paperless().list_catalogue())
        except Exception as exc:  # noqa: BLE001
            st["docs"] = []
            ui.notify(_("Could not load the document list: {err}").format(err=exc), type="negative")
        _flags_refresh()
        open_id = request.query_params.get("open") or ""
        if open_id.isdigit():  # the quick switcher's document rows
            try:
                await _open_id(int(open_id))
            except Exception as exc:  # noqa: BLE001 — the list still loads
                ui.notify(_("Could not open document #{id}: {err}").format(id=open_id, err=exc), type="warning")
        if st["q"].strip() and st["mode"] != "title":
            await _run_query()
        else:
            if st["mode"] == "title":
                F.text = st["q"]
            render()

    render()
    ui.timer(0.05, _load, once=True)


# ── Module-level card renderer ────────────────────────────────────────────────


def _render_card(result: DocumentResult, on_eye, on_cluster=None, on_pin=None,
                 is_pinned: bool = False, show_distance: bool = False) -> None:
    _ = get_translator()
    doc = result.document
    _pin_border = "border: 2px solid var(--c-accent) !important;" if is_pinned else ""
    with ui.card().classes("w-52 hover:shadow-xl bg-gray-800 gap-0 doc-card").style(
        "position:relative; overflow:hidden; " + _pin_border
    ):
        ui.element("div").classes("w-full rounded-t doc-thumb").style(
            f"height:9rem; background-image:url('/thumbnails/{doc.id}.jpg');"
            "background-size:cover; background-position:top center;"
        ).on("click", lambda r=result: on_eye(r))
        if is_pinned:
            ui.icon("push_pin", size="xs").classes("pb-accent-text").style(
                "position:absolute; top:4px; right:4px;"
                "background:rgba(0,0,0,.55); border-radius:50%; padding:2px; z-index:1;"
            )
        with ui.column().classes("p-2 gap-1 w-full"):
            # The card body is the "open" affordance, which is what let the
            # preview icon go. Four equally-weighted icons per card across six
            # visible cards was 24 competing targets in one panel.
            _body = ui.column().classes("gap-1 w-full doc-card-body")
            _body.on("click", lambda r=result: on_eye(r))
            with _body:
                ui.label(result.display_title).classes(
                    "font-semibold text-xs text-gray-100 leading-tight"
                ).style(
                    "display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;"
                    "overflow:hidden;word-break:break-all;"
                )
                ui.separator().classes("my-1")

                if doc.correspondent:
                    with ui.row().classes("items-center gap-1 no-wrap w-full"):
                        ui.icon("person", size="xs").classes("text-gray-400").tooltip(
                            _("Correspondent")
                        )
                        ui.label(doc.correspondent).classes(
                            "text-xs text-gray-300 truncate"
                        ).style("min-width:0;")
                if doc.document_type:
                    with ui.row().classes("items-center gap-1 no-wrap w-full"):
                        ui.icon("description", size="xs").classes("text-gray-400").tooltip(
                            _("Document type")
                        )
                        ui.label(doc.document_type).classes(
                            "text-xs text-gray-300 truncate"
                        ).style("min-width:0;")

                # Owner is metadata, and reads exactly like the correspondent and
                # document-type rows above it. It used to carry a per-user hue,
                # which spent the accent on identity.
                if doc.owner_name:
                    with ui.row().classes("items-center gap-1 no-wrap w-full"):
                        ui.icon("person", size="xs").classes("text-gray-400").tooltip(
                            _("Owner")
                        )
                        ui.label(doc.owner_name).classes(
                            "text-xs text-gray-300 truncate"
                        ).style("min-width:0;")

                with ui.row().classes("items-center gap-2"):
                    if result.display_date:
                        with ui.row().classes("items-center gap-1"):
                            ui.icon("calendar_today", size="xs").classes(
                                "text-gray-400"
                            ).tooltip(_("Creation date"))
                            ui.label(result.display_date).classes("text-xs text-gray-400")
                    if doc.page_count:
                        with ui.row().classes("items-center gap-1"):
                            ui.icon("pages", size="xs").classes("text-gray-400").tooltip(
                                _("Page count")
                            )
                            ui.label(str(doc.page_count)).classes("text-xs text-gray-400")

                # In the similarity dialog the distance is not decoration, it is
                # the only thing that says where the list stops being useful --
                # so it is shown there regardless of the global setting. As a
                # percentage it would not: with cosine distances of 0.002..0.09
                # every neighbour rounds to "99%" and the gap that separates the
                # real relatives from the tail disappears.
                if show_distance and result.relevance_score is not None:
                    ui.label(
                        _("Distance: {d:.3f}").format(d=result.relevance_score)
                    ).classes("text-xs text-gray-400")
                elif settings.show_relevance_scores and result.relevance is not None:
                    ui.label(
                        _("Relevance: {score:.0%}").format(score=result.relevance)
                    ).classes("text-xs text-gray-400")

            # Tag row = Paperless data only. Anything PaperSage derives (the
            # action flag below) gets its own row and its own treatment.
            # Outside _body: the +N control has its own click.
            render_tag_chips(doc.tags)

            if result.has_actions:
                with ui.row().classes("items-center gap-1 w-full").style(
                    "border-top:0.5px solid var(--c-border);"
                    "margin-top:6px;padding-top:5px;"
                ):
                    ui.icon("bolt").classes("card-action-flag-icon").style(
                        "font-size:13px;"
                    )
                    # "Action required", not "Actions": a noun does not tell the
                    # user what is being asserted about the document.
                    ui.label(_("Action required")).classes("card-action-flag")

            with ui.row().classes("items-center justify-between mt-1 w-full"):
                ui.label(f"#{doc.id}").classes("text-xs text-gray-600")
                with ui.row().classes("gap-0"):
                    if on_cluster and cross_ref_index.has_related(doc.id):
                        ui.button(
                            icon="hub",
                            on_click=lambda d=doc.id: on_cluster(d),
                        ).props("flat dense dark").classes("card-action-btn").tooltip(
                            _("Show cross-reference cluster")
                        )
                    if on_pin:
                        ui.button(
                            icon="push_pin",
                            on_click=lambda r=result: on_pin(r),
                        ).props("flat dense dark").classes(
                            "card-action-btn" + (" is-pinned" if is_pinned else "")
                        ).tooltip(
                            _("Pinned — click to unpin") if is_pinned else _("Pin")
                        )
                    async def _download(_e=None, doc_id: int = doc.id) -> None:
                        """Fetch server-side, hand the bytes to the browser.

                        Never link the browser at `doc.pdf_url`: that is built
                        from PAPERLESS_URL, which is how *this server* reaches
                        Paperless — an internal address behind a reverse proxy,
                        and one the browser holds no Paperless session for, so
                        the link either does not resolve or answers 401. Going
                        through the session client also means the download obeys
                        the user's own Paperless permissions.
                        """
                        try:
                            data, name = await get_session_paperless(
                            ).download_document_named(doc_id)
                        except Exception as exc:
                            ui.notify(
                                _("Download of document #{id} failed: {err}").format(
                                    id=doc_id, err=exc
                                ),
                                type="negative",
                            )
                            return
                        ui.download(data, name, media_type="application/octet-stream")

                    ui.button(
                        icon="download", on_click=_download,
                    ).props("flat dense dark").classes("card-action-btn").tooltip(
                        _("Download PDF")
                    )


