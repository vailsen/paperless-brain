# app_ui/document_dialog.py
"""Shared document detail dialog.

Call ``create_document_dialog()`` once inside any ``@ui.page`` handler to
register the dialog in that page's context.  The returned coroutine
``open_document(result)`` downloads/caches the PDF and opens the dialog.
"""

import asyncio
import html as _html
import os
import re

from nicegui import ui

from config.settings import settings
from i18n import get_translator
from models.result_document import DocumentResult
from services.clients import cross_ref_index, paperless, sidecar_service

_PDF_CACHE = str(settings.app_path / "data" / "pdf_cache")
os.makedirs(_PDF_CACHE, exist_ok=True)

_TAG_COLORS = [
    "#ef4444",  # red       0°
    "#f97316",  # orange   30°
    "#facc15",  # yellow   55°
    "#84cc16",  # lime     85°
    "#10b981",  # emerald 161°
    "#06b6d4",  # cyan    192°
    "#3b82f6",  # blue    217°
    "#8b5cf6",  # violet  265°
    "#d946ef",  # fuchsia 294°
    "#ec4899",  # pink    328°
]


def tag_color(tag: str) -> str:
    # zlib.crc32, not hash(): built-in str hash is salted per process, which
    # would reshuffle every tag's color on each app restart.
    import zlib

    return _TAG_COLORS[zlib.crc32(tag.encode("utf-8")) % len(_TAG_COLORS)]


# ── Text highlighting ─────────────────────────────────────────────────────────


def _wrap_pre(html: str) -> str:
    return (
        '<pre style="white-space:pre-wrap;font-family:inherit;font-size:0.875rem;'
        f'color:var(--c-text-2);margin:0;line-height:1.6;">{html}</pre>'
    )


def _strip_chunk_prefix(chunk: str) -> str:
    """Remove the ingest-time context prefix 'Dokument: ..., ..., ... — ' if present."""
    if chunk.startswith("Dokument: "):
        idx = chunk.find(" — ")
        if idx != -1:
            return chunk[idx + 3 :]
    return chunk


def highlight_text(full_text: str, chunks: list[str], text_query: str = "") -> str:
    """Return an HTML <pre> block with matches highlighted.

    - Semantic chunk matches → purple
    - text_query (Titel/Inhalt) substring matches → amber
    """
    # Collect tagged regions: (start, end, style_key)
    # style_key: "chunk" = purple, "query" = amber
    tagged: list[tuple[int, int, str]] = []

    # ── Semantic chunk regions ────────────────────────────────────────────────
    for raw_chunk in chunks:
        clean = _strip_chunk_prefix(raw_chunk).strip()
        if not clean or len(clean) < 20:
            continue
        words = [w for w in re.split(r"\s+", clean) if w]
        if len(words) < 3:
            continue
        anchor = r"\s+".join(re.escape(w) for w in words[: min(6, len(words))])
        try:
            m = re.search(anchor, full_text, re.IGNORECASE)
        except re.error:
            continue
        if not m:
            continue
        start = m.start()
        if len(words) > 6:
            tail = r"\s+".join(re.escape(w) for w in words[-min(3, len(words)) :])
            try:
                tm = re.search(tail, full_text[start:], re.IGNORECASE)
                end = start + tm.end() if tm else min(start + len(clean) * 2, len(full_text))
            except re.error:
                end = m.end()
        else:
            end = m.end()
        tagged.append((start, min(end, len(full_text)), "chunk"))

    # ── text_query substring regions ──────────────────────────────────────────
    if text_query and len(text_query.strip()) >= 2:
        try:
            for m in re.finditer(re.escape(text_query.strip()), full_text, re.IGNORECASE):
                tagged.append((m.start(), m.end(), "query"))
        except re.error:
            pass

    if not tagged:
        return _wrap_pre(_html.escape(full_text))

    # Merge by priority: chunk (purple) wins over query (amber) on overlap.
    # Strategy: flatten into per-character style, then emit spans.
    style_map: dict[str, str] = {
        "chunk": "background:rgba(139,92,246,0.4);color:inherit;border-radius:3px;padding:0 2px;",
        "query": "background:rgba(251,191,36,0.4);color:inherit;border-radius:3px;padding:0 2px;",
    }
    # Sort: chunks first so they win on overlap
    tagged.sort(key=lambda t: (0 if t[2] == "chunk" else 1, t[0]))

    # Merge per style into non-overlapping intervals, chunk beats query
    covered: list[list[int | str]] = []  # [start, end, style]
    for s, e, style in tagged:
        if covered and s < covered[-1][1]:
            if covered[-1][2] == "chunk":
                continue  # chunk wins
            covered[-1][1] = max(covered[-1][1], e)
        else:
            covered.append([s, e, style])

    parts: list[str] = []
    pos = 0
    for s, e, style in covered:
        parts.append(_html.escape(full_text[pos:s]))
        parts.append(
            f'<mark style="{style_map[style]}">{_html.escape(full_text[s:e])}</mark>'
        )
        pos = e
    parts.append(_html.escape(full_text[pos:]))
    return _wrap_pre("".join(parts))


