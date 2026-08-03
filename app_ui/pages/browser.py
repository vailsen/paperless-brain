# app_ui/pages/browser.py
import re

from nicegui import app as ng_app, ui

from app_ui.cluster_dialog import create_cluster_dialog
from app_ui.document_dialog import create_document_dialog
from app_ui.layout import page_layout, require_auth
from app_ui.tag_style import render_tag_chips
from config.settings import settings
from i18n import get_translator
from models.result_document import DocumentResult
from pipelines.search import search
from services.clients import cross_ref_index, get_session_paperless, sidecar_service

# Sort field identifiers (keys are stable; German labels translated at render).
SORT_OPTIONS: dict[str, str] = {
    "created": "Erstellt",
    "title": "Titel",
    "correspondent": "Korrespondent",
    "document_type": "Dokumententyp",
    "relevance_score": "Relevanz",
}

SORT_REVERSE: dict[str, bool] = {
    "created": True,
    "title": False,
    "correspondent": False,
    "document_type": False,
    "relevance_score": False,
}


def _date_filter_widget(label: str) -> dict:
    _ = get_translator()
    state = {"mode": "="}
    # Keys are stable operators (used in logic); only labels are translated.
    _mode_options = {
        "=": _("Exactly"),
        ">": _("After"),
        "<": _("Before"),
        "von / bis": _("Range"),
    }
    with ui.column().classes("gap-1 min-w-fit"):
        ui.label(label).classes("text-xs text-gray-500 font-semibold uppercase tracking-wider")
        with ui.row().classes("items-center gap-2 flex-nowrap"):
            mode_select = (
                ui.select(_mode_options, value="=")
                .classes("w-24")
                .props("dense outlined dark")
            )
            date_single = ui.date_input(label="").props("dense outlined dark")
            date_range = ui.date_input(label="", range_input=True).props(
                "dense outlined dark"
            )
            date_range.set_visibility(False)

    def on_mode_change(e) -> None:
        state["mode"] = e.value
        is_range = e.value == "von / bis"
        date_single.set_visibility(not is_range)
        date_range.set_visibility(is_range)

    mode_select.on_value_change(on_mode_change)

    def get_filter() -> dict | None:
        mode = state["mode"]
        if mode == "von / bis":
            val = date_range.value
            if not val:
                return None
            after, before = val.split(" - ")
            return {"after": after, "before": before}
        val = date_single.value
        if not val:
            return None
        if mode == "=":
            return {"after": val, "before": val}
        if mode == ">":
            return {"after": val}
        if mode == "<":
            return {"before": val}
        return None

    return {"get_filter": get_filter}


