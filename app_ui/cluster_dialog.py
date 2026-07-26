# app_ui/cluster_dialog.py
from typing import Callable

from nicegui import ui

from i18n import get_translator
from models.result_document import DocumentResult
from services.clients import cross_ref_index, get_session_paperless, sidecar_service


def create_cluster_dialog(
    open_document_fn: Callable,
    pin_fn: Callable,
    get_pinned_ids_fn: Callable[[], set],
    render_card_fn: Callable,
) -> Callable:
    """
    Factory: build one persistent cluster-dialog per page instance and return
    the ``open_cluster(doc_id)`` coroutine that opens it.

    Callers pass lambdas for pin_fn / get_pinned_ids_fn so late-bound names
    (defined after this call) still resolve correctly at event time.
    """
    _ = get_translator()
    _cluster_history: list[int] = []
    _active_refs: list = [[]]   # _active_refs[0] = current sorted ref list
    _current_doc_id: list[int] = [0]
    _loading: list[bool] = [False]

    async def _open_doc(res) -> None:
        # Close the cluster dialog first — the document dialog would otherwise
        # open underneath it and stay hidden.
        cluster_dlg.close()
        await open_document_fn(res)

    # ── Tab content (refreshable) ─────────────────────────────────────────────

    @ui.refreshable
    def _cluster_karten(docs=None, pinned=None):
        if not docs:
            return
        with ui.scroll_area().style("height:100%;"):
            with ui.row().classes("flex-wrap gap-4 p-4 doc-cards-row"):
                for _doc_obj in docs:
                    _r = DocumentResult(
                        document=_doc_obj,
                        has_actions=sidecar_service.has_actions(_doc_obj.id),
                    )
                    render_card_fn(
                        _r, _open_doc, open_cluster,
                        on_pin=pin_fn,
                        is_pinned=_doc_obj.id in (pinned or set()),
                    )

    @ui.refreshable
    def _cluster_zeitachse(sorted_docs=None, root_id=0, pinned=None):
        if not sorted_docs:
            return
        _pset = pinned or set()
        with ui.scroll_area().style("height:100%;"):
            with ui.column().classes("px-4 py-3 w-full gap-0"):
                ui.label(_("↑ Oldest")).classes("text-xs text-gray-600 mb-2 self-start pl-8")
                for i, _doc_obj in enumerate(sorted_docs):
                    _is_root = (_doc_obj.id == root_id)
                    _is_last = (i == len(sorted_docs) - 1)
                    _is_pinned_doc = _doc_obj.id in _pset
                    _r = DocumentResult(
                        document=_doc_obj,
                        has_actions=sidecar_service.has_actions(_doc_obj.id),
                    )
                    _date_str = _doc_obj.created.strftime("%d.%m.%y") if _doc_obj.created else "?"
                    with ui.row().classes("w-full items-stretch gap-0"):
                        with ui.column().classes("items-center gap-0").style(
                            "width:28px; flex-shrink:0; padding-top:10px;"
                        ):
                            _dot_bg = (
                                "background:#7c3aed; box-shadow:0 0 8px #7c3aed80;"
                                if _is_root else "background:var(--c-border-strong);"
                            )
                            ui.element("div").style(
                                f"width:11px; height:11px; border-radius:50%;"
                                f" flex-shrink:0; {_dot_bg}"
                            )
                            if not _is_last:
                                ui.element("div").style(
                                    "width:2px; flex:1; background:var(--c-border);"
                                    "min-height:28px; margin-top:3px;"
                                )
                        _border = (
                            "border:1px solid #7c3aed; background:rgba(124,58,237,0.07);"
                            if _is_root else "border:1px solid var(--c-border);"
                        )
                        with ui.row().classes(
                            "items-center gap-2 px-3 py-2 flex-1 rounded mb-1 ml-2"
                        ).style(_border + "flex-wrap:nowrap; overflow:hidden;"):
                            ui.label(_date_str).style(
                                "color:var(--c-text-muted); font-size:10px; flex-shrink:0;"
                                "font-family:monospace;"
                            )
                            ui.label(f"#{_doc_obj.id}").style(
                                "color:#a78bfa; font-size:11px; font-weight:600;"
                                "flex-shrink:0; font-family:monospace;"
                                "cursor:pointer; text-decoration:underline;"
                            ).on("click", lambda res=_r: _open_doc(res))
                            ui.label(_doc_obj.title or "?").classes(
                                "text-xs text-gray-300 flex-1 truncate"
                            ).style("min-width:0;")
                            if _doc_obj.document_type:
                                ui.label(_doc_obj.document_type).style(
                                    "font-size:10px;background:var(--c-border);color:var(--c-text-muted);"
                                    "border-radius:3px;padding:1px 5px;flex-shrink:1;"
                                    "overflow:hidden;text-overflow:ellipsis;"
                                    "white-space:nowrap;max-width:60px;"
                                )
                            if _is_root:
                                ui.icon("hub", size="xs").classes(
                                    "text-purple-400 flex-shrink-0"
                                ).tooltip(_("Source document"))
                            ui.button(
                                icon="push_pin",
                                on_click=lambda r=_r: pin_fn(r),
                            ).props("flat dense dark").classes(
                                "text-purple-400 flex-shrink-0"
                                if _is_pinned_doc else "text-gray-400 flex-shrink-0"
                            )
                            ui.button(
                                icon="visibility",
                                on_click=lambda res=_r: _open_doc(res),
                            ).props("flat dense dark").classes("text-gray-400 flex-shrink-0")
                ui.label(_("↓ Newest")).classes("text-xs text-gray-600 mt-1 self-start pl-8")

    # ── Dialog shell ──────────────────────────────────────────────────────────

    ui.add_head_html("""<style>
@media (max-width: 767px) {
    /* Cluster dialog takes the full viewport on mobile */
    .cl-dialog .q-dialog__inner { padding: 0 !important; }
    .cl-dialog .q-dialog__inner > div { max-height: 100dvh !important; border-radius: 0 !important; }
    .cl-card { height: 100dvh !important; }
}
</style>""")

    with ui.dialog().props("full-width").classes("cl-dialog") as cluster_dlg:
        with ui.card().classes("cl-card").style(
            "width:100%; height:85vh; background:var(--c-surface); padding:0;"
            "display:flex; flex-direction:column;"
        ):
            # Header: title row (back / title / close), ref-select on its own
            # row below — never competes with the close button for width.
            with ui.column().classes(
                "w-full gap-1 px-4 py-2 border-b border-gray-700"
            ).style("flex-shrink:0;"):
                with ui.row().classes("w-full items-center gap-2").style(
                    "flex-wrap:nowrap;"
                ):
                    cluster_back_btn = (
                        ui.button(icon="arrow_back")
                        .props("flat dark dense")
                        .classes("text-gray-400")
                        .style("flex-shrink:0;")
                        .tooltip(_("Back"))
                    )
                    cluster_back_btn.set_visibility(False)
                    cluster_title_lbl = ui.label("").classes(
                        "text-gray-100 font-semibold text-sm flex-1"
                    ).style(
                        "white-space:nowrap; overflow:hidden;"
                        "text-overflow:ellipsis; min-width:0;"
                    )
                    ui.button(
                        icon="close", on_click=cluster_dlg.close
                    ).props("flat dark dense").classes("text-gray-400").style(
                        "flex-shrink:0;"
                    )
                ref_select = (
                    ui.select(options={}, value=None)
                    .props("dense outlined dark")
                    .classes("w-full text-xs")
                    .style("max-width:420px;")
                )
                ref_select.set_visibility(False)

            # Tab bar
            with (
                ui.tabs(value="karten")
                .props("dense align='left'")
                .classes("bg-gray-800 text-gray-300")
                .style("flex-shrink:0;") as _cluster_tabs
            ):
                ui.tab("karten", label=_("Cards"), icon="grid_view")
                ui.tab("zeitachse", label=_("Timeline"), icon="timeline")

            # Plain columns — avoids QTabPanels v-if lazy rendering
            with ui.column().classes("w-full").style(
                "flex:1; overflow:hidden; padding:0;"
            ) as _karten_col:
                _cluster_karten()
            with ui.column().classes("w-full").style(
                "flex:1; overflow:hidden; padding:0;"
            ) as _zeitachse_col:
                _cluster_zeitachse()
            _zeitachse_col.set_visibility(False)

    _cluster_tabs.on_value_change(lambda e: (
        _karten_col.set_visibility(e.value == "karten"),
        _zeitachse_col.set_visibility(e.value == "zeitachse"),
    ))
    cluster_dlg.on("hide", lambda *_: _cluster_history.clear())

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _refresh_tabs(doc_id: int, ref_idx: int) -> None:
        refs = _active_refs[0]
        root_id = _cluster_history[0] if _cluster_history else doc_id
        if not refs:
            _cluster_karten.refresh()
            _cluster_zeitachse.refresh()
            return
        selected = refs[ref_idx]
        cluster_ids = sorted({doc_id} | set(selected["matching_ids"]))
        try:
            docs = await get_session_paperless().list_documents(ids=cluster_ids)
        except Exception:
            ui.notify(_("Error loading"), type="negative")
            return
        # TODO i18n-plural
        cluster_title_lbl.set_text(
            _("Cluster #{id} — {n} documents").format(id=doc_id, n=len(docs))
        )
        _pinned = get_pinned_ids_fn()
        sorted_docs = sorted(docs, key=lambda d: d.created)
        _cluster_karten.refresh(docs=docs, pinned=_pinned)
        _cluster_zeitachse.refresh(sorted_docs=sorted_docs, root_id=root_id, pinned=_pinned)

    async def _render_cluster_content(doc_id: int) -> None:
        _current_doc_id[0] = doc_id
        cluster_back_btn.set_visibility(len(_cluster_history) > 1)
        related = cross_ref_index.get_related(doc_id)
        relevant_refs = sorted(
            [r for r in related if r.get("matching_ids")],
            key=lambda r: len(r["matching_ids"]),
            reverse=True,
        )
        _active_refs[0] = relevant_refs
        if not relevant_refs:
            cluster_title_lbl.set_text(_("Cluster #{id} — no matches").format(id=doc_id))
            ref_select.set_visibility(False)
            _cluster_karten.refresh()
            _cluster_zeitachse.refresh()
            return
        options = {
            i: _("{type}: {value}  ({n} docs)").format(
                type=r['type'], value=r['value'], n=len(r['matching_ids'])
            )
            for i, r in enumerate(relevant_refs)
        }
        _loading[0] = True
        ref_select.options = options
        ref_select.value = 0
        ref_select.update()
        ref_select.set_visibility(True)
        _loading[0] = False
        await _refresh_tabs(doc_id, 0)

    async def _on_ref_change(e) -> None:
        if not _loading[0] and e.value is not None:
            await _refresh_tabs(_current_doc_id[0], e.value)

    ref_select.on_value_change(_on_ref_change)

    async def open_cluster(doc_id: int) -> None:
        _cluster_history.append(doc_id)
        cluster_title_lbl.set_text(_("Loading…"))
        cluster_dlg.open()
        await _render_cluster_content(doc_id)

    async def _cluster_go_back() -> None:
        if len(_cluster_history) > 1:
            _cluster_history.pop()
            await _render_cluster_content(_cluster_history[-1])

    cluster_back_btn.on_click(_cluster_go_back)

    return open_cluster