# ── Dialog factory ────────────────────────────────────────────────────────────


def create_document_dialog(open_cluster_fn=None, pin_fn=None, get_pin_state_fn=None):
    """Create the document detail dialog in the current NiceGUI page context.

    Returns ``(open_document, dlg)``:
    - ``open_document(result)`` — async callable; caches PDF and opens the dialog.
    - ``dlg`` — the raw ``ui.dialog`` element; attach ``dlg.on('hide', cb)`` in the
      calling page to restore any dynamically-rendered content after the dialog closes.
    """
    _ = get_translator()
    _active: list = [None]  # (DocumentResult, file_url, related_refs, related_docs)

    def _on_ref_click(doc_id: int) -> None:
        asyncio.ensure_future(_fetch_and_navigate(doc_id))

    async def _fetch_and_navigate(doc_id: int) -> None:
        try:
            doc = await paperless.get_document(doc_id)
            await open_document(DocumentResult(document=doc))
        except Exception:
            ui.notify(_("#{id} not found").format(id=doc_id), type="warning")

    ui.add_head_html("""<style>
.dl-table th {
    background: var(--c-surface) !important;
    color: var(--c-text-muted) !important;
    font-size: 0.75rem !important;
    padding: 6px 10px !important;
    white-space: normal !important;
}
.dl-table td {
    color: var(--c-text-2) !important;
    font-size: 0.75rem !important;
    padding: 4px 10px !important;
    white-space: normal !important;
    word-break: break-word;
    vertical-align: top;
}
.dl-table .q-table { background: transparent !important; }
.dl-table .q-table__container { border: 1px solid var(--c-border); border-radius: 4px; min-width: max-content; }
/* Actions table: fit the panel and wrap long descriptions instead of growing
   to content width (min-width: max-content would force horizontal scroll,
   truncating deadlines visually on mobile). */
.dl-actions .q-table__container { min-width: 0 !important; }
.dl-actions table { table-layout: fixed; width: 100%; }
.dl-actions td:nth-child(2) { white-space: nowrap !important; }
.dl-splitter > .q-splitter__panel {
    position: relative !important;
    overflow: hidden !important;
}
.dl-splitter > .q-splitter__panel:first-child {
    border-right: 1px solid var(--c-border) !important;
}
/* PDF button + thumbnail: hidden on desktop (PDF panel shows the doc there),
   visible on mobile */
.dl-pdf-btn { display: none !important; }
.dl-thumb { display: none; }
@media (max-width: 767px) {
    .dl-pdf-btn { display: flex !important; }
    .dl-thumb { display: block; flex-shrink: 0; }
    /* Hide PDF panel on mobile — tabs take full width */
    .dl-splitter > .q-splitter__panel.q-splitter__after { display: none !important; }
    .dl-splitter > .q-splitter__panel.q-splitter__before { width: 100% !important; min-width: 100% !important; }
    .dl-splitter .q-splitter__separator-area { display: none !important; }
    /* Header: full-width title as the real header, buttons row beneath —
       one shared row truncates long filenames */
    .dl-header { flex-direction: column !important; align-items: stretch !important; }
    .dl-header-btns { align-self: flex-end; margin-top: 0 !important; }
    /* Dialog takes the full viewport on mobile — no Quasar inner margins */
    .dl-doc-dialog .q-dialog__inner { padding: 0 !important; }
    .dl-doc-dialog .q-dialog__inner > div { max-height: 100dvh !important; border-radius: 0 !important; }
    .dl-doc-container { height: 100dvh !important; }
}
</style>""")

    # full-width prop lets Quasar stretch the panel to 100 vw.
    # Explicit height on the inner container defines the vertical extent.
    with ui.dialog().props("full-width").classes("dl-doc-dialog") as dlg:
        _container = ui.element("div").classes("dl-doc-container").style(
            "width:100%; height:95vh; overflow:hidden;"
        )

    def _refresh() -> None:
        _container.clear()
        if _active[0] is None:
            return
        result, file_url, related_refs, related_docs = _active[0]
        _is_pinned = get_pin_state_fn(result.document.id) if get_pin_state_fn else False
        with _container:
            _render_content(
                result, file_url, dlg, related_refs, related_docs,
                navigate_fn=_on_ref_click,
                open_cluster_fn=open_cluster_fn,
                pin_fn=pin_fn,
                is_pinned=_is_pinned,
            )

    async def open_document(result: DocumentResult) -> None:
        doc_id = result.document.id
        cached = os.path.join(_PDF_CACHE, f"{doc_id}.pdf")
        if not os.path.exists(cached):
            data = await paperless.download_document(doc_id)
            with open(cached, "wb") as f:
                f.write(data)

        # Resolve cross-reference matches using the inverted index
        related_refs = cross_ref_index.get_related(doc_id)
        all_related_ids: set[int] = set()
        for r in related_refs:
            all_related_ids.update(r["matching_ids"])
        related_docs: dict = {}
        if all_related_ids:
            try:
                fetched = await paperless.list_documents(ids=list(all_related_ids))
                related_docs = {d.id: d for d in fetched}
            except Exception:
                pass

        _active[0] = (result, f"/pdftmp/{doc_id}.pdf", related_refs, related_docs)
        _refresh()
        dlg.open()
        # On mobile: collapse the PDF panel so tabs fill full width.
        # CSS media query already handles this; JS is a belt-and-suspenders tweak
        # and must be best-effort because context may be missing in background tasks.
        try:
            await ui.run_javascript("""
                setTimeout(function() {
                    if (window.innerWidth >= 768) return;
                    var sp = document.querySelector('.dl-splitter');
                    if (!sp) return;
                    var after = sp.querySelector('.q-splitter__after');
                    if (after) after.style.setProperty('display', 'none', 'important');
                    var before = sp.querySelector('.q-splitter__before');
                    if (before) {
                        before.style.setProperty('width', '100%', 'important');
                        before.style.setProperty('min-width', '100%', 'important');
                    }
                    var sep = sp.querySelector('.q-splitter__separator-area');
                    if (sep) sep.style.setProperty('display', 'none', 'important');
                }, 80);
            """)
        except Exception:
            pass

    return open_document, dlg