@ui.page("/browser")
async def browser():
    if not require_auth():
        return
    page_layout()
    _ = get_translator()
    ui.add_head_html("""<style>
    html, body { overflow: hidden !important; }
    .q-page { min-height: 0 !important; overflow: hidden !important; }
    /* NiceGUI wraps the page in .nicegui-content with 1rem padding — the
       header band and the card grid bring their own spacing. */
    .nicegui-content { padding: 0 !important; }
    .doc-card {
        border: 1px solid var(--c-border);
        transition: transform .15s ease, box-shadow .15s ease, border-color .15s ease;
    }
    .doc-card:hover {
        transform: translateY(-3px);
        box-shadow: 0 10px 28px rgba(0,0,0,.45);
        border-color: rgba(167,139,250,.55);
    }
    .doc-thumb { cursor: pointer; transition: opacity .15s ease; }
    .doc-thumb:hover { opacity: .85; }
    /* NiceGUI gives .q-scrollarea__content 1rem padding — stacks with the
       card row's own padding and squeezes the cards on mobile. */
    .browser-results .q-scrollarea__content { padding: 0 !important; }
    @media (max-width: 767px) {
        /* Grid instead of flex: cards always fill the full row width.
           Scoped selectors beat the global .doc-card{width:312px} from
           layout.py's mobile CSS. */
        .browser-results .doc-cards-row {
            display: grid !important;
            grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
            /* start, not stretch — the shorter card of a row would otherwise
               be inflated with empty space */
            align-items: start;
            gap: 10px !important;
            padding: 10px !important;
            width: 100%;
        }
        .browser-results .doc-card { width: 100% !important; }
        .browser-header {
            padding-left: 12px !important; padding-right: 12px !important;
            padding-top: 12px !important;
        }
    }
    </style>""")

    # Register the shared detail dialog for this page instance.
    open_document, _doc_dlg = create_document_dialog(
        open_cluster_fn=lambda doc_id: open_cluster(doc_id),
        pin_fn=lambda r: _browser_pin(r),
        get_pin_state_fn=lambda doc_id: doc_id in _get_pinned_ids(),
    )

    _s = {"results": [], "sort": "created"}
    _search_seq = [0]

    def _get_pinned_ids() -> set[int]:
        return set(ng_app.storage.user.get("pinned_doc_ids") or [])

    def _browser_pin(result: DocumentResult) -> None:
        ids = _get_pinned_ids()
        cache: list[dict] = list(ng_app.storage.user.get("pinned_docs_cache") or [])
        if result.document.id in ids:
            ids.discard(result.document.id)
            cache = [c for c in cache if c["id"] != result.document.id]
            ui.notify(_("#{id} unpinned").format(id=result.document.id), timeout=1500)
        else:
            ids.add(result.document.id)
            cache.append({"id": result.document.id, "title": result.document.title})
            ui.notify(_("#{id} pinned").format(id=result.document.id), type="positive", timeout=1500)
        ng_app.storage.user["pinned_doc_ids"] = list(ids)
        ng_app.storage.user["pinned_docs_cache"] = cache
        render_results()

    # render_results / do_search reference variables defined later in the layout.
    # Python closures resolve names at call-time, so this is safe.

    def render_results() -> None:
        results_container.clear()
        if not _s["results"]:
            with results_container:
                with ui.column().classes(
                    "items-center justify-center w-full py-20 gap-3"
                ):
                    ui.icon(
                        "search_off" if _s.get("searched") else "travel_explore"
                    ).classes("text-6xl").style("color:var(--c-border-strong);")
                    ui.label(
                        _("No matches — adjust search term or filters.")
                        if _s.get("searched")
                        else _("No results yet — start a search.")
                    ).classes("text-sm text-gray-500")
            return
        key = _s["sort"]
        if key == "relevance_score" and any(
            r.relevance_score is not None for r in _s["results"]
        ):
            sorted_results = sorted(
                _s["results"],
                key=lambda r: (
                    r.relevance_score if r.relevance_score is not None else float("inf")
                ),
            )
        else:
            effective_key = key if key != "relevance_score" else "created"
            sorted_results = sorted(
                _s["results"],
                key=lambda r: getattr(r.document, effective_key, None) or "",
                reverse=SORT_REVERSE.get(effective_key, True),
            )
        _pinned = _get_pinned_ids()
        with results_container:
            # TODO i18n-plural
            ui.label(
                _("{n} documents").format(n=len(sorted_results))
            ).classes("text-xs text-gray-500 px-4 pt-3")
            with ui.row().classes("flex-wrap gap-4 p-4 doc-cards-row"):
                for r in sorted_results:
                    _render_card(
                        r, open_document, open_cluster,
                        on_pin=_browser_pin,
                        is_pinned=r.document.id in _pinned,
                    )

    async def do_search() -> None:
        _search_seq[0] += 1
        seq = _search_seq[0]
        _s["searched"] = True
        search_btn.props(add="loading")

        # Direct ID lookup — bypass all other filters
        if id_input.value:
            doc_id = int(id_input.value)
            try:
                doc = await get_session_paperless().get_document(doc_id)
                new_results = [DocumentResult(
                    document=doc,
                    has_actions=sidecar_service.has_actions(doc_id),
                )]
            except Exception:
                new_results = []
            search_btn.props(remove="loading")
            _s["results"] = new_results
            _s["sort"] = "created"
            sort_select.set_value("created")
            render_results()
            return

        filters = {
            "query": text_input.value or None,
            "correspondent": correspondents_input.value or None,
            "document_type": doctype_input.value or None,
            "tags": tags_input.value or None,
            "created": created_filter["get_filter"](),
            "added": added_filter["get_filter"](),
        }
        try:
            new_results = await search(
                filters=filters,
                semantic_query=semantic_input.value or None,
                paperless_client=get_session_paperless(),
                owner=owner_input.value if owner_input.value else None,
            )
        except Exception:
            new_results = []
        search_btn.props(remove="loading")
        if seq != _search_seq[0]:
            return
        _s["results"] = new_results
        new_sort = "relevance_score" if semantic_input.value else "created"
        if _s["sort"] != new_sort:
            _s["sort"] = new_sort
            sort_select.set_value(new_sort)
        render_results()

    _filters_open = [False]

    with ui.column().classes("w-full gap-0 bg-gray-900").style(
        "height:calc(100dvh - var(--q-header-height,52px)); overflow:hidden;"
    ):
        # ── Search header ─────────────────────────────────────────────────────
        with ui.column().classes(
            "browser-header w-full bg-gray-800 border-b border-gray-700 px-6 pt-5 pb-4 gap-0 flex-shrink-0"
        ):
            # ── Hero search row ───────────────────────────────────────────────
            with ui.row().classes("w-full items-center gap-3"):
                with ui.row().classes(
                    "items-center gap-3 flex-1 bg-gray-800 rounded-xl"
                    " px-4 py-2 border-2 border-purple-600/70"
                ):
                    ui.icon("auto_awesome").classes("text-gray-400 text-xl flex-shrink-0")
                    semantic_input = (
                        ui.input(
                            placeholder=_("AI search — what are you looking for? e.g. 'dentist invoice 2024'...")
                        )
                        .classes("flex-1")
                        .props("borderless dark clearable")
                    )
                    semantic_input.on("keydown.enter", do_search)
                search_btn = (
                    ui.button(_("Search"), icon="search", on_click=do_search)
                    .props("color=purple unelevated")
                    .classes("text-sm font-semibold px-5")
                )

            # ── Toolbar row ───────────────────────────────────────────────────
            with ui.row().classes("w-full items-center justify-between mt-3"):
                def _toggle_filters() -> None:
                    _filters_open[0] = not _filters_open[0]
                    filter_panel.set_visibility(_filters_open[0])
                    _filter_icon.props(
                        f"name={'expand_less' if _filters_open[0] else 'tune'}"
                    )

                with (
                    ui.row()
                    .classes("items-center gap-1 cursor-pointer select-none")
                    .on("click", _toggle_filters)
                ):
                    _filter_icon = ui.icon("tune", size="xs").classes("text-gray-500")
                    ui.label(_("Advanced filters")).classes("text-xs text-gray-500")

                _sort_labels = {
                    "created": _("Created"),
                    "title": _("Title"),
                    "correspondent": _("Correspondent"),
                    "document_type": _("Document type"),
                    "relevance_score": _("Relevance"),
                }
                sort_select = (
                    ui.select(_sort_labels, value="created", label=_("Sort"))
                    .classes("w-40")
                    .props("dense outlined dark")
                )

                def _on_sort_change(e) -> None:
                    val = e.value
                    if val and isinstance(val, str) and val in SORT_OPTIONS:
                        _s["sort"] = val
                        render_results()

                sort_select.on_value_change(_on_sort_change)

            # ── Collapsible filter panel ──────────────────────────────────────
            with ui.element("div").style(
                "margin-top:12px; padding:16px 20px; border-radius:10px;"
                "background:var(--c-bg); border:1px solid var(--c-border);"
            ) as filter_panel:
                filter_panel.set_visibility(False)

                # ── Row 1: text + ID ─────────────────────────────────────────
                with ui.row().classes("w-full items-center gap-3 flex-wrap").style("margin-bottom:12px;"):
                    text_input = (
                        ui.input(placeholder=_("Title / content (full text)..."))
                        .classes("flex-1")
                        .props("dense outlined dark")
                    )
                    text_input.on("keydown.enter", do_search)

                    id_input = (
                        ui.number(placeholder=_("Doc ID"), min=1, step=1, format="%d")
                        .classes("w-28")
                        .props("dense outlined dark")
                        .tooltip(_("Direct lookup by Paperless ID (#NNN)"))
                    )
                    id_input.on("keydown.enter", do_search)

                # ── Row 2: category filters ───────────────────────────────────
                with ui.element("div").style(
                    "display:grid; grid-template-columns:repeat(auto-fill,minmax(160px,1fr));"
                    "gap:10px; width:100%; margin-bottom:16px;"
                ):
                    tags_input = (
                        ui.select([], multiple=True, value=[], label=_("Tags"))
                        .classes("w-full")
                        .props("use-chips use-input fill-input outlined dark dense")
                    )

                    async def refresh_tags() -> None:
                        tags_input.options = await get_session_paperless().get_tag_map()
                        tags_input.update()

                    tags_input.on("focus", refresh_tags)

                    correspondents_input = (
                        ui.select([], multiple=True, value=[], label=_("Correspondent"))
                        .classes("w-full")
                        .props("use-chips use-input fill-input outlined dark dense")
                    )

                    async def refresh_correspondents() -> None:
                        correspondents_input.options = await get_session_paperless().get_correspondent_map()
                        correspondents_input.update()

                    correspondents_input.on("focus", refresh_correspondents)

                    doctype_input = (
                        ui.select([], multiple=True, value=[], label=_("Document type"))
                        .classes("w-full")
                        .props("use-chips use-input fill-input outlined dark dense")
                    )

                    async def refresh_doctype() -> None:
                        doctype_input.options = await get_session_paperless().get_document_types_map()
                        doctype_input.update()

                    doctype_input.on("focus", refresh_doctype)

                    owner_input = (
                        ui.select({}, value=None, label=_("Owner"))
                        .classes("w-full")
                        .props("outlined dark dense clearable")
                    )

                    async def refresh_owners() -> None:
                        users = await get_session_paperless().list_users()
                        owner_input.options = {uid: name for uid, name in users.items()}
                        owner_input.update()

                    owner_input.on("focus", refresh_owners)

                # ── Row 3: date filters ───────────────────────────────────────
                ui.separator().style("border-color:var(--c-surface); margin-bottom:12px;")
                with ui.row().classes("items-end gap-8 flex-wrap"):
                    created_filter = _date_filter_widget(_("Created"))
                    added_filter = _date_filter_widget(_("Added"))

        # ── Results ───────────────────────────────────────────────────────────
        with ui.scroll_area().classes("browser-results flex-1 w-full"):
            results_container = ui.element("div").classes("w-full")
        render_results()

    # ── Querverweis-Cluster dialog ────────────────────────────────────────────
    open_cluster = create_cluster_dialog(
        open_document_fn=open_document,
        pin_fn=lambda r: _browser_pin(r),
        get_pinned_ids_fn=lambda: _get_pinned_ids(),
        render_card_fn=_render_card,
    )

    # Re-render cards after the dialog closes — NiceGUI's client re-sync on
    # dialog hide can drop dynamically-added children from results_container.
    _doc_dlg.on("hide", lambda _: render_results())



# ── Module-level card renderer ────────────────────────────────────────────────


def _render_card(result: DocumentResult, on_eye, on_cluster=None, on_pin=None, is_pinned: bool = False) -> None:
    _ = get_translator()
    doc = result.document
    _pin_border = "border: 2px solid #a78bfa !important; box-shadow: 0 0 8px #a78bfa44;" if is_pinned else ""
    with ui.card().classes("w-52 hover:shadow-xl bg-gray-800 gap-0 doc-card").style(
        "position:relative; overflow:hidden; " + _pin_border
    ):
        ui.element("div").classes("w-full rounded-t doc-thumb").style(
            f"height:9rem; background-image:url('/thumbnails/{doc.id}.jpg');"
            "background-size:cover; background-position:top center;"
        ).on("click", lambda r=result: on_eye(r))
        if is_pinned:
            ui.icon("push_pin", size="xs").classes("text-purple-300").style(
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

                if settings.show_relevance_scores and result.relevance is not None:
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
                    ui.button(
                        icon="download",
                        on_click=lambda url=doc.pdf_url: ui.navigate.to(
                            url, new_tab=True
                        ),
                    ).props("flat dense dark").classes("card-action-btn").tooltip(
                        _("Download PDF")
                    )


