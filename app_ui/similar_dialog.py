# app_ui/similar_dialog.py
"""'Documents like this one' — nearest neighbours of one document.

The sibling of `cluster_dialog`, and deliberately not folded into it: a cluster
is documents that *state* the same reference number, which is a fact in the
documents. This is documents that *read* alike, which is a measurement. Mixing
them in one list would make a similarity guess look like a filed cross-reference.
"""

from typing import Callable

from nicegui import ui

from i18n import get_translator


def create_similar_dialog(
    open_document_fn: Callable,
    pin_fn: Callable,
    get_pinned_ids_fn: Callable[[], set],
    render_card_fn: Callable,
    max_results: int | None = None,
) -> Callable:
    """Factory: build one persistent dialog per page and return
    ``open_similar(doc_id)``.

    Same late-binding contract as `create_cluster_dialog`: callers pass lambdas
    so names defined after this call still resolve at event time.
    """
    _ = get_translator()
    _state: dict = {"results": [], "empty_reason": ""}

    async def _open_doc(res) -> None:
        # The document dialog would otherwise open underneath this one.
        similar_dlg.close()
        await open_document_fn(res)

    @ui.refreshable
    def _similar_cards() -> None:
        if _state["empty_reason"]:
            with ui.column().classes("w-full items-center justify-center p-8 gap-2"):
                ui.icon("search_off", size="lg").style("color:var(--c-text-muted)")
                ui.label(_state["empty_reason"]).classes("text-sm").style(
                    "color:var(--c-text-muted); text-align:center;"
                )
            return
        if not _state["results"]:
            return
        pinned = get_pinned_ids_fn() or set()
        with ui.scroll_area().style("height:100%;"):
            with ui.row().classes("flex-wrap gap-4 p-4 doc-cards-row"):
                for r in _state["results"]:
                    render_card_fn(
                        r, _open_doc, None,
                        on_pin=pin_fn,
                        is_pinned=r.document.id in pinned,
                        show_distance=True,
                    )

    ui.add_head_html("""<style>
@media (max-width: 767px) {
    .sim-dialog .q-dialog__inner { padding: 0 !important; }
    .sim-dialog .q-dialog__inner > div { max-height: 100dvh !important; border-radius: 0 !important; }
    .sim-card { height: 100dvh !important; }
}
</style>""")

    with ui.dialog().props("full-width").classes("sim-dialog") as similar_dlg:
        with ui.card().classes("sim-card").style(
            "width:100%; height:85vh; background:var(--c-surface); padding:0;"
            "display:flex; flex-direction:column;"
        ):
            with ui.row().classes(
                "w-full items-center gap-2 px-4 py-2 border-b border-gray-700"
            ).style("flex-shrink:0; flex-wrap:nowrap;"):
                title_lbl = ui.label("").classes(
                    "text-gray-100 font-semibold text-sm flex-1"
                ).style(
                    "white-space:nowrap; overflow:hidden;"
                    "text-overflow:ellipsis; min-width:0;"
                )
                ui.button(icon="close", on_click=similar_dlg.close).props(
                    "flat dark dense"
                ).classes("text-gray-400").style("flex-shrink:0;")
            with ui.column().classes("w-full").style(
                "flex:1; overflow:hidden; padding:0;"
            ):
                _similar_cards()

    async def open_similar(doc_id: int) -> None:
        from pipelines.similar import find_similar_documents
        from services.clients import chroma, get_session_paperless
        from werkbank.settings_store import get_search_max_results

        # Read per open, not per dialog build: changing the setting then takes
        # effect on the next search instead of on the next restart. Same knob
        # the semantic search uses -- one number for "how many results", not a
        # second one hidden in the code.
        n = max_results if max_results is not None else get_search_max_results()

        _state["results"] = []
        _state["empty_reason"] = ""
        title_lbl.set_text(_("Loading…"))
        _similar_cards.refresh()
        similar_dlg.open()
        try:
            results = await find_similar_documents(
                doc_id,
                n_results=n,
                paperless_client=get_session_paperless(),
            )
        except Exception as exc:
            _state["empty_reason"] = _("Error: {err}").format(err=exc)
            title_lbl.set_text(_("Similar to #{id}").format(id=doc_id))
            _similar_cards.refresh()
            return

        if not results:
            # "Nothing similar" and "never measured" are different answers, and
            # only the index knows which one this is.
            indexed = await chroma.get(
                where={"paperless_id": {"$eq": doc_id}}, limit=1
            )
            _state["empty_reason"] = (
                _("No similar documents found.")
                if indexed
                else _("Document #{id} is not indexed — re-ingest it to compare it "
                       "with the archive.").format(id=doc_id)
            )
        _state["results"] = results
        # TODO i18n-plural
        title_lbl.set_text(
            _("Similar to #{id} — {n} documents").format(id=doc_id, n=len(results))
        )
        _similar_cards.refresh()

    return open_similar