# ── Dialog content ────────────────────────────────────────────────────────────

_TABS_H = "48px"


def render_document_body(
    result: DocumentResult,
    file_url: str,
    outer_style: str = "",
    related_refs: list | None = None,
    related_docs: dict | None = None,
    navigate_fn=None,
) -> None:
    """Render the content splitter: tabs (Volltext / Termine / Tabellen) + PDF viewer.

    ``outer_style`` controls the splitter's CSS. Defaults to flex layout for use inside
    a flex card. Pass absolute-positioning style for in-panel (non-dialog) use.
    """
    _ = get_translator()
    doc = result.document
    sidecar = sidecar_service.load_sidecar(doc.id)
    style = outer_style or "flex:1; min-height:0; overflow:hidden; width:100%;"

    with ui.splitter(value=50).classes("dl-splitter").style(style) as _splitter:
        # ── Left: tabs ────────────────────────────────────────────────────────
        with _splitter.before:
            with (
                ui.tabs(value="zusammenfassung")
                .props("dense align='left'")
                .classes("bg-gray-800 text-gray-300")
                .style(
                    f"position:absolute; top:0; left:0; right:0; height:{_TABS_H};"
                ) as tabs
            ):
                ui.tab("zusammenfassung", label=_("Summary"), icon="info")
                ui.tab("volltext", label=_("Full text"), icon="article")
                ui.tab("aktionen", label=_("Dates / References"), icon="event_note")
                ui.tab("tabellen", label=_("Tables"), icon="table_chart")

            with (
                ui.tab_panels(tabs, value="zusammenfassung")
                .style(
                    f"position:absolute; top:{_TABS_H}; left:0; right:0; bottom:0;"
                    "overflow:hidden;"
                )
                .classes("bg-transparent")
            ):
                # ── Zusammenfassung & Metadaten ───────────────────────────────
                with ui.tab_panel("zusammenfassung").style(
                    "padding:0; height:100%; overflow:hidden;"
                ):
                    with ui.scroll_area().style("height:100%; width:100%;"):
                        with ui.column().classes("p-4 gap-3"):
                            ui.label(_("Metadata")).classes(
                                "text-xs font-semibold text-gray-500 uppercase tracking-wide"
                            )
                            # Thumbnail right of the metadata — mobile only
                            # (.dl-thumb); desktop shows the PDF panel instead.
                            with ui.row().classes("w-full no-wrap gap-3 items-start"):
                                with ui.column().classes("flex-1 gap-3 min-w-0"):
                                    if doc.correspondent:
                                        with ui.row().classes("items-center gap-2"):
                                            ui.icon("person", size="xs").classes("text-gray-400")
                                            ui.label(doc.correspondent).classes("text-sm text-gray-300")
                                    if doc.document_type:
                                        with ui.row().classes("items-center gap-2"):
                                            ui.icon("description", size="xs").classes("text-gray-400")
                                            ui.label(doc.document_type).classes("text-sm text-gray-300")
                                    if doc.created:
                                        with ui.row().classes("items-center gap-2"):
                                            ui.icon("calendar_today", size="xs").classes("text-gray-400")
                                            ui.label(doc.created.strftime("%d.%m.%Y")).classes("text-sm text-gray-300")
                                    if doc.page_count:
                                        with ui.row().classes("items-center gap-2"):
                                            ui.icon("pages", size="xs").classes("text-gray-400")
                                            ui.label(_("{n} pages").format(n=doc.page_count)).classes("text-sm text-gray-300")
                                    if doc.owner_name:
                                        with ui.row().classes("items-center gap-2"):
                                            ui.icon("account_circle", size="xs").classes("text-gray-400")
                                            ui.label(doc.owner_name).classes("text-sm text-gray-300")
                                    if doc.tags:
                                        with ui.row().classes("flex-wrap gap-1 items-center"):
                                            ui.icon("label", size="xs").classes("text-gray-400")
                                            for _tag in doc.tags:
                                                # color= (not .style) — the badge's default
                                                # bg-primary class wins over inline style.
                                                ui.badge(_tag, color=tag_color(_tag)).style(
                                                    "color:white;font-size:10px;"
                                                )
                                with ui.element("div").classes("dl-thumb"):
                                    ui.image(f"/thumbnails/{doc.id}.jpg").style(
                                        "width:33vw;max-width:190px;"
                                        "border-radius:4px;"
                                        "border:1px solid var(--c-border);"
                                    )

                            _summary = None
                            if sidecar:
                                _summary = (
                                    sidecar.get("full_summary_summarized")
                                    or sidecar.get("full_summary")
                                )
                            if _summary:
                                ui.separator().classes("my-1")
                                ui.label(_("Summary")).classes(
                                    "text-xs font-semibold text-gray-500 uppercase tracking-wide"
                                )
                                ui.label(_summary).classes(
                                    "text-sm text-gray-300 leading-relaxed"
                                )

                            if doc.notes:
                                ui.separator().classes("my-1")
                                ui.label(_("Notes")).classes(
                                    "text-xs font-semibold text-gray-500 uppercase tracking-wide"
                                )
                                for _note in doc.notes:
                                    _note_user = (
                                        _note.user.get("username")
                                        or _note.user.get("display_name")
                                        or "?"
                                    )
                                    _note_date = _note.created.strftime("%d.%m.%Y")
                                    with ui.element("div").classes(
                                        "bg-gray-900 border border-gray-700 rounded p-2 w-full"
                                    ):
                                        ui.label(f"{_note_date} · {_note_user}").classes(
                                            "text-xs text-gray-500 mb-1"
                                        )
                                        ui.label(_note.note).classes(
                                            "text-sm text-gray-300 whitespace-pre-wrap"
                                        )

                            _actions = sidecar.get("actions") if sidecar else None
                            if _actions:
                                ui.separator().classes("my-1")
                                ui.label(_("Deadlines / Actions")).classes(
                                    "text-xs font-semibold text-gray-500 uppercase tracking-wide"
                                )
                                for _a in _actions[:5]:
                                    _certain = "●" if _a.get("deadline_certain") else "○"
                                    _dl = _a.get("deadline", "—")
                                    _desc = _a.get("description", "") or ""
                                    with ui.row().classes("items-start gap-2 no-wrap"):
                                        ui.label(_certain).classes("text-gray-400 text-xs flex-shrink-0 mt-0.5")
                                        ui.label(f"{_dl} — {_desc}").classes(
                                            "text-xs text-gray-300 whitespace-normal break-words min-w-0"
                                        )
                                if len(_actions) > 5:
                                    ui.label(
                                        _("+{n} more in the \"Dates / References\" tab").format(n=len(_actions) - 5)
                                    ).classes("text-xs text-gray-500 italic")

                # ── Volltext ──────────────────────────────────────────────────
                with ui.tab_panel("volltext").style(
                    "padding:0; height:100%; overflow:hidden;"
                ):
                    with ui.scroll_area().style("height:100%; width:100%;"):
                        with ui.column().classes("p-4 gap-2"):
                            if sidecar and sidecar.get("full_text"):
                                full_text: str = sidecar["full_text"]
                                if result.matched_chunks or result.text_query:
                                    ui.html(
                                        highlight_text(
                                            full_text,
                                            result.matched_chunks,
                                            result.text_query,
                                        ),
                                        sanitize=False,
                                    )
                                else:
                                    ui.label(full_text).classes(
                                        "text-sm text-gray-300 whitespace-pre-wrap"
                                    )
                            else:
                                ui.label(
                                    _("No extracted text available.")
                                ).classes("text-sm text-gray-500")

                # ── Termine / Aktionen ────────────────────────────────────────
                with ui.tab_panel("aktionen").style(
                    "padding:0; height:100%; overflow:hidden;"
                ):
                    with ui.scroll_area().style("height:100%; width:100%;"):
                        with ui.column().classes("p-4 gap-3"):
                            actions = sidecar.get("actions") if sidecar else None
                            cross_refs = (
                                sidecar.get("cross_refs") if sidecar else None
                            )
                            if actions:
                                ui.label(_("Actions")).classes(
                                    "text-sm font-semibold text-blue-400"
                                )
                                with ui.element("div").classes("dl-table dl-actions w-full"):
                                    ui.table(
                                        columns=[
                                            {
                                                "name": "desc",
                                                "label": _("Description"),
                                                "field": "desc",
                                                "align": "left",
                                                "style": "width:64%",
                                            },
                                            {
                                                "name": "deadline",
                                                "label": _("Deadline"),
                                                "field": "deadline",
                                                "align": "left",
                                                "style": "width:24%",
                                            },
                                            {
                                                "name": "certain",
                                                "label": _("Certain"),
                                                "field": "certain",
                                                "align": "left",
                                                "style": "width:12%",
                                            },
                                        ],
                                        rows=[
                                            {
                                                "desc": a.get("description", ""),
                                                "deadline": a.get("deadline", ""),
                                                "certain": _("Yes")
                                                if a.get("deadline_certain")
                                                else _("No"),
                                            }
                                            for a in actions
                                        ],
                                    ).classes("w-full").props("flat dense dark")
                            else:
                                ui.label(_("No actions detected.")).classes(
                                    "text-sm text-gray-500"
                                )
                            if cross_refs:
                                ui.separator().classes("my-2")
                                ui.label(_("Cross-references")).classes(
                                    "text-sm font-semibold text-blue-400"
                                )
                                # Build enriched ref lookup
                                _ref_lookup: dict[str, list[int]] = {}
                                if related_refs:
                                    for _r in related_refs:
                                        _ref_lookup[_r["value"]] = _r["matching_ids"]

                                _seen_ref_keys: set = set()
                                _unique_cross_refs = []
                                for _ref in cross_refs:
                                    _key = (_ref.get("type", ""), (_ref.get("value") or "").strip())
                                    if _key not in _seen_ref_keys:
                                        _seen_ref_keys.add(_key)
                                        _unique_cross_refs.append(_ref)
                                cross_refs = _unique_cross_refs

                                with ui.element("div").classes("w-full mt-1"):
                                    for _ref in cross_refs:
                                        _val = (_ref.get("value") or "").strip()
                                        _rtype = _ref.get("type", "")
                                        _matches = _ref_lookup.get(_val, [])
                                        with ui.element("div").style(
                                            "padding:5px 6px; border-bottom:1px solid var(--c-surface);"
                                        ):
                                            with ui.row().classes("items-center gap-2 flex-wrap"):
                                                if _rtype:
                                                    ui.label(_rtype).style(
                                                        "font-size:10px;background:var(--c-border);"
                                                        "color:var(--c-text-muted);border-radius:3px;"
                                                        "padding:1px 5px;flex-shrink:0;"
                                                    )
                                                ui.label(_val).classes(
                                                    "text-xs text-gray-200 font-medium"
                                                )
                                            if _matches and related_docs:
                                                with ui.element("div").style(
                                                    "margin-top:4px; margin-left:8px;"
                                                    "display:flex; flex-wrap:wrap; gap:6px 16px;"
                                                ):
                                                    for _mid in _matches:
                                                        _d = related_docs.get(_mid)
                                                        if _d:
                                                            _t = (_d.title or "?")[:40]
                                                            with ui.row().classes(
                                                                "items-center gap-1 flex-nowrap"
                                                            ):
                                                                _id_lbl = ui.label(
                                                                    f"#{_mid}"
                                                                ).style(
                                                                    "font-size:11px;"
                                                                    "color:#a78bfa;"
                                                                    "font-family:monospace;"
                                                                    "font-weight:600;"
                                                                    "cursor:pointer;"
                                                                    "text-decoration:underline;"
                                                                )
                                                                if navigate_fn:
                                                                    _id_lbl.on(
                                                                        "click",
                                                                        lambda m=_mid: navigate_fn(m),
                                                                    )
                                                                ui.label(_t).style(
                                                                    "font-size:11px;color:var(--c-text-muted);"
                                                                )

                # ── Tabellen ──────────────────────────────────────────────────
                with ui.tab_panel("tabellen").style(
                    "padding:0; height:100%; overflow:hidden;"
                ):
                    with ui.scroll_area().style("height:100%; width:100%;"):
                        with ui.column().classes("p-4 gap-4"):
                            tables = sidecar.get("tables") if sidecar else None
                            if tables:
                                for i, tbl in enumerate(tables):
                                    if not tbl:
                                        continue
                                    caption = tbl.get("caption") or tbl.get("title")
                                    page_num = tbl.get("page_number", None)
                                    ui.label(_("Page: {page}").format(page=str(page_num))).classes(
                                        "text-xs font-semibold text-gray-400"
                                    )
                                    ui.label(caption).classes(
                                        "text-xs font-semibold text-gray-400"
                                    )
                                    rows_data = [
                                        r
                                        for r in (tbl.get("rows") or tbl.get("data") or [])
                                        if isinstance(r, dict)
                                    ]
                                    # Union of keys in first-appearance order — the
                                    # extractor sometimes omits keys in single rows;
                                    # first-row-only headers would blank those cells.
                                    headers: list = []
                                    for _r in rows_data:
                                        for _k in _r.keys():
                                            if _k not in headers:
                                                headers.append(_k)
                                    # No key shared by all rows → not a real grid but
                                    # extracted form-field pairs (each "row" = its own
                                    # label/value set). Render as Feld/Wert list.
                                    _is_form_pairs = len(rows_data) > 1 and not set(
                                        rows_data[0].keys()
                                    ).intersection(*[set(r.keys()) for r in rows_data[1:]])
                                    if _is_form_pairs:
                                        with ui.element("div").classes("dl-table dl-actions w-full"):
                                            ui.table(
                                                columns=[
                                                    {"name": "feld", "label": _("Field"), "field": "feld", "align": "left", "style": "width:45%"},
                                                    {"name": "wert", "label": _("Value"), "field": "wert", "align": "left", "style": "width:55%"},
                                                ],
                                                rows=[
                                                    {"feld": str(_k), "wert": str(_v)}
                                                    for _r in rows_data
                                                    for _k, _v in _r.items()
                                                ],
                                            ).classes("w-full").props("flat dense dark")
                                    elif headers and rows_data:
                                        with ui.element("div").style("overflow-x:auto; max-width:100%; display:block"):
                                            with ui.element("div").classes("dl-table"):
                                                ui.table(
                                                    columns=[
                                                        {
                                                            "name": str(h),
                                                            "label": str(h),
                                                            "field": str(h),
                                                            "align": "left",
                                                            "sortable": True,
                                                        }
                                                        for h in headers
                                                    ],
                                                    rows=rows_data,
                                                ).props("flat dense dark")
                                    else:
                                        raw = tbl.get("text") or ""
                                        if raw:
                                            ui.label(raw).classes(
                                                "text-xs text-gray-300 whitespace-pre-wrap"
                                            )
                                        else:
                                            ui.label(_("(empty table)")).classes(
                                                "text-xs text-gray-500 italic"
                                            )
                                    if i < len(tables) - 1:
                                        ui.separator().classes("my-1")
                            else:
                                ui.label(_("No tables detected.")).classes(
                                    "text-sm text-gray-500"
                                )

        # ── Right: PDF viewer ─────────────────────────────────────────────────
        with _splitter.after:
            ui.html(
                f'<object data="{file_url}" type="application/pdf"'
                f' width="100%" height="100%"'
                f' style="border:none;position:absolute;top:0;left:0;'
                f'width:100%;height:100%;">'
                f'<p style="color:var(--c-text-muted);padding:1rem;">'
                f"{_("PDF cannot be displayed.")}</p>"
                f"</object>",
                sanitize=False,
            )


