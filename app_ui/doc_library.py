"""The documents an expert can see, as a compact text tree.

Shown as the "Library" tab of the chat's results drawer and in the findings
page. Not cards with previews: a power user wants to scan two hundred titles,
grouped the way they think about them — by correspondent, tag or document
type — and open one.

The list comes from the expert's scoped client (`services/expert_scope.py`),
so it is exactly what the expert's tools can reach: what the user sees here is
what the expert sees, nothing more.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable

from nicegui import ui

from i18n import get_translator

log = logging.getLogger("doc_library")

GROUPS = ("correspondent", "tag", "document_type")
#: A tree of thousands of rows stays usable; beyond this the user filters.
MAX_DOCS = 5000

_CSS = """<style>
.pb-lib .q-tree__node-header { padding: 2px 4px; border-radius: 6px; }
.pb-lib .q-tree__node-header-content { min-width: 0; }
.pb-lib-doc { display: flex; gap: 6px; min-width: 0; width: 100%; font-size: 0.78rem; color: var(--c-text-2); }
.pb-lib-doc .t { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pb-lib-doc .d { flex-shrink: 0; color: var(--c-text-muted); font-variant-numeric: tabular-nums; }
.pb-lib-grp { font-size: 0.8rem; font-weight: 600; color: var(--c-text); display: flex; gap: 6px; min-width: 0; }
.pb-lib-grp .n { color: var(--c-text-muted); font-weight: 400; }
.pb-lib-grp .t { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>"""

_NODE = """
<div v-if="props.node.doc" class="pb-lib-doc">
  <span class="t">{{ props.node.label }}</span><span class="d">{{ props.node.date }}</span>
</div>
<div v-else class="pb-lib-grp"><span class="t">{{ props.node.label }}</span><span class="n">{{ props.node.count }}</span></div>
"""


def build_nodes(docs: list, group: str, none_label: str) -> list[dict]:
    """Tree nodes: one group per value, documents newest first inside it. A
    document with several tags is listed under each of them."""
    buckets: dict[str, list] = defaultdict(list)
    for d in docs:
        if group == "tag":
            keys = list(d.tags or []) or [none_label]
        elif group == "document_type":
            keys = [d.document_type or none_label]
        else:
            keys = [d.correspondent or none_label]
        for k in keys:
            buckets[k].append(d)
    nodes = []
    for key in sorted(buckets, key=lambda k: (k == none_label, k.casefold())):
        ds = sorted(buckets[key], key=lambda d: d.created, reverse=True)
        nodes.append({
            "id": f"g:{key}",
            "label": key,
            "count": len(ds),
            "children": [
                {"id": f"d:{d.id}:{key}", "label": f"{d.title} (#{d.id})", "doc": d.id,
                 "date": d.created.date().isoformat() if getattr(d, "created", None) else ""}
                for d in ds
            ],
        })
    return nodes


class DocLibrary:
    """One library view. `get_client()` returns the (scoped) Paperless client
    of the moment — called on every load, so an expert switch is picked up."""

    def __init__(self, get_client: Callable[[], object], open_document: Callable[[int], Awaitable[None]],
                 *, subtitle: Callable[[], str] | None = None):
        self._get_client = get_client
        self._open = open_document
        self._subtitle = subtitle
        self._docs: list | None = None
        self._group = "correspondent"
        self._tree = None
        self._count = None
        self._sub = None
        self._loading = False

    def render(self) -> None:
        _ = get_translator()
        ui.add_head_html(_CSS)
        with ui.column().classes("w-full h-full gap-1 pb-lib").style("min-width:0; padding:8px;"):
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                ui.toggle(
                    {"correspondent": _("Correspondent"), "tag": _("Tag"), "document_type": _("Type")},
                    value=self._group, on_change=lambda e: self._set_group(e.value),
                ).props("dense no-caps unelevated toggle-color=primary").style("font-size:0.75rem").classes("flex-shrink-0")
                ui.element("div").style("flex:1;")
                ui.button(icon="refresh", on_click=self.reload).props("flat dense round size=sm").classes(
                    "card-action-btn").tooltip(_("Reload"))
            search = ui.input(placeholder=_("Filter titles …")).props("dense outlined clearable").classes("w-full")
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                self._count = ui.label("").classes("text-xs").style(
                    "color:var(--c-text-muted); white-space:nowrap; flex-shrink:0;")
                self._sub = ui.label("").classes("text-xs w-full").style(
                    "color:var(--c-text-muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;")
            with ui.scroll_area().classes("w-full").style("flex:1; min-height:0;"):
                self._tree = ui.tree([], label_key="label", node_key="id", on_select=self._on_select).classes("w-full")
                self._tree.add_slot("default-header", _NODE)
                search.bind_value_to(self._tree, "filter")

    def _set_group(self, group: str) -> None:
        self._group = group if group in GROUPS else "correspondent"
        self._fill()

    async def _on_select(self, e) -> None:
        node_id = e.value or ""
        if self._tree is not None:
            self._tree._props["selected"] = None  # a second click opens it again
            self._tree.update()
        if node_id.startswith("d:"):
            try:
                await self._open(int(node_id.split(":")[1]))
            except Exception:
                log.warning("could not open document from the library", exc_info=True)

    def _fill(self) -> None:
        if self._tree is None:
            return
        _ = get_translator()
        docs = self._docs or []
        self._tree._props["nodes"] = build_nodes(docs[:MAX_DOCS], self._group, _("(none)"))
        self._tree.update()
        if self._count is not None:
            self._count.set_text(
                _("Loading …") if self._docs is None else _("{n} documents").format(n=len(docs)))  # TODO i18n-plural
        if self._sub is not None and self._subtitle:
            self._sub.set_text(self._subtitle())

    async def reload(self) -> None:
        if self._loading:
            return
        self._loading = True
        self._docs = None
        self._fill()
        try:
            from pipelines.search import _drop_no_ingest

            client = self._get_client()
            self._docs = _drop_no_ingest(await client.list_documents())
        except Exception:
            log.warning("library load failed", exc_info=True)
            self._docs = []
            ui.notify(get_translator()("Could not load the document list."), type="warning")
        finally:
            self._loading = False
        self._fill()

    def invalidate(self) -> None:
        """The expert changed: the next show reloads."""
        self._docs = None

    @property
    def loaded(self) -> bool:
        return self._docs is not None
