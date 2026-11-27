"""The dashboard's archive section and its "expected but missing" card
(docs/ux-overhaul-plan.md, Phase 6; figures from `services/archive_stats.py`).

Every chart answers a question and taps through to something: a month opens
the browser filtered to it, a topic lists its documents, a health count opens
the browser on what is missing. Charts are one neutral hue — colour encodes
state, not identity (UI invariant); the warn colour is for the overdue only.

Loaded after the page is on screen (`load`), with the user's own token; the
vector part is cached per user while the catalogue and the active index are
unchanged.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import date

from nicegui import ui

from i18n import get_translator
from services import archive_stats as ax

_CACHE: dict[str, tuple[float, tuple, dict]] = {}  # user → (time, fingerprint, result)
CACHE_TTL_S = 6 * 3600

_CSS = """<style>
.dash-sec{font-size:.78rem;font-weight:600;color:var(--c-text-muted);margin-bottom:10px}
.dash-row{display:flex;align-items:baseline;gap:10px;padding:6px 0;border-bottom:1px solid var(--c-border);min-width:0}
.dash-row:last-child{border-bottom:0}
.dash-row .t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--c-text);font-size:.86rem}
.dash-row .m{color:var(--c-text-muted);font-size:.75rem;white-space:nowrap;font-variant-numeric:tabular-nums}
.dash-row .w{color:var(--c-warn);font-size:.75rem;white-space:nowrap}
.dash-row.click{cursor:pointer}.dash-row.click:hover .t{text-decoration:underline}
.dash-dots{display:flex;gap:3px;align-items:center}
.dash-dots span{width:6px;height:6px;border-radius:50%;background:var(--c-text-muted)}
.dash-health{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.dash-kpi{border:1px solid var(--c-border);border-radius:10px;padding:10px 12px;cursor:pointer;background:var(--c-surface)}
.dash-kpi .v{font-size:1.4rem;font-weight:700;color:var(--c-text);font-variant-numeric:tabular-nums}
.dash-kpi .l{font-size:.75rem;color:var(--c-text-muted)}
.dash-system .q-expansion__content{padding:0}
</style>"""


def _palette() -> dict:
    from app_ui.theme import current_theme

    if current_theme() == "light":
        return {"bar": "#7d8696", "text": "#5b6270", "grid": "#d9dce2", "bg": "transparent", "hl": "#3563d9"}
    return {"bar": "#8c95a5", "text": "#a7aebb", "grid": "#2c2f35", "bg": "transparent", "hl": "#7b9cf0"}


async def _vectors(docs: list) -> tuple[list[int], object]:
    """Mean vectors of the user's documents from the active index."""
    from services.clients import chroma

    allowed = {d.id for d in docs}
    rows = await chroma.get(where={"paperless_id": {"$in": sorted(allowed) or [-1]}},
                            include=["embeddings", "metadatas"])
    return await asyncio.to_thread(ax.doc_vectors, rows, allowed)


async def compute(user: str, docs: list) -> dict:
    """Topics, duplicates and outliers — cached while nothing changed."""
    from services.embedding_registry import active_id

    try:
        model = active_id()
    except Exception:  # noqa: BLE001
        model = ""
    newest = max((str(d.modified) for d in docs), default="")
    fp = (model, len(docs), newest)
    hit = _CACHE.get(user)
    if hit and hit[1] == fp and time.time() - hit[0] < CACHE_TTL_S:
        return hit[2]
    ids, mat = await _vectors(docs)

    def _all():
        return {"topics": ax.topics(docs, ids, mat), "dups": ax.duplicates(docs, ids, mat),
                "outliers": ax.outliers(docs, ids, mat)}

    out = await asyncio.to_thread(_all)
    _CACHE[user] = (time.time(), fp, out)
    return out


def render_expected(box, docs: list, open_doc: Callable[[int], Awaitable[None]]) -> None:
    """The "Expected but missing" card."""
    from services.user_prefs import get_pref, set_pref

    _ = get_translator()
    dismissed = set(get_pref("expected_dismissed") or [])
    series = ax.expected_missing(docs, today=date.today(), dismissed=dismissed)
    names = {"monthly": _("monthly"), "quarterly": _("quarterly"), "half-yearly": _("every six months"),
             "yearly": _("yearly")}

    def dismiss(key: str) -> None:
        set_pref("expected_dismissed", sorted(set(get_pref("expected_dismissed") or []) | {key}))
        box.clear()
        with box:
            render_expected(box, docs, open_doc)

    with box:
        ui.label(_("Expected but missing")).classes("dash-sec")
        if not series:
            ui.label(_("Every regular series is up to date.")).classes("text-xs").style("color:var(--c-text-muted)")
            return
        for s in series[:6]:
            with ui.element("div").classes("dash-row"):
                with ui.column().classes("gap-0 flex-1 min-w-0"):
                    ui.label(f"{s.correspondent} · {s.document_type or _('(none)')}").classes("t w-full")
                    ui.label(_("{cadence}, last {last}, expected around {expected}").format(
                        cadence=names[s.cadence], last=s.last.strftime("%d.%m.%Y"),
                        expected=s.expected.strftime("%m/%Y"))).classes("m")
                    with ui.element("div").classes("dash-dots").tooltip(
                            ", ".join(d.strftime("%d.%m.%Y") for d in s.history)):
                        for _d in s.history:
                            ui.element("span")
                ui.button(icon="open_in_new", on_click=lambda i=s.last_id: open_doc(i)).props(
                    "flat dense round size=sm").classes("card-action-btn").tooltip(_("Open the last one"))
                ui.button(icon="do_not_disturb_on", on_click=lambda k=s.key: dismiss(k)).props(
                    "flat dense round size=sm").classes("card-action-btn").tooltip(_("Not coming any more"))


def render_archive(box, docs: list, open_doc: Callable[[int], Awaitable[None]], user: str) -> None:
    _ = get_translator()
    pal = _palette()
    st = {"field": "created", "type": ""}
    types = sorted({d.document_type for d in docs if d.document_type})

    with box:
        # ── Documents over time ─────────────────────────────────────────────
        with ui.element("div").classes("dash-card w-full"):
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                ui.label(_("Documents over time")).classes("dash-sec flex-1").style("margin:0")
                ui.toggle({"created": _("Created"), "added": _("Added")}, value="created",
                          on_change=lambda e: (st.update(field=e.value), draw())).props(
                    "dense flat no-caps toggle-color=primary").style("font-size:.75rem")
                ui.select({"": _("All types")} | {t: t for t in types}, value="",
                          on_change=lambda e: (st.update(type=e.value or ""), draw())).props(
                    "dense outlined options-dense").classes("w-40")
            chart = ui.echart({}).classes("w-full").style("height:220px")

            def draw() -> None:
                r = ax.over_time(docs, field_name=st["field"], months=24, doc_type=st["type"])
                chart.options.clear()
                chart.options.update({
                    "grid": {"left": 34, "right": 8, "top": 10, "bottom": 28},
                    "tooltip": {"trigger": "axis"},
                    "xAxis": {"type": "category", "data": r["months"],
                              "axisLabel": {"color": pal["text"], "fontSize": 10},
                              "axisLine": {"lineStyle": {"color": pal["grid"]}}},
                    "yAxis": {"type": "value", "minInterval": 1, "axisLabel": {"color": pal["text"], "fontSize": 10},
                              "splitLine": {"lineStyle": {"color": pal["grid"]}}},
                    "series": [{"type": "bar", "data": r["counts"], "itemStyle": {"color": pal["bar"]},
                                "emphasis": {"itemStyle": {"color": pal["hl"]}}, "barMaxWidth": 22}],
                })
                chart.update()

            def on_bar(e) -> None:
                month = e.name
                q = f"/browser?y={month}" + (f"&d={st['type']}" if st["type"] else "")
                if st["field"] == "added":
                    q = "/browser?s=added" + (f"&d={st['type']}" if st["type"] else "")
                ui.navigate.to(q)

            chart.on_point_click(on_bar)
            draw()
            ui.label(_("Tap a month to see its documents.")).classes("text-xs").style("color:var(--c-text-muted)")

        # ── Archive health ──────────────────────────────────────────────────
        h = ax.health(docs)
        with ui.element("div").classes("dash-card w-full"):
            ui.label(_("Keeping the archive tidy")).classes("dash-sec")
            with ui.element("div").classes("dash-health"):
                for key, label, href in (("no_correspondent", _("without correspondent"), "/browser?c=~&v=list"),
                                         ("no_type", _("without document type"), "/browser?d=~&v=list"),
                                         ("no_tags", _("without tags"), "/browser?t=~&v=list")):
                    el = ui.element("div").classes("dash-kpi")
                    el.on("click", lambda _e, u=href: ui.navigate.to(u))
                    with el:
                        ui.label(str(h[key])).classes("v")
                        ui.label(label).classes("l")

        # ── Topics, duplicates, outliers (vectors; filled when computed) ────
        vec_box = ui.column().classes("w-full gap-5")
        with vec_box, ui.element("div").classes("dash-card w-full"):
            ui.label(_("Topics in the archive")).classes("dash-sec")
            with ui.row().classes("items-center gap-2"):
                ui.spinner(size="sm")
                ui.label(_("Reading the index …")).classes("text-xs").style("color:var(--c-text-muted)")

    async def fill() -> None:
        try:
            res = await compute(user, docs)
        except Exception as exc:  # noqa: BLE001 — no index, no model: say so, never break the page
            vec_box.clear()
            with vec_box, ui.element("div").classes("dash-card w-full"):
                ui.label(_("Topics in the archive")).classes("dash-sec")
                ui.label(_("Not available: {err}").format(err=exc)).classes("text-xs").style(
                    "color:var(--c-text-muted)")
            return
        vec_box.clear()
        _render_vectors(vec_box, docs, res, open_doc, pal)

    return fill


def _render_vectors(box, docs: list, res: dict, open_doc, pal: dict) -> None:
    _ = get_translator()
    by_id = {d.id: d for d in docs}

    def title(i: int) -> str:
        d = by_id.get(i)
        return f"{d.title} (#{i})" if d else f"#{i}"

    def show_topic(t) -> None:
        with ui.dialog() as dlg, ui.card().classes("gap-1").style(
                "background:var(--c-surface); width:560px; max-width:94vw; max-height:80vh;"):
            ui.label(t.label).classes("text-base font-semibold").style("color:var(--c-text)")
            ui.label(_("{n} documents").format(n=len(t.ids))).classes("text-xs").style("color:var(--c-text-muted)")
            with ui.column().classes("w-full gap-0").style("overflow-y:auto"):
                for i in sorted(t.ids, key=lambda i: str(getattr(by_id.get(i), "created", "")), reverse=True):
                    row = ui.element("div").classes("dash-row click")
                    row.on("click", lambda _e, i=i: (dlg.close(), open_doc(i)))
                    with row:
                        ui.label(title(i)).classes("t")
        dlg.open()

    with box:
        tops = res.get("topics") or []
        with ui.element("div").classes("dash-card w-full"):
            ui.label(_("Topics in the archive")).classes("dash-sec")
            if not tops:
                ui.label(_("Too few indexed documents to group.")).classes("text-xs").style("color:var(--c-text-muted)")
            else:
                chart = ui.echart({
                    "tooltip": {"formatter": "{b}: {c}"},
                    "series": [{
                        "type": "treemap", "roam": False, "nodeClick": False, "breadcrumb": {"show": False},
                        "width": "100%", "height": "100%", "top": 0, "left": 0, "right": 0, "bottom": 0,
                        "itemStyle": {"color": pal["bar"], "borderColor": pal["grid"], "borderWidth": 2, "gapWidth": 2},
                        "label": {"show": True, "color": "#fff", "fontSize": 11, "overflow": "truncate"},
                        "data": [{"name": t.label, "value": len(t.ids)} for t in tops],
                    }],
                }).classes("w-full").style("height:260px")
                chart.on_point_click(lambda e: show_topic(tops[e.data_index]) if e.data_index is not None
                                     and e.data_index < len(tops) else None)
                ui.label(_("Grouped by meaning from the index; named after their most common correspondents and "
                           "types. Tap one to list its documents.")).classes("text-xs").style(
                    "color:var(--c-text-muted)")

        dups = res.get("dups") or []
        outs = res.get("outliers") or []
        if dups or outs:
            with ui.row().classes("w-full gap-5 no-wrap").style("flex-wrap:wrap"):
                if dups:
                    with ui.element("div").classes("dash-card flex-1").style("min-width:300px"):
                        ui.label(_("Possible duplicates")).classes("dash-sec")
                        for a, b, _s in dups[:8]:
                            with ui.element("div").classes("dash-row"):
                                with ui.column().classes("gap-0 flex-1 min-w-0"):
                                    for i in (a, b):
                                        lab = ui.label(title(i)).classes("t w-full").style("cursor:pointer")
                                        lab.on("click", lambda _e, i=i: open_doc(i))
                if outs:
                    with ui.element("div").classes("dash-card flex-1").style("min-width:300px"):
                        ui.label(_("Possibly mis-filed")).classes("dash-sec")
                        for o in outs[:8]:
                            row = ui.element("div").classes("dash-row click")
                            row.on("click", lambda _e, i=o.doc_id: open_doc(i))
                            with row:
                                ui.label(title(o.doc_id)).classes("t")
                                what = _("type") if o.field == "document_type" else _("correspondent")
                                ui.label(_("{what} “{has}”, its neighbours say “{say}”").format(
                                    what=what, has=o.has, say=o.neighbours_say)).classes("m")