def _render_content(
    result: DocumentResult,
    file_url: str,
    dlg,
    related_refs: list | None = None,
    related_docs: dict | None = None,
    navigate_fn=None,
    open_cluster_fn=None,
    pin_fn=None,
    is_pinned: bool = False,
) -> None:
    _ = get_translator()
    doc = result.document
    sidecar = sidecar_service.load_sidecar(doc.id)

    with (
        ui.card()
        .classes("w-full h-full rounded-none bg-gray-900 p-0 gap-0")
        .style("display:flex; flex-direction:column;")
    ):
        # ── Header ────────────────────────────────────────────────────────────
        with (
            ui.row()
            .classes(
                "dl-header w-full items-start justify-between bg-gray-800"
                " border-b border-gray-700 px-4 py-3 gap-2"
            )
            .style("flex-shrink:0;")
        ):
            with ui.column().classes("flex-1 gap-1 overflow-hidden"):
                with ui.row().classes("items-center gap-2"):
                    ui.label(result.display_title).classes(
                        "text-base font-bold text-gray-100 leading-tight"
                    ).style("word-break:break-word;")
                    ui.label(f"#{doc.id}").classes("text-sm text-gray-500")
                with ui.row().classes("flex-wrap gap-2 items-center"):
                    if doc.correspondent:
                        with ui.row().classes("items-center gap-1"):
                            ui.icon("person", size="xs").classes("text-gray-400")
                            ui.label(doc.correspondent).classes("text-xs text-gray-300")
                    if doc.document_type:
                        with ui.row().classes("items-center gap-1"):
                            ui.icon("description", size="xs").classes("text-gray-400")
                            ui.label(doc.document_type).classes("text-xs text-gray-300")
                    if result.display_date:
                        with ui.row().classes("items-center gap-1"):
                            ui.icon("calendar_today", size="xs").classes(
                                "text-gray-400"
                            )
                            ui.label(result.display_date).classes(
                                "text-xs text-gray-400"
                            )
                    if result.relevance_score is not None:
                        ui.badge(
                            _("Score {score:.3f}").format(score=result.relevance_score), color="purple"
                        ).classes("text-xs")
                    for tag in doc.tags:
                        ui.badge(tag, color=tag_color(tag)).style(
                            "color:white;font-size:10px;"
                        )
            with ui.row().classes(
                "dl-header-btns self-start mt-1 flex-shrink-0 items-center gap-1"
            ):
                if open_cluster_fn and cross_ref_index.has_related(doc.id):
                    ui.button(
                        icon="hub",
                        on_click=lambda _id=doc.id: open_cluster_fn(_id),
                    ).props("flat dark dense").classes("text-purple-400").tooltip(_("Cross-reference cluster"))
                if pin_fn:
                    _pin_state = [is_pinned]
                    def _do_pin(ps=_pin_state, r=result):
                        pin_fn(r)
                        ps[0] = not ps[0]
                        _pin_btn.classes(
                            remove="text-purple-400 text-gray-400",
                            add="text-purple-400" if ps[0] else "text-gray-400",
                        )
                    _pin_btn = (
                        ui.button(icon="push_pin", on_click=_do_pin)
                        .props("flat dark dense")
                        .classes("text-purple-400" if is_pinned else "text-gray-400")
                        .tooltip(_("Pinned — unpin") if is_pinned else _("Pin"))
                    )
                with ui.element("div").classes("dl-pdf-btn"):
                    ui.button(
                        "PDF", icon="picture_as_pdf",
                        on_click=lambda: ui.run_javascript(f"window.open('{file_url}', '_blank')"),
                    ).props("flat dark dense").classes("text-gray-400 text-xs")

                async def _reingest() -> None:
                    """Re-run extraction+embedding for this document (fix bad ingests)."""
                    _id = doc.id
                    from pipelines.delete import delete_document
                    from pipelines.ingest import ingest_document
                    from services.clients import (
                        chroma as _chroma,
                        thumbnail_service as _thumb,
                        vision as _vision,
                    )
                    _note = ui.notification(
                        _("Re-ingesting document #{id} …").format(id=_id),
                        spinner=True, timeout=None, type="ongoing",
                    )
                    try:
                        await delete_document(_id, _chroma, sidecar_service, _thumb)
                        await ingest_document(
                            _id, paperless, _chroma, _vision, sidecar_service, _thumb
                        )
                        _note.dismiss()
                        ui.notify(_("#{id} re-ingested.").format(id=_id), type="positive")
                        dlg.close()
                    except Exception as exc:
                        _note.dismiss()
                        ui.notify(
                            _("Error during re-ingest: {err}").format(err=exc),
                            type="negative",
                        )

                ui.button(
                    icon="refresh",
                    on_click=_reingest,
                ).props("flat dark dense").classes("text-gray-400").tooltip(
                    _("Re-ingest (extraction + embedding)")
                )
                ui.button(icon="close", on_click=dlg.close).props("flat dark dense").classes("text-gray-400")

        # ── Body ──────────────────────────────────────────────────────────────
        render_document_body(result, file_url, related_refs=related_refs, related_docs=related_docs, navigate_fn=navigate_fn)
