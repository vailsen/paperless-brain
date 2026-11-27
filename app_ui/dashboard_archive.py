"""The dashboard's archive section (docs/ux-overhaul-plan.md, Phase 6;
figures from `services/archive_stats.py`).

Every part taps through to something: a month opens the browser filtered to
it, a health count opens the browser on what is missing, a duplicate opens the
document. Charts are one neutral hue — colour encodes state, not identity (UI
invariant).

Loaded after the page is on screen, with the user's own token; the duplicate
pairs are cached per user while the catalogue and the active index are
unchanged. A pair the user dismissed (pref `dup_dismissed`) stays away.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

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
.dash-link{display:block;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.86rem;
 color:var(--c-accent-text);text-decoration:underline;text-decoration-color:var(--c-accent-border);
 text-underline-offset:2px;cursor:pointer}
.dash-link:hover{text-decoration-color:currentColor}
.dash-health{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.dash-kpi{border:1px solid var(--c-border);border-radius:10px;padding:10px 12px;cursor:pointer;background:var(--c-surface)}
.dash-kpi .v{font-size:1.4rem;font-weight:700;color:var(--c-text);font-variant-numeric:tabular-nums}
.dash-kpi .l{font-size:.75rem;color:var(--c-text-muted)}
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
    """Duplicate pairs — cached while nothing changed."""
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

    out = {"dups": await asyncio.to_thread(ax.duplicates, docs, ids, mat)}
    _CACHE[user] = (time.time(), fp, out)
    return out


def render_archive(box, docs: list, open_doc: Callable[[int], Awaitable[None]], user: str) -> None:
    _ = get_translator()
    pal = _palette()
    st = {"field": "created", "type": ""}
    types = sorted({d.document_type for d in docs if d.document_type})

    with box:
        # ── Documents over time ─────────────────────────────────────────────
        with ui.element("div").classes("dash-card w-full"):
            # Wraps on a phone: title on its own line, the two choices below
            # (no-wrap pushed the type picker past the card's right edge).
            with ui.row().classes("w-full items-center gap-2").style("flex-wrap:wrap; row-gap:6px"):
                ui.label(_("Documents over time")).classes("dash-sec").style("margin:0; flex:1 1 160px")
                with ui.row().classes("items-center gap-2 no-wrap").style("min-width:0; max-width:100%"):
                    ui.toggle({"created": _("Created"), "added": _("Added")}, value="created",
                              on_change=lambda e: (st.update(field=e.value), draw())).props(
                        "dense flat no-caps toggle-color=primary").style("font-size:.75rem; flex-shrink:0")
                    ui.select({"": _("All types")} | {t: t for t in types}, value="",
                              on_change=lambda e: (st.update(type=e.value or ""), draw())).props(
                        "dense outlined options-dense").style("width:150px; min-width:0; flex-shrink:1")
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

        # ── Duplicates (vectors; filled when computed, absent when none) ────
        vec_box = ui.column().classes("w-full gap-5")

    async def fill() -> None:
        try:
            res = await compute(user, docs)
        except Exception:  # noqa: BLE001 — no index, no model: nothing to show, never break the page
            return
        _render_dups(vec_box, docs, res.get("dups") or [], open_doc)

    return fill


def _dup_key(a: int, b: int) -> str:
    return f"{min(a, b)}-{max(a, b)}"


def _render_dups(box, docs: list, dups: list, open_doc) -> None:
    """Possible duplicates: two linked titles per pair, and a dismiss button
    that keeps the pair away for good (pref `dup_dismissed`)."""
    from services.user_prefs import get_pref, set_pref

    _ = get_translator()
    by_id = {d.id: d for d in docs}

    def title(i: int) -> str:
        d = by_id.get(i)
        return f"{d.title} (#{i})" if d else f"#{i}"

    def dismiss(key: str) -> None:
        set_pref("dup_dismissed", sorted(set(get_pref("dup_dismissed") or []) | {key}))
        draw()

    def draw() -> None:
        box.clear()
        gone = set(get_pref("dup_dismissed") or [])
        pairs = [(a, b) for a, b, _s in dups if _dup_key(a, b) not in gone]
        if not pairs:
            return
        with box, ui.element("div").classes("dash-card w-full"):
            ui.label(_("Possible duplicates")).classes("dash-sec")
            ui.label(_("Nearly the same text and page count. Tap a title to open the document.")).classes(
                "text-xs").style("color:var(--c-text-muted); margin:-6px 0 4px")
            for a, b in pairs[:8]:
                key = _dup_key(a, b)
                with ui.element("div").classes("dash-row").style("align-items:center"):
                    with ui.column().classes("gap-0 flex-1 min-w-0"):
                        for i in (a, b):
                            link = ui.label(title(i)).classes("dash-link w-full").props('role="link"')
                            link.on("click", lambda _e, i=i: open_doc(i))
                    ui.button(icon="close", on_click=lambda k=key: dismiss(k)).props(
                        "flat dense round size=sm").classes("card-action-btn").tooltip(
                        _("Not duplicates — hide this pair"))

    draw()
