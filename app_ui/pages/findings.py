# app_ui/pages/findings.py
"""Findings — the proactive system's notices, read from the user's own ledger.

Registered only with `PROACTIVE_ENABLED=true` (`proactive.app.register`).
Every lookup goes through `proactive.findings.get(user, …)`, which reads only
the signed-in user's vault: another user's finding id is simply not found.

Document links open the app's own document dialog. The note file keeps
Paperless links (they work in Obsidian too); they are rewritten for display.

Email action links land here (`/findings/{id}?action=…`). The action is not
applied on load — link scanners and prefetchers follow links — but offered as
one confirm button.
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote

from nicegui import app as ng_app
from nicegui import ui

from app_ui.layout import page_layout, require_auth
from i18n import N_, get_translator

from app_ui.findings_view import STATUS_ICONS, STATUS_LABELS

KIND_LABELS = {"deadline": N_("Deadline"), "insight": N_("Insight")}


_DOC_EVENT = "pb_finding_doc"
# One delegated listener: links in the rendered Markdown are plain anchors.
# Web sources and research PDFs open in a new tab — in the installed PWA a
# plain link would replace the app.
_DOC_CLICK_JS = (
    "<script>document.addEventListener('click',function(e){"
    "var a=e.target.closest&&e.target.closest('.finding-md a[href]');if(!a)return;"
    "var h=a.getAttribute('href');"
    "if(h.indexOf('#pbdoc-')===0){e.preventDefault();"
    "emitEvent('" + _DOC_EVENT + "',parseInt(h.slice(7),10));}"
    "else if(/^https?:/.test(h)||h.indexOf('/api/research/pdf')===0){"
    "e.preventDefault();window.open(h,'_blank','noopener');}"
    "});</script>"
)
# A bare URL (the "Sources" list) — not one already inside a Markdown link,
# an <autolink> or an attribute. Trailing punctuation is not part of it.
_BARE_URL = re.compile(r"(?<![(<\[\"'=])\bhttps?://[^\s<>()\[\]]+[^\s<>()\[\].,;:!?'\"]")


_BARE_REF = re.compile(r"\[#(\d+)((?: p\d+)?)\](?!\()")


def _in_app_links(body: str, user: str = "") -> str:
    """`…/documents/241/details` (Paperless) → `#pbdoc-241` (the dialog).
    With `user`, links to a research the user deleted (its run, its note)
    are shown as deleted instead of leading to "not found"."""
    from config.settings import settings

    from config.proactive import proactive_settings

    base = re.escape(settings.paperless_url.rstrip("/"))
    body = re.sub(base + r"/documents/(\d+)/details/?", r"#pbdoc-\1", body)
    # A bare `[#241]` (dossier state, model text) opens the document too.
    body = _BARE_REF.sub(r"[#\1\2](#pbdoc-\1)", body)
    # Links into the app itself (the deep dive's report) stay in the app.
    app = proactive_settings().proactive_app_base_url.rstrip("/")
    if app:
        body = re.sub(re.escape(app) + r"(?=/)", "", body)
    if user:
        body = _dead_run_links(body, user)
    body = _wikilinks(body, user)
    # markdown2 does not autolink bare URLs; Obsidian does.
    return _BARE_URL.sub(lambda m: f"<{m.group(0)}>", body)


_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


_RUN_LINK = re.compile(r"\[([^\]]+)\]\((?:https?://[^)\s]*)?/werkbank\?run=([\w-]+)\)")


def _deleted(label: str) -> str:
    _ = get_translator()
    return _("{label} (deleted)").format(label=label)


def _dead_run_links(body: str, user: str) -> str:
    """An older finding links its Werkbank run; a run the user deleted would
    open "Run not found"."""
    from werkbank.v2 import store

    def repl(m: re.Match) -> str:
        return m.group(0) if store.run_status(m.group(2), user) else _deleted(m.group(1))

    return _RUN_LINK.sub(repl, body)


def _wikilinks(body: str, user: str = "") -> str:
    """Obsidian links: one to a research note opens its PDF; any other is
    shown as its text — the page has nowhere to open a note."""
    from app_ui.research_routes import pdf_url
    from vault.research import research_rel, wikilink_target

    root = research_rel().strip("/") + "/"

    def repl(m: re.Match) -> str:
        target, label = m.group(1), m.group(2) or m.group(1).rsplit("/", 1)[-1]
        if target.startswith(root):
            if user and not _note_exists(user, wikilink_target(target)):
                return _deleted(label)
            return f"[{label}]({pdf_url(wikilink_target(target))})"
        return label

    return _WIKILINK.sub(repl, body)


def _note_exists(user: str, rel: str) -> bool:
    from vault.notes import resolve

    try:
        return resolve(user, rel).is_file()
    except Exception:  # noqa: BLE001 — outside the vault, unreadable: not there
        return False


async def _adopt_research_note(user: str, f) -> None:
    """A finding researched before reports went to the vault links its
    Werkbank run. Write the run's note now (while the run still exists) and
    point the finding at it, so deleting the run no longer loses the report."""
    from proactive import findings
    from vault.research import note_for_run, wikilink

    if f is None or f.research_note or not f.research_run or f.status != "researched":
        return
    note = await note_for_run(user, f.research_run)
    if not note:
        return
    link = wikilink(note)
    await findings.update(
        user, f.finding_id, lambda x: {"research_note": link} if not x.research_note else None,
        body=lambda x: findings.link_research_note(x.body, x.research_run or "", note),
    )


def _user() -> str:
    return str(ng_app.storage.user.get("paperless_user") or "")


def _meta_line(f, *, topic: bool = True, status: bool = True) -> str:
    """Kind · status · urgency · due · topic. The list leaves out what its
    heading already says, and the status, which its icon shows."""
    from proactive.notify import URGENCY_LABELS

    _ = get_translator()
    from proactive.states import display_status

    parts = [_(KIND_LABELS.get(f.kind, f.kind))]
    if status:
        parts.append(_(STATUS_LABELS[display_status(f.status)]))
    parts.append(_("Urgency: {urgency}").format(urgency=_(URGENCY_LABELS.get(f.urgency, f.urgency))))
    if f.deadline:
        parts.append(_("Due {date}").format(date=f.deadline))
    if topic and f.topic:
        parts.append(f.topic_display)
    return " · ".join(parts)


_LIST_CSS = """
.pb-finding-card{background:var(--c-surface);border:1px solid var(--c-border);
  border-left:3px solid var(--c-border);border-radius:8px;cursor:pointer}
.pb-finding-card:hover{border-color:var(--c-border-strong)}
.pb-finding-card.is-pressing{border-left-color:var(--c-warn)}
/* The user's topic colour: the whole outline; "pressing" keeps its left edge. */
.pb-finding-card.has-topic{border-color:var(--card-topic);
  background:color-mix(in srgb, var(--card-topic) 9%, var(--c-surface))}
.pb-finding-card.has-topic:hover{border-color:var(--card-topic)}
.pb-finding-card.has-topic.is-pressing{border-left-color:var(--c-warn)}
.pb-hint-icon{color:var(--c-text-muted)}
.pb-swatch{width:22px;height:22px;border-radius:50%;cursor:pointer;border:2px solid transparent}
.pb-swatch.is-on{border-color:var(--c-text);box-shadow:0 0 0 2px var(--c-surface) inset}
.pb-swatch-none{background:transparent;border:1px dashed var(--c-text-muted)}
.pb-finding-card.is-closed{opacity:.6}
.pb-finding-card .pb-status-icon{color:var(--c-text-2)}
.pb-finding-head{color:var(--c-text);border-bottom:1px solid var(--c-border)}
.pb-finding-sub{color:var(--c-text-2)}
.pb-finding-count{color:var(--c-text-muted);font-weight:400}
"""


def _status_icon(status: str, size: str = "1.25rem"):
    """The icon of the inbox status `status` stands for (a stored status like
    "surfaced" shows as "open")."""
    from proactive.states import display_status

    _ = get_translator()
    key = status if status in STATUS_LABELS else display_status(status)
    icon = ui.icon(STATUS_ICONS[key]).classes("pb-status-icon flex-shrink-0").style(f"font-size:{size}")
    icon.tooltip(_(STATUS_LABELS[key]))
    return icon


def _hints(f) -> None:
    """Small icons for what is not a status: the notification, the research."""
    from app_ui import findings_view as fv

    _ = get_translator()
    for hint in (fv.notice(f), fv.research(f)):
        if hint:
            ui.icon(hint[0]).classes("pb-hint-icon flex-shrink-0").style("font-size:.95rem").tooltip(_(hint[1]))


def _status_select(value: str, on_change) -> None:
    """The status filter, each status with its icon — "All" on top."""
    import json

    from app_ui import findings_view as fv

    _ = get_translator()
    opts = [("all", _("All statuses"), "select_all")]
    opts += [(k, _(fv.STATUS_LABELS[k]), fv.STATUS_ICONS[k]) for k in fv.STATUS_ORDER]
    if value not in [o[0] for o in opts]:
        value = "open"  # a stored status from before the inbox
    sel = ui.select({k: label for k, label, _i in opts}, value=value, label=_("Status"),
                    on_change=on_change).props("outlined dark dense options-dense").classes("min-w-0")
    # NiceGUI hands Quasar the options as {value: index, label}: the slot looks
    # each icon up by that index.
    icons = json.dumps([i for _k, _l, i in opts])
    sel.add_slot("option", (
        '<q-item v-bind="props.itemProps">'
        f"<q-item-section avatar style=\"min-width:28px\"><q-icon :name='{icons}[props.opt.value]' size=\"18px\" />"
        "</q-item-section><q-item-section><q-item-label>{{ props.opt.label }}</q-item-label></q-item-section>"
        "</q-item>"
    ))
    with sel.add_slot("prepend"):
        ui.icon(next(i for k, _l, i in opts if k == value)).style("font-size:18px")


def _overview(items: list, today, on_pick) -> None:
    """Per topic: open findings, pressing ones, the next deadline
    (`app_ui/findings_chart`). A tap opens the topic — its dossier, else the
    inbox filtered to it. The first `TOP_ROWS`, then "Show all"."""
    from app_ui import findings_chart, topic_colors

    _ = get_translator()
    data = findings_chart.topics(items, today)
    if not data:
        return
    colors = topic_colors.colors()
    shown = {"all": False}

    @ui.refreshable
    def rows() -> None:
        with ui.element("div").classes("pb-thead"):
            ui.element("span").classes("sp")
            ui.label(_("open")).classes("n")
            ui.label(_("due")).classes("due")
        rs = data if shown["all"] else data[:findings_chart.TOP_ROWS]
        with ui.element("div").classes("pb-topics"):
            for r in rs:
                none = r["topic"] == findings_chart.NO_TOPIC
                el = ui.element("div").classes("pb-trow" + (" is-none" if none else ""))
                el.on("click", lambda _e, k=r["topic"]: on_pick(k))
                with el:
                    dot = ui.element("span").classes("dot")
                    if r["topic"] in colors:
                        dot.style(f"background:{topic_colors.css_var(colors[r['topic']])}")
                    ui.label(r["label"] or _("No topic")).classes("name")
                    if r["act"]:
                        ui.label(_("{n} pressing").format(n=r["act"])).classes("act")
                    ui.label(str(r["open"])).classes("n")
                    ui.label(findings_chart.due_label(r["due"], today) or "—").classes("due")
        if len(data) > findings_chart.TOP_ROWS:
            ui.button(_("Show fewer") if shown["all"] else _("Show all {n} topics").format(n=len(data)),
                      icon="expand_less" if shown["all"] else "expand_more",
                      on_click=lambda: (shown.update(all=not shown["all"]), rows.refresh())).props(
                "flat dense no-caps").classes("text-xs self-start")

    with ui.element("div").classes("pbm-card"):
        ui.label(_("Topics")).classes("pbm-label")
        rows()


def _topic_colors_dialog(topics: dict[str, str], refresh) -> None:
    """Pick a colour per topic: the outline of its cards."""
    from app_ui import topic_colors

    _ = get_translator()
    with ui.dialog() as dlg, ui.card().classes("w-full").style("max-width:420px;background:var(--c-surface)"):
        ui.label(_("Topic colours")).classes("text-base font-semibold")
        ui.label(_("A colour outlines the cards of a topic. No colour is the default.")).classes(
            "text-xs").style("color:var(--c-text-muted)")
        if not topics:
            ui.label(_("No topics yet.")).classes("text-sm")

        @ui.refreshable
        def rows() -> None:
            chosen = topic_colors.colors()
            for key, label in sorted(topics.items(), key=lambda kv: kv[1].lower()):
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    ui.label(label).classes("text-sm flex-1 min-w-0 truncate")
                    with ui.row().classes("gap-1 no-wrap flex-shrink-0"):
                        def pick(k=key, s=None):
                            topic_colors.set_color(k, s)
                            rows.refresh()

                        ui.element("div").classes("pb-swatch pb-swatch-none" + (
                            " is-on" if key not in chosen else "")).on("click", lambda k=key: pick(k, None)) \
                            .tooltip(_("No colour"))
                        for s in topic_colors.SLOTS:
                            ui.element("div").classes("pb-swatch" + (" is-on" if chosen.get(key) == s else "")) \
                                .style(f"background:{topic_colors.css_var(s)}") \
                                .on("click", lambda k=key, s=s: pick(k, s))

        # A plain scrolling column: a q-scroll-area shrink-fits to its widest row.
        with ui.column().classes("w-full gap-2 min-w-0").style("max-height:60vh;overflow-y:auto"):
            rows()
        with ui.row().classes("w-full justify-end"):
            ui.button(_("Close"), on_click=lambda: (dlg.close(), refresh())).props("flat dense no-caps")
    dlg.open()


def _summary_item(icon: str, text: str, warn: bool = False) -> None:
    with ui.row().classes("items-center gap-1 no-wrap" + (" pb-warn-text" if warn else "")):
        ui.icon(icon).style("font-size:1rem")
        ui.label(text)


def _heading(g, *, top: bool) -> None:
    """A headline, nothing to click: the list is for scrolling."""
    _ = get_translator()
    label = (_(STATUS_LABELS.get(g.label, g.label)) if g.kind == "status"
             else (g.label or _("No topic")))
    if top:
        with ui.row().classes("pb-finding-head w-full items-center gap-2 no-wrap mt-3 pb-1"):
            if g.kind == "status":
                _status_icon(g.key, "1.1rem")
            ui.label(label).classes("text-base font-semibold min-w-0 truncate")
            ui.label(str(g.count)).classes("pb-finding-count text-sm flex-shrink-0")
    else:
        with ui.row().classes("pb-finding-sub w-full items-center gap-2 no-wrap mt-1 pl-1"):
            _status_icon(g.key, "1rem")
            ui.label(label).classes("text-sm font-medium min-w-0 truncate")
            ui.label(str(g.count)).classes("pb-finding-count text-xs flex-shrink-0")


def _card(f, today, *, topic: bool, status: bool, on_open=None, on_file=None) -> None:
    from app_ui import findings_view as fv

    from app_ui import topic_colors

    cls = "pb-finding-card w-full p-3 min-w-0"
    if fv.is_pressing(f, today):
        cls += " is-pressing"
    if f.status in fv.CLOSED_STATUSES:
        cls += " is-closed"
    slot = topic_colors.colors().get(f.topic or "")
    style = ""
    if slot:
        cls += " has-topic"
        style = f"--card-topic:{topic_colors.css_var(slot)}"
    with ui.row().classes(cls + " items-start gap-3 no-wrap").style(style).on(
            "click", lambda fid=f.finding_id: on_open(fid) if on_open else ui.navigate.to(f"/findings/{fid}")):
        _status_icon(f.status)
        with ui.column().classes("gap-0 flex-1 min-w-0"):
            ui.label(f.title).classes("text-sm font-semibold text-gray-100 w-full truncate")
            ui.label(_meta_line(f, topic=topic, status=status)).classes("text-xs text-gray-400 w-full")
        with ui.row().classes("items-center gap-1 no-wrap flex-shrink-0"):
            _hints(f)
            if on_file is not None:
                _ = get_translator()
                ui.button(icon="drive_file_move").props("flat dense round size=sm").classes(
                    "card-action-btn").on("click.stop", lambda _e, x=f: on_file(x)).tooltip(_("File in a dossier"))




# ── Findings workspace (docs/findings-workspace.md) ───────────────────────
#
# One page: a navigator (global search, inbox buckets, the dossier tree), a
# viewer (home, a list, a finding, a dossier binder) and a chat dock below it.
# The dock is the chat page itself, embedded (`/chat?embed=1&dossier=…`) — the
# same tools, citations, experts and "Save to dossier" button, no second chat
# implementation. On a phone the three zones are tabs.
#
# Deep links keep working: `/findings/{id}` (push and email links, with
# `?action=` confirmed by a button, never applied on load) opens the finding
# in the viewer; `/findings?d=<dossier id>&tab=insights` a binder.

_MC_CSS = """
.pbm-root{position:fixed;top:var(--q-header-height,52px);left:0;right:0;bottom:0;display:grid;
  grid-template-columns:290px minmax(0,1fr);background:var(--c-bg)}
.pbm-nav{border-right:1px solid var(--c-border);display:flex;flex-direction:column;min-width:0;min-height:0}
/* Desktop: the chat is a column on the right, always there, its width
   dragged at the handle. A phone stacks it under the item (media query). */
.pbm-center{display:flex;flex-direction:row;min-width:0;min-height:0}
.pbm-viewer{flex:1;min-width:0;min-height:0;overflow-y:auto;overflow-x:hidden}
.pbm-handle{width:6px;flex-shrink:0;cursor:col-resize;background:var(--c-border);opacity:.6}
.pbm-handle:hover{opacity:1}
.pbm-dock{width:var(--pbm-dock-w,40%);flex-shrink:0;display:flex;flex-direction:column;min-width:0;min-height:0;
  background:var(--c-surface)}
/* Only for "Talk about the selection" — no empty row otherwise. */
.pbm-dock-head{display:none;height:36px;flex-shrink:0;align-items:center;gap:8px;padding:0 10px;min-width:0;
  border-bottom:1px solid var(--c-border)}
.pbm-dock-head.has-retarget{display:flex}
.pbm-dragging .pbm-dock-frame iframe{pointer-events:none}
.pbm-dragging{cursor:col-resize;user-select:none}
.pbm-dock-frame{flex:1;min-height:0;position:relative}
.pbm-dock-frame iframe{position:absolute;inset:0;width:100%;height:100%;border:0;background:var(--c-bg)}
.pbm-scroll{flex:1;min-height:0;overflow-y:auto;overflow-x:hidden}
.pbm-bucket{display:flex;align-items:center;gap:10px;padding:7px 12px;margin:1px 6px;border-radius:8px;cursor:pointer;
  color:var(--c-text-2);font-size:.85rem;min-width:0}
.pbm-bucket:hover{background:var(--c-surface-2)}
.pbm-bucket.is-active{background:var(--c-accent-soft);color:var(--c-text)}
.pbm-bucket .n{margin-left:auto;color:var(--c-text-muted);font-size:.75rem;font-variant-numeric:tabular-nums}
.pbm-bucket.warn .n{color:var(--c-warn)}
.pbm-shelf-sep{height:1px;background:var(--c-border);margin:8px 6px 4px}
.pbm-shelf{display:flex;align-items:center;gap:6px;padding:4px 6px;margin:1px 6px;border-radius:6px;cursor:pointer;
  font-size:.84rem;color:var(--c-text-2);min-width:0}
.pbm-shelf:hover{background:var(--c-surface-2)}
.pbm-shelf.is-active{background:var(--c-accent-soft);color:var(--c-text)}
.pbm-shelf .n{margin-left:auto;color:var(--c-text-muted);font-size:.72rem}
.pbm-sec{font-size:.7rem;font-weight:600;color:var(--c-text-muted);padding:12px 14px 4px;display:flex;align-items:center;gap:4px}
.pbm-tree .q-tree__node-header{padding:3px 6px;border-radius:6px}
.pbm-tree .q-tree__node--selected .q-tree__node-header{background:var(--c-accent-soft)}
.pbm-tnode{display:flex;align-items:center;gap:6px;min-width:0;width:100%;font-size:.84rem;color:var(--c-text-2)}
.pbm-tnode .t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pbm-tnode .n{color:var(--c-text-muted);font-size:.72rem}
.pbm-tnode .bar{width:3px;height:14px;border-radius:2px;flex-shrink:0}
/* Centred between the navigator and the chat column, wherever the handle is. */
.pbm-panel{padding:16px 18px;max-width:960px;width:100%;box-sizing:border-box;margin-left:auto;margin-right:auto}
.pbm-viewer > *{max-width:960px;margin-left:auto;margin-right:auto}
.pbm-card{border:1px solid var(--c-border);border-radius:10px;padding:12px 14px;background:var(--c-surface);
  width:100%;box-sizing:border-box;min-width:0}
.pbm-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;width:100%}
.pbm-kpi{border:1px solid var(--c-border);border-radius:10px;padding:10px 12px;background:var(--c-surface);cursor:pointer}
.pbm-kpi .v{font-size:1.5rem;font-weight:700;color:var(--c-text);font-variant-numeric:tabular-nums}
.pbm-kpi .l{font-size:.75rem;color:var(--c-text-muted)}
.pbm-kpi.warn .v{color:var(--c-warn)}
.pbm-tabs{display:flex;gap:2px;overflow-x:auto;scrollbar-width:none;border-bottom:1px solid var(--c-border);padding:0 10px}
.pbm-tabs::-webkit-scrollbar{display:none}
.pbm-tab{flex-shrink:0;padding:10px 12px;font-size:.82rem;color:var(--c-text-muted);cursor:pointer;
  border-bottom:2px solid transparent;white-space:nowrap}
.pbm-tab.is-active{color:var(--c-text);border-bottom-color:var(--c-accent)}
.pbm-label{font-size:.75rem;font-weight:600;color:var(--c-text-2)}
.pbm-hint{font-size:.75rem;color:var(--c-text-muted)}
.pbm-ins{border-left:2px solid var(--c-border);padding:2px 0 14px 12px;margin-left:4px;min-width:0}
.pbm-ins h4{font-size:.92rem;font-weight:600;color:var(--c-text);margin:0;line-height:1.35}
.pbm-fold{max-height:13.5em;overflow:hidden;-webkit-mask-image:linear-gradient(#000 70%,transparent);
  mask-image:linear-gradient(#000 70%,transparent)}
.pbm-src{font-size:.72rem;color:var(--c-text-muted);overflow-wrap:anywhere}
.pbm-evid{display:inline-block;font-size:.68rem;padding:1px 7px;border-radius:999px;border:1px solid var(--c-border);
  color:var(--c-text-2);margin-right:4px}
.pbm-due{display:flex;gap:10px;align-items:baseline;padding:6px 0;border-bottom:1px solid var(--c-border);cursor:pointer;min-width:0}
.pbm-due:last-child{border-bottom:0}
.pbm-q{display:flex;align-items:flex-start;gap:4px;min-width:0;width:100%;padding:2px 0}
.pbm-q .t{flex:1;min-width:0;font-size:.875rem;color:var(--c-text-2);overflow-wrap:anywhere;padding-top:5px}
.pbm-q.is-done .t{color:var(--c-text-muted);text-decoration:line-through}
.pbm-q-add{margin-top:4px;border-top:1px dashed var(--c-border)}
.pbm-due .d{font-variant-numeric:tabular-nums;color:var(--c-text-2);font-size:.8rem;flex-shrink:0;width:6.5rem}
.pbm-due .t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.85rem;color:var(--c-text)}
.pbm-due.warn .d{color:var(--c-warn)}
.pbm-doc{display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:1px solid var(--c-border);cursor:pointer;
  min-width:0;box-sizing:border-box}
.pbm-doc:last-child{border-bottom:0}
.pbm-doc-main{flex:1;min-width:0;display:flex;flex-direction:column;gap:3px}
.pbm-doc .t{font-size:.86rem;color:var(--c-text);overflow-wrap:anywhere}
.pbm-doc .m{display:flex;align-items:center;gap:6px;flex-wrap:wrap;font-size:.75rem;color:var(--c-text-muted);
  font-variant-numeric:tabular-nums}
.pbm-mbar{display:none}
/* Maximized dock: the chat takes the viewer's place; the navigator (desktop)
   and the bottom switch (phone) stay, so the way back is one tap. */
.pbm-root.dock-max .pbm-viewer,.pbm-root.dock-max .pbm-handle{display:none!important}
.pbm-root.dock-max .pbm-dock{flex:1;width:auto!important;height:auto!important}
.pbm-kbd{font-size:.65rem;border:1px solid var(--c-border);border-radius:4px;padding:0 4px;color:var(--c-text-muted)}
.pbm-nsearch{padding:4px 6px}
.pbm-nres{display:flex;align-items:center;gap:8px;padding:6px 10px;margin:1px 0;border-radius:8px;cursor:pointer;
  min-width:0;font-size:.84rem;color:var(--c-text-2)}
.pbm-nres:hover{background:var(--c-surface-2)}
.pbm-nres .t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--c-text)}
.pbm-nres .s{font-size:.72rem;color:var(--c-text-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pbm-pending{display:flex;align-items:center;gap:8px;flex-wrap:wrap;font-size:.78rem;color:var(--c-text-2);
  border:1px dashed var(--c-border-strong);border-radius:8px;padding:6px 10px}
@media (max-width:1023px){
  .pbm-root{grid-template-columns:minmax(0,1fr);bottom:calc(48px + env(safe-area-inset-bottom))}
  .pbm-center{flex-direction:column}
  .pbm-dock{width:auto;border-left:0}
  .pbm-handle{width:auto;cursor:row-resize}
  .pbm-root .pbm-nav,.pbm-root .pbm-viewer,.pbm-root .pbm-dock,.pbm-root .pbm-handle{display:none}
  .pbm-root.z-nav .pbm-nav{display:flex}
  .pbm-root.z-viewer .pbm-viewer{display:block}
  /* Chat keeps the item in sight: the viewer stays on top, the chat below. */
  .pbm-root.z-chat .pbm-viewer{display:block}
  .pbm-root.z-chat .pbm-handle{display:block;height:16px;background:var(--c-surface);opacity:1;touch-action:none;
    border-top:1px solid var(--c-border);position:relative}
  .pbm-root.z-chat .pbm-handle::after{content:'';position:absolute;left:50%;top:6px;width:36px;height:4px;
    margin-left:-18px;border-radius:2px;background:var(--c-border-strong)}
  .pbm-root.z-chat .pbm-dock{display:flex;height:var(--pbm-dock-m,62%)!important;border-top:0}
  .pbm-root.z-chat .pbm-dock .pbm-dock-frame{display:block}
  .pbm-root.z-chat.dock-max .pbm-dock{height:100%!important}
  .pbm-root.z-chat .pbm-dock-head.has-retarget{height:30px}
  .pbm-mbar{display:flex;position:fixed;left:0;right:0;bottom:0;align-items:center;
    padding:6px 12px calc(6px + env(safe-area-inset-bottom));border-top:1px solid var(--c-border);background:var(--c-surface)}
  .pbm-seg{display:flex;flex:1;gap:2px;padding:3px;border-radius:999px;background:var(--c-surface-2);
    border:1px solid var(--c-border)}
  .pbm-seg button.pbm-seg-btn{flex:1;min-height:30px;border-radius:999px;font-size:.78rem;color:var(--c-text-muted);
    padding:0 6px}
  .pbm-seg button.pbm-seg-btn .q-icon{font-size:17px;margin-right:5px}
  .pbm-seg button.pbm-seg-btn.is-on{background:var(--c-surface);color:var(--c-text);
    box-shadow:0 1px 2px rgba(0,0,0,.18)}
  .pbm-panel{padding:12px}
  .pbm-kbd{display:none}
}
"""

_MC_JS = """<script>
(function(){
  // Drag the dock's edge; the height is remembered on this device.
  try{var w=localStorage.getItem('pbm-dock-w');if(w)document.documentElement.style.setProperty('--pbm-dock-w',w);
    var m=localStorage.getItem('pbm-dock-m');if(m)document.documentElement.style.setProperty('--pbm-dock-m',m);}catch(e){}
  document.addEventListener('pointerdown',function(e){
    var hd=e.target.closest&&e.target.closest('.pbm-handle');if(!hd)return;
    // The chat frame would swallow the pointer while dragging over it —
    // the release never reached this page and the drag stuck.
    e.preventDefault();var center=hd.parentElement;document.documentElement.classList.add('pbm-dragging');
    // Desktop: the chat column's width; a phone: the chat split's height.
    var wide=window.innerWidth>=1024, key=wide?'--pbm-dock-w':'--pbm-dock-m';
    function mv(ev){var r=center.getBoundingClientRect();var px=wide
        ? Math.max(320,Math.min(r.width-360,r.right-ev.clientX))
        : Math.max(120,Math.min(r.height-90,r.bottom-ev.clientY));
      document.documentElement.style.setProperty(key,px+'px');}
    function up(){document.documentElement.classList.remove('pbm-dragging');
      document.removeEventListener('pointermove',mv);document.removeEventListener('pointerup',up);
      try{localStorage.setItem('pbm'+key.slice(5),getComputedStyle(document.documentElement).getPropertyValue(key));}catch(e){}}
    document.addEventListener('pointermove',mv);document.addEventListener('pointerup',up);
  });
  // Ctrl/Cmd K focuses the search.
  document.addEventListener('keydown',function(e){
    if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){var i=document.querySelector('.pbm-search input');
      if(i){e.preventDefault();i.focus();i.select();}}
  });
  // The dock maximized or not: tell the embedded chat, which shows its full
  // controls only when maximized — also after the frame (re)loads.
  window.pbmDockMode=function(){var f=document.querySelector('.pbm-dock-frame iframe');
    var full=!!document.querySelector('.pbm-root.dock-max');
    if(f&&f.contentWindow){f.contentWindow.postMessage({type:'pb-dock-mode',full:full},window.location.origin);}};
  document.addEventListener('load',function(e){
    if(e.target&&e.target.tagName==='IFRAME'&&e.target.closest('.pbm-dock-frame'))window.pbmDockMode();},true);
  // "Ask the chat" on an open question: into the dock's input, once the
  // frame is there (it may be loading the dossier's conversation).
  window.pbmPrefill=function(text){var tries=0;(function go(){
    var f=document.querySelector('.pbm-dock-frame iframe');
    var ta=f&&f.contentDocument&&f.contentDocument.querySelector('.chat-main-input textarea');
    if(!ta){if(++tries<60)setTimeout(go,250);return;}
    ta.value=text;ta.dispatchEvent(new Event('input',{bubbles:true}));ta.focus();})();};
  // The embedded chat's maximize / back buttons.
  window.addEventListener('message',function(e){
    if(e.origin!==window.location.origin||!e.data||e.data.type!=='pb-dock-max')return;
    emitEvent('pb_dock_max',!!e.data.on);
  });
  // The embedded chat saved an insight: re-read the dossier.
  window.addEventListener('message',function(e){
    if(e.origin!==window.location.origin||!e.data||e.data.type!=='pb-insight-saved')return;
    emitEvent('pb_insight_saved',e.data.topic||'');
  });
})();
</script>"""

BUCKETS = ("open", "pressing", "relevant", "unfiled", "done", "dismissed")


def _bucket_items(items: list, bucket: str, today, dossier_topics: set[str]) -> list:
    from app_ui import findings_view as fv
    from proactive.states import display_status

    if bucket == "pressing":
        return [f for f in items if f.status in fv.OPEN_STATUSES and fv.is_pressing(f, today)]
    if bucket == "unfiled":
        return [f for f in items if display_status(f.status) == "open" and f.topic not in dossier_topics]
    return [f for f in items if display_status(f.status) == bucket]


def search_ledger(query: str, items: list, dossiers: list, titles: dict[int, str] | None = None) -> dict:
    """Lexical search over the ledger (titles, topics, bodies, insights, and
    the titles of the documents findings and dossiers name) — the ledger is
    small, and held or dismissed findings, which the semantic index never
    holds, must be findable too. Ranked title > topic > insight/document > body."""
    import unicodedata

    titles = titles or {}

    def fold(s: str) -> str:
        s = unicodedata.normalize("NFKD", (s or "").casefold())
        return "".join(c for c in s if not unicodedata.combining(c))

    terms = [t for t in fold(query).split() if t]
    if not terms:
        return {"dossiers": [], "findings": [], "insights": [], "documents": []}

    def score(fields: list[tuple[str, int]]) -> int:
        total = 0
        for t in terms:
            best = max((w for text, w in fields if t in text), default=0)
            if not best:
                return 0
            total += best
        return total

    def doc_text(ids) -> str:
        return fold(" ".join(titles.get(int(i), "") for i in ids))

    out_d, out_f, out_i, out_doc = [], [], [], []
    for d in dossiers:
        ids = set(d.doc_ids) | set(getattr(d, "added_docs", []) or [])
        s = score([(fold(d.display), 5), (fold(d.topic), 4), (doc_text(ids), 2), (fold(d.current_state), 1),
                   (fold(" ".join(d.open_questions)), 1)])
        if s:
            out_d.append((s, d))
        for ins in d.insights:
            si = score([(fold(ins.title), 3), (fold(ins.text), 1)])
            if si:
                out_i.append((si, d, ins))
    for f in items:
        s = score([(fold(f.title), 5), (fold(f.topic_display), 4), (doc_text(f.doc_ids), 2), (fold(f.body), 1)])
        if s:
            out_f.append((s, f))
    for i, t in titles.items():
        sd = score([(fold(t), 3), (fold(f"#{i}"), 3)])
        if sd:
            out_doc.append((sd, i, t))
    return {
        "dossiers": [d for _s, d in sorted(out_d, key=lambda x: -x[0])][:20],
        "findings": [f for _s, f in sorted(out_f, key=lambda x: (-x[0], x[1].created or ""))][:40],
        "insights": [(d, i) for _s, d, i in sorted(out_i, key=lambda x: -x[0])][:20],
        "documents": [(i, t) for _s, i, t in sorted(out_doc, key=lambda x: (-x[0], x[2].casefold()))][:20],
    }


def dossier_tree(dossiers: list, folders: list[str], open_counts: dict[str, int]) -> list[dict]:
    """Nodes for the navigator: folders (from disk) → dossiers."""
    root: list[dict] = []
    index: dict[str, dict] = {}

    def folder_node(path: str) -> list[dict]:
        if not path:
            return root
        if path in index:
            return index[path]["children"]
        parent, _sep, name = path.rpartition("/")
        node = {"id": f"f:{path}", "label": name, "folder": True, "children": []}
        index[path] = node
        folder_node(parent).append(node)
        return node["children"]

    for p in folders:
        folder_node(p)
    for d in sorted(dossiers, key=lambda d: d.display.casefold()):
        folder_node(d.folder).append({
            "id": f"d:{d.pbrain_id}", "label": d.display, "topic": d.topic,
            "count": open_counts.get(d.topic, 0), "insights": len(d.insights),
        })

    def order(nodes: list[dict]) -> list[dict]:
        nodes.sort(key=lambda n: (not n.get("folder"), n["label"].casefold()))
        for n in nodes:
            if n.get("children"):
                order(n["children"])
        return nodes

    return order(root)


_TREE_NODE = """
<div class="pbm-tnode">
  <q-icon :name="props.node.folder ? 'folder' : 'folder_open'" size="16px" />
  <span v-if="props.node.color" class="bar" :style="{background: props.node.color}"></span>
  <span class="t">{{ props.node.label }}</span>
  <span v-if="props.node.count" class="n">{{ props.node.count }}</span>
</div>
"""


@ui.page("/findings")
async def findings_page(d: str = "", f: str = "", tab: str = "", view: str = "") -> None:
    await _workspace(dossier=d, finding=f, tab=tab, view=view, action="")


@ui.page("/findings/{finding_id}")
async def finding_detail_page(finding_id: str, action: str = "") -> None:
    await _workspace(dossier="", finding=finding_id, tab="", view="", action=action)


async def _workspace(*, dossier: str, finding: str, tab: str, view: str, action: str) -> None:
    if not require_auth():
        return
    page_layout()
    _ = get_translator()
    from datetime import date

    from app_ui import findings_chart
    from app_ui import findings_view as fv
    from app_ui import topic_colors
    from app_ui.document_dialog import create_document_dialog
    from models.result_document import DocumentResult
    from proactive import dossiers as dmod
    from proactive import findings
    from services.clients import get_session_paperless
    from services.user_prefs import get_pref, set_pref

    user = _user()
    ui.add_css(_LIST_CSS)
    ui.add_css(_MC_CSS)
    ui.add_css(findings_chart.CSS)
    ui.add_body_html(_DOC_CLICK_JS)
    ui.add_head_html(_MC_JS)

    def _pinned_ids() -> set[int]:
        return set(get_pref("pinned_doc_ids") or [])

    def _pin(result: DocumentResult) -> None:
        doc = result.document
        ids = _pinned_ids()
        cache: list[dict] = list(get_pref("pinned_docs_cache") or [])
        if doc.id in ids:
            ids.discard(doc.id)
            cache = [c for c in cache if c["id"] != doc.id]
            ui.notify(_("#{id} unpinned").format(id=doc.id), timeout=1500)
        else:
            ids.add(doc.id)
            cache.append({"id": doc.id, "title": doc.title})
            ui.notify(_("#{id} pinned").format(id=doc.id), type="positive", timeout=1500)
        set_pref("pinned_doc_ids", list(ids))
        set_pref("pinned_docs_cache", cache)

    open_document, _dlg = create_document_dialog(
        pin_fn=_pin, get_pin_state_fn=lambda doc_id: doc_id in _pinned_ids(),
    )

    async def _open_doc_id(doc_id: int) -> None:
        try:
            # The user's own token: a finding names only their documents, but
            # the dialog must not become a way round Paperless' permissions.
            doc = await get_session_paperless().get_document(int(doc_id))
        except Exception:
            ui.notify(_("#{id} not found").format(id=doc_id), type="warning")
            return
        await open_document(DocumentResult(document=doc))

    ui.on(_DOC_EVENT, lambda e: _open_doc_id(int(e.args)))

    sel0: tuple = ("home", "")
    if finding:
        sel0 = ("finding", finding)
    elif dossier:
        sel0 = ("dossier", dossier)
    elif view in BUCKETS:
        sel0 = ("list", view)
    elif view in dmod.SHELVES:
        sel0 = ("shelf", view)
    st: dict = {
        "sel": sel0,
        "tab": tab or "overview",
        "q": "",
        "zone": "viewer" if sel0[0] != "home" else "nav",
        "back": ("home", ""),
        "dock_open": True,  # the chat column is always there
        "dock_ctx": None,  # ("dossier", id) | ("finding", id)
        "dock_max": False,
        "action": action,
    }

    def _url() -> str:
        kind, key = st["sel"]
        if kind == "finding":
            return f"/findings/{quote(key)}"
        if kind == "dossier":
            return f"/findings?d={quote(key)}&tab={quote(st['tab'])}"
        if kind == "list":
            return f"/findings?view={quote(key)}"
        if kind == "shelf":
            return f"/findings?view={quote(key)}"
        return "/findings"

    def select(kind: str, key: str = "", *, tab_: str | None = None) -> None:
        if kind in ("finding", "dossier") and (
                st["sel"][0] in ("list", "home") or (st["sel"][0] == "dossier" and kind == "finding")):
            # A finding opened from its dossier goes back to that dossier (and tab).
            st["back"] = st["sel"] if st["sel"][0] != "dossier" else ("dossier", st["sel"][1], st["tab"])
        st["sel"] = (kind, key)
        if tab_ is not None:
            st["tab"] = tab_
        elif kind == "dossier":
            st["tab"] = "overview"
        st["action"] = ""
        _zone("viewer")
        nav_body.refresh()
        viewer_body.refresh()
        with root:  # the timer must outlive the clicked element
            ui.timer(0.01, _dock_follow, once=True)
        ui.run_javascript(f"history.replaceState(null,'',{json.dumps(_url())})")

    # ── Shell ────────────────────────────────────────────────────────────────
    root = ui.element("div").classes("pbm-root z-" + st["zone"])
    with root:
        nav = ui.element("div").classes("pbm-nav")
        with ui.element("div").classes("pbm-center"):
            viewer = ui.element("div").classes("pbm-viewer")
            ui.element("div").classes("pbm-handle")
            dock = ui.element("div").classes("pbm-dock")
    with ui.element("div").classes("pbm-mbar"), ui.element("div").classes("pbm-seg"):
        mbtns = {}
        for z, icon, label in (("nav", "account_tree", _("Navigator")), ("viewer", "article", _("Viewer")),
                               ("chat", "forum", _("Chat"))):
            mbtns[z] = ui.button(label, icon=icon, color=None, on_click=lambda z=z: _zone(z)).props(
                "flat no-caps dense").classes("pbm-seg-btn" + (" is-on" if z == st["zone"] else ""))

    def _zone(z: str) -> None:
        if z != "chat" and st.get("dock_max"):
            _dock_max(False)  # back to the item: the dock shrinks again
        st["zone"] = z
        root.classes(remove="z-nav z-viewer z-chat", add=f"z-{z}")
        for k, b in mbtns.items():
            b.classes(add="is-on") if k == z else b.classes(remove="is-on")
        if z == "chat":
            _dock_load()

    # ── Navigator ────────────────────────────────────────────────────────────
    with nav:
        with ui.row().classes("w-full items-center gap-2 no-wrap").style("padding:10px 10px 4px;"):
            search = ui.input(placeholder=_("Search findings & dossiers")).props(
                "dense outlined clearable debounce=350").classes("pbm-search flex-1")
            with search.add_slot("prepend"):
                ui.icon("search", size="xs")
            with search.add_slot("append"):
                ui.html('<span class="pbm-kbd">Ctrl K</span>', sanitize=False)

        async def _on_search(e) -> None:
            # Results show in the navigator, under the field: typing never
            # switches the view (on a phone that hid the field mid-word).
            st["q"] = (e.value or "").strip()
            if st["q"] and st.get("titles") is None:
                st["titles"] = {}
                nav_body.refresh()
                st["titles"] = await _doc_titles()
            nav_body.refresh()

        search.on_value_change(_on_search)
        nav_host = ui.element("div").classes("pbm-scroll")

    def _data():
        items = findings.list_findings(user)
        ds = dmod.list_dossiers(user)
        return items, ds

    async def _doc_titles() -> dict[int, str]:
        """Titles of every document the ledger names — read as the user, so a
        document they cannot see stays unnamed."""
        items, ds = _data()
        ids = sorted({i for f in items for i in f.doc_ids}
                     | {i for d in ds for i in set(d.doc_ids) | set(d.added_docs)})
        out: dict[int, str] = {}
        pl = get_session_paperless()
        for k in range(0, len(ids), 100):
            try:
                for doc in await pl.list_documents(ids=ids[k:k + 100]):
                    out[doc.id] = doc.title
            except Exception:
                break
        return out

    def _nav_results(q: str) -> None:
        from proactive.states import display_status

        items, ds = _data()
        res = search_ledger(q, items, ds, st.get("titles") or {})
        today = date.today()
        with ui.column().classes("w-full gap-0 pbm-nsearch"):
            if st.get("titles") == {} and not any(res.values()):
                ui.label(_("Searching …")).classes("pbm-hint").style("padding:4px 8px;")
            elif not any(res.values()):
                ui.label(_("Nothing found.")).classes("pbm-hint").style("padding:4px 8px;")

            def row(icon: str, title: str, sub: str, on_click, warn: bool = False) -> None:
                el = ui.element("div").classes("pbm-nres")
                el.on("click", on_click)
                with el:
                    ui.icon(icon, size="xs").style("color:var(--c-warn)" if warn else "color:var(--c-text-muted)")
                    with ui.column().classes("gap-0 flex-1").style("min-width:0;"):
                        ui.label(title).classes("t w-full")
                        if sub:
                            ui.label(sub).classes("s w-full")

            if res["dossiers"]:
                ui.label(_("Dossiers")).classes("pbm-sec")
                for d in res["dossiers"]:
                    row("folder_open", d.display, d.folder, lambda _e, i=d.pbrain_id: select("dossier", i))
            if res["findings"]:
                ui.label(_("Findings")).classes("pbm-sec")
                for f in res["findings"]:
                    row(fv.STATUS_ICONS.get(display_status(f.status), "lightbulb"), f.title,
                        " · ".join(x for x in (f.topic_display, f.deadline and _("Due {date}").format(date=f.deadline))
                                   if x),
                        lambda _e, i=f.finding_id: select("finding", i), warn=fv.is_pressing(f, today))
            if res["insights"]:
                ui.label(_("Insights")).classes("pbm-sec")
                for d, ins in res["insights"]:
                    row("tips_and_updates", ins.title, f"{d.display} · {ins.created[:10]}",
                        lambda _e, i=d.pbrain_id: select("dossier", i, tab_="insights"))
            if res["documents"]:
                ui.label(_("Documents")).classes("pbm-sec")
                for i, t in res["documents"]:
                    where = [f.title for f in items if i in f.doc_ids][:1] + [
                        d.display for d in ds if i in set(d.doc_ids) | set(d.added_docs)][:1]
                    row("description", f"{t} (#{i})", " · ".join(where), lambda _e, i=i: _open_doc_id(i))

    @ui.refreshable
    def nav_body() -> None:
        if st["q"]:
            _nav_results(st["q"])
            return
        items, ds = _data()
        today = date.today()
        topics = {d.topic for d in ds}
        c = fv.counts(items, today)
        cur = st["sel"]
        rows = [
            ("home", "space_dashboard", _("Overview"), None, False),
            ("open", fv.STATUS_ICONS["open"], _("Inbox"), c["open"], False),
            ("pressing", "priority_high", _("Pressing"), c["pressing"], bool(c["pressing"])),
            ("relevant", fv.STATUS_ICONS["relevant"], _("Relevant"), c["relevant"], False),
            ("unfiled", "inventory_2", _("Not in a dossier"),
             len(_bucket_items(items, "unfiled", today, topics)), False),
            ("done", fv.STATUS_ICONS["done"], _("Done"), len(_bucket_items(items, "done", today, topics)), False),
            ("dismissed", fv.STATUS_ICONS["dismissed"], _("Dismissed"),
             len(_bucket_items(items, "dismissed", today, topics)), False),
        ]
        for key, icon, label, n, warn in rows:
            active = (cur == ("home", "") and key == "home") or cur == ("list", key)
            el = ui.element("div").classes("pbm-bucket" + (" is-active" if active else "") + (" warn" if warn else ""))
            el.on("click", lambda _e, k=key: select("home") if k == "home" else select("list", k))
            with el:
                ui.icon(icon, size="xs")
                ui.label(label)
                if n is not None:
                    ui.label(str(n)).classes("n")
        with ui.element("div").classes("pbm-sec"):
            ui.label(_("Dossiers")).classes("flex-1")
            ui.icon("info_outline", size="14px").classes("card-action-btn").tooltip(_(
                "One binder per matter. Folders only sort them. Findings land in the dossier of their "
                "topic; add documents or file findings by hand, then use “Rescan”."))
            ui.button(icon="create_new_folder", on_click=_new_folder).props("flat dense round size=sm").classes(
                "card-action-btn").tooltip(_("New folder"))
            ui.button(icon="add", on_click=lambda: _new_dossier("")).props("flat dense round size=sm").classes(
                "card-action-btn").tooltip(_("New dossier"))
        open_counts: dict[str, int] = {}
        for f in items:
            if f.status in fv.OPEN_STATUSES and f.topic:
                open_counts[f.topic] = open_counts.get(f.topic, 0) + 1
        nodes = dossier_tree(ds, dmod.folders(user), open_counts)
        colors = topic_colors.colors()

        def _paint(ns):
            for n in ns:
                if n.get("topic") and colors.get(n["topic"]):
                    n["color"] = topic_colors.css_var(colors[n["topic"]])
                _paint(n.get("children") or [])

        _paint(nodes)
        if not nodes:
            ui.label(_("No dossiers yet. They appear when the system follows a matter — or make one yourself.")).classes(
                "pbm-hint").style("padding:4px 14px;")
            return
        tree = ui.tree(nodes, label_key="label", node_key="id",
                       on_select=lambda e: _tree_pick(e.value)).classes("pbm-tree w-full").style("padding:0 6px;")
        tree.add_slot("default-header", _TREE_NODE)
        tree.expand()
        if cur[0] == "dossier":
            tree._props["selected"] = f"d:{cur[1]}"
        _shelf_rows(cur)

    def _shelf_rows(cur) -> None:
        """Archive and trash, under a line below the tree — aligned with its
        top-level entries — only when they hold something."""
        shelves = [(k, i, label, n) for k, i, label in (
            (dmod.ARCHIVE, "archive", _("Archive")), (dmod.TRASH, "delete_outline", _("Deleted dossiers")))
            if (n := len(dmod.list_shelf(user, k)))]
        if not shelves:
            return
        ui.element("div").classes("pbm-shelf-sep")
        for shelf, icon, label, n in shelves:
            el = ui.element("div").classes("pbm-shelf" + (" is-active" if cur == ("shelf", shelf) else ""))
            el.on("click", lambda _e, k=shelf: select("shelf", k))
            with el:
                ui.icon(icon, size="xs")
                ui.label(label)
                ui.label(str(n)).classes("n")

    def _tree_pick(node_id) -> None:
        if node_id and str(node_id).startswith("d:"):
            select("dossier", str(node_id)[2:])
        elif node_id and str(node_id).startswith("f:"):
            _folder_dialog(str(node_id)[2:])

    def _folder_dialog(folder: str) -> None:
        with ui.dialog() as dlg, ui.card().classes("gap-2").style("background:var(--c-surface); width:min(420px,94vw);"):
            ui.label(_("Folder “{name}”").format(name=folder)).classes("text-base font-semibold").style(
                "color:var(--c-text)")
            ui.label(_("Deleting the folder moves its dossiers one level up; nothing else changes.")).classes(
                "pbm-hint")

            async def _del() -> None:
                try:
                    n = await dmod.delete_folder(user, folder)
                except dmod.DossierError as err:
                    ui.notify(_("The folder holds notes that are not dossiers — it stays. Remove them in your "
                                "vault first.") if str(err) == "not_empty" else _("Not a valid folder."),
                              type="warning")
                    return
                dlg.close()
                ui.notify(_("Folder deleted.") + (" " + _("{n} dossiers moved up.").format(n=n) if n else ""),
                          type="positive")  # TODO i18n-plural
                nav_body.refresh()
                viewer_body.refresh()

            def _new_here() -> None:
                dlg.close()
                _new_dossier(folder)

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button(_("New dossier here"), icon="add", on_click=_new_here).props("flat no-caps")
                ui.button(_("Delete folder"), icon="folder_delete", on_click=_del).props(
                    "unelevated no-caps").classes("bg-negative text-white")
        # The tree keeps a folder selected; a second tap would only unselect it.
        dlg.on("hide", lambda: nav_body.refresh())
        dlg.open()

    # ── Viewer ───────────────────────────────────────────────────────────────
    @ui.refreshable
    def viewer_body() -> None:
        kind, key = st["sel"]
        if kind == "finding":
            _finding_view(key)
        elif kind == "dossier":
            _dossier_view(key)
        elif kind == "list":
            _list_view(key)
        elif kind == "shelf":
            _shelf_view(key)
        else:
            _home_view()

    def _back_btn() -> None:
        b = st["back"]
        label = {"home": _("Overview"), "list": _("Back to the list"), "dossier": _("Back to the dossier")}.get(
            b[0], _("Back"))

        def _go_back() -> None:
            if b[0] == "dossier":
                select("dossier", b[1], tab_=b[2] if len(b) > 2 else None)
            else:
                select(*b)

        ui.button(label, icon="arrow_back", on_click=_go_back).props("flat dense no-caps").classes("text-xs")

    def _home_view() -> None:
        items, ds = _data()
        today = date.today()
        c = fv.counts(items, today)
        with ui.column().classes("pbm-panel gap-4"):
            ui.label(_("Findings")).classes("text-xl font-bold").style("color:var(--c-text)")
            with ui.element("div").classes("pbm-kpis"):
                for key, label, n, warn in (("open", _("In the inbox"), c["open"], False),
                                            ("pressing", _("Pressing"), c["pressing"], bool(c["pressing"])),
                                            ("relevant", _("Relevant"), c["relevant"], False),
                                            ("dossiers", _("Dossiers"), len(ds), False)):
                    el = ui.element("div").classes("pbm-kpi" + (" warn" if warn else ""))
                    el.on("click", lambda _e, k=key: (select("list", k) if k != "dossiers" else _zone("nav")))
                    with el:
                        ui.label(str(n)).classes("v")
                        ui.label(label).classes("l")
            if c["researching"]:
                ui.label(_("{n} being researched — it gives way while you chat.").format(n=c["researching"])).classes(
                    "pbm-hint")
            # Next 30 days of deadlines.
            due = sorted((f for f in items if f.status in fv.OPEN_STATUSES and f.deadline
                          and (fv.days_left(f, today) is not None) and 0 <= fv.days_left(f, today) <= 30),
                         key=lambda f: f.deadline)
            with ui.element("div").classes("pbm-card"):
                ui.label(_("Due in the next 30 days")).classes("pbm-label")
                if not due:
                    ui.label(_("Nothing due.")).classes("pbm-hint")
                for f in due:
                    el = ui.element("div").classes("pbm-due" + (" warn" if fv.is_pressing(f, today) else ""))
                    el.on("click", lambda _e, i=f.finding_id: select("finding", i))
                    with el:
                        ui.label(f.deadline).classes("d")
                        ui.label(f.title).classes("t")
            _overview(items, today, _topic_pick)
            recent = sorted(ds, key=lambda d: (d.insights[0].created if d.insights else ""), reverse=True)
            recent = [d for d in recent if d.insights][:5]
            if recent:
                with ui.element("div").classes("pbm-card"):
                    ui.label(_("Latest insights")).classes("pbm-label")
                    for d in recent:
                        ins = d.insights[0]
                        el = ui.element("div").classes("pbm-due")
                        el.on("click", lambda _e, i=d.pbrain_id: select("dossier", i, tab_="insights"))
                        with el:
                            ui.label(ins.created[:10]).classes("d")
                            ui.label(f"{d.display} — {ins.title}").classes("t")

    def _topic_pick(topic: str) -> None:
        d = next((x for x in dmod.list_dossiers(user) if x.topic == topic), None)
        if d is not None:
            select("dossier", d.pbrain_id)
        else:
            st["list_topic"] = topic
            select("list", "open")

    def _list_view(bucket: str) -> None:
        items, ds = _data()
        today = date.today()
        topics = {d.topic for d in ds}
        shown = _bucket_items(items, bucket, today, topics)
        topic_filter = st.pop("list_topic", "")
        if topic_filter:
            shown = [f for f in shown if (f.topic or fv.NO_TOPIC) == topic_filter]
        view_pref = get_pref("findings_view") or {}
        sort = view_pref.get("sort", fv.DEFAULT_VIEW["sort"])
        group = view_pref.get("group", fv.DEFAULT_VIEW["group"])
        title = {"open": _("Inbox"), "pressing": _("Pressing"), "relevant": _("Relevant"),
                 "unfiled": _("Not in a dossier"), "done": _("Done"), "dismissed": _("Dismissed")}.get(bucket, bucket)
        with ui.column().classes("pbm-panel gap-2"):
            with ui.row().classes("w-full items-center gap-2"):
                ui.label(title).classes("text-xl font-bold flex-1").style("color:var(--c-text)")
                ui.label(str(len(shown))).classes("pbm-hint")

                def _setv(k: str, v: str) -> None:
                    pref = dict(get_pref("findings_view") or {})
                    pref[k] = v
                    set_pref("findings_view", pref)
                    viewer_body.refresh()

                groups = {k: _(v) for k, v in fv.GROUPS.items() if k not in ("status", "topic_status")}
                ui.select(groups, value=group if group in groups else "none", label=_("Group"),
                          on_change=lambda e: _setv("group", e.value)).props("outlined dense options-dense").style(
                    "min-width:9rem;")
                ui.select({k: _(v) for k, v in fv.SORTS.items()}, value=sort, label=_("Sort"),
                          on_change=lambda e: _setv("sort", e.value)).props("outlined dense options-dense").style(
                    "min-width:9rem;")
                topics_d = {f.topic: f.topic_display for f in items if f.topic}
                ui.button(icon="palette", on_click=lambda: _topic_colors_dialog(
                    topics_d, lambda: (nav_body.refresh(), viewer_body.refresh()))).props(
                    "flat dense round").classes("card-action-btn").tooltip(_("Topic colours"))
            if topic_filter:
                name = next((f.topic_display for f in shown if f.topic), "") or _("No topic")
                ui.label(_("Topic: {t}").format(t=name)).classes("pbm-hint")
            if bucket == "unfiled" and shown:
                ui.label(_("Open findings whose topic has no dossier. File one into a dossier with the folder "
                           "icon; its documents join that dossier.")).classes("pbm-hint")
            arranged = fv.arrange(shown, status="all", topic="", group=group if group in ("topic", "none") else "none",
                                  sort=sort, today=today)
            if not arranged:
                ui.label(_("Nothing here.")).classes("pbm-hint mt-4")
            for g in arranged:
                if g.kind != "none":
                    _heading(g, top=True)
                for f in g.items:
                    _card(f, today, topic=g.kind != "topic", status=bucket in ("pressing", "unfiled"),
                          on_open=lambda fid: select("finding", fid),
                          on_file=_file_finding if bucket == "unfiled" else None)

    # ── Finding ──────────────────────────────────────────────────────────────
    def _finding_view(fid: str) -> None:
        from proactive import jobs as _jobs
        from proactive import reactions
        from proactive.notify import ACTION_LABELS
        from proactive.states import reactions_for

        f = findings.get(user, fid)
        with ui.column().classes("pbm-panel gap-3"):
            with ui.row().classes("w-full items-center justify-between gap-2"):
                _back_btn()
                with ui.row().classes("items-center gap-1 no-wrap"):
                    from app_ui.research_routes import finding_pdf_url

                    ui.button(icon="picture_as_pdf",
                              on_click=lambda: ui.navigate.to(finding_pdf_url(fid), new_tab=True)).props(
                        "flat dense round").classes("card-action-btn").tooltip(_("Finding as PDF"))
                    if f is not None:
                        d = next((x for x in dmod.list_dossiers(user) if x.topic == f.topic), None) if f.topic else None
                        ui.button(_("File in a dossier") if d is None else _("Move to another dossier"),
                                  icon="drive_file_move", on_click=lambda x=f: _file_finding(x)).props(
                            "flat dense no-caps").classes("text-xs")
            if f is None:
                ui.label(_("This item no longer exists.")).classes("text-sm text-gray-400")
                return
            ui.label(f.title).classes("text-xl font-bold text-gray-100 w-full break-words")
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                _status_icon(f.status, "1.1rem")
                _hints(f)
                ui.label(_meta_line(f)).classes("text-xs text-gray-400 min-w-0 flex-1")
            home = next((x for x in dmod.list_dossiers(user) if x.topic == f.topic), None) if f.topic else None
            if f.doc_ids or home is not None:
                # `finding-md`: the delegated click handler opens `#pbdoc-` links
                # only inside it — outside, #731 was a dead anchor.
                with ui.row().classes("finding-md w-full items-center gap-2 flex-wrap"):
                    if home is not None:
                        ui.button(_("Dossier: {name}").format(name=home.display), icon="folder_open",
                                  on_click=lambda i=home.pbrain_id: select("dossier", i)).props(
                            "flat dense no-caps").classes("text-xs")
                    for did in f.doc_ids:
                        ui.link(f"#{did}", f"#pbdoc-{did}").classes("text-xs")

            if f.status == "researched" and f.research_note:
                ui.label(_("Researched — the newest result is at the top, the report is in your vault.")).classes(
                    "text-xs text-gray-400 w-full")
            elif f.status == "interested":
                from werkbank.v2 import store as _wb_store

                run_status = _wb_store.run_status(f.research_run, user) if f.research_run else None
                failed = run_status in ("failed", "briefing_failed")
                running = run_status is not None and not failed
                queued = _jobs.is_pending(f"deep_dive:{f.finding_id}") or (
                    running and _jobs.is_pending(f"deep_dive_poll:{f.finding_id}"))
                if running:
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        ui.spinner(size="sm").props("color=primary")
                        ui.label(_("Research running — it gives way while you chat.")).classes(
                            "text-xs text-gray-400 flex-1 min-w-0")
                elif queued:
                    ui.label(_("Research queued — it starts as soon as the model is free.")).classes(
                        "text-xs text-gray-400")
                else:
                    reason = _("No research is running for this item.")
                    if failed:
                        from werkbank.v2.ui.page import failure_text

                        run_state = _wb_store.load_state(f.research_run, user)
                        reason = failure_text(run_state) if run_state and run_state.error else _("The research failed.")
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        ui.label(reason).classes("text-xs flex-1 min-w-0").style(
                            "color:var(--c-warn)" if failed else "color:var(--c-text-muted)")
                        ui.button(_("Research again"), icon="travel_explore",
                                  on_click=lambda: _research_again()).props(
                            "flat dense no-caps").classes("text-xs flex-shrink-0")

            async def _research_again() -> None:
                await findings.update(user, fid, lambda x: (
                    {"research_run": None, "research_note": None} if x.status == "interested" else None))
                _jobs.enqueue("deep_dive", user, {"finding_id": fid}, dedupe_key=f"deep_dive:{fid}")
                ui.notify(_("Research queued — it starts as soon as the model is free."), type="positive")
                viewer_body.refresh()

            allowed = reactions_for(f.status, f.kind)

            async def _do(a: str) -> None:
                msg = await reactions.react(user, fid, a, channel="app")
                ui.notify(msg, type="positive")
                # Handled: back to where the user came from.
                b = st["back"]
                st["sel"] = (b[0], b[1])
                if b[0] == "dossier" and len(b) > 2:
                    st["tab"] = b[2]
                nav_body.refresh()
                viewer_body.refresh()
                ui.run_javascript(f"history.replaceState(null,'',{json.dumps(_url())})")

            pending = st.get("action") or ""
            if pending and pending in allowed:
                with ui.card().classes("w-full bg-gray-900 border border-gray-700 p-3"):
                    ui.label(_("Apply “{action}” to this item?").format(action=_(ACTION_LABELS[pending]))).classes(
                        "text-sm text-gray-200")
                    ui.button(_("Confirm"), on_click=lambda a=pending: _do(a)).props("unelevated dense color=primary")
            elif pending:
                ui.label(_("Already handled.")).classes("text-xs text-gray-500")

            if allowed:
                with ui.row().classes("gap-2 flex-wrap"):
                    for a in allowed:
                        if a == "interested":
                            label = _("Research again") if (f.status == "researched" or f.research_note) \
                                else _("Start research")
                        else:
                            label = _(ACTION_LABELS[a])
                        ui.button(label, on_click=lambda a=a: _do(a)).props("flat dense")

            with ui.expansion(_("Add a note"), icon="edit_note").classes("w-full").props("dense"):
                box = ui.textarea(placeholder=_("e.g. The operation took place in April 2024.")).props(
                    "outlined dense autogrow").classes("w-full")

                async def _save_note() -> None:
                    if not (box.value or "").strip():
                        return
                    await findings.add_note(user, fid, box.value)
                    ui.notify(_("Note added."), type="positive")
                    viewer_body.refresh()

                with ui.row().classes("w-full justify-end"):
                    ui.button(_("Save note"), on_click=_save_note).props("unelevated dense no-caps color=primary")
            ui.separator()
            ui.add_css(
                ".finding-md h2{font-size:1rem;font-weight:600;margin:1rem 0 .4rem}"
                ".finding-md h3{font-size:.9rem;font-weight:600;margin:.8rem 0 .3rem}"
                ".finding-md{overflow-x:auto}"
                ".finding-md a{overflow-wrap:anywhere}"
                ".pb-tl-item{position:relative;border-left:2px solid var(--c-border);"
                "padding:0 0 1.1rem 1rem;margin-left:.35rem}"
                ".pb-tl-item:last-child{padding-bottom:0}"
                ".pb-tl-item::before{content:'';position:absolute;left:-6px;top:.3rem;width:10px;height:10px;"
                "border-radius:50%;background:var(--c-surface);border:2px solid var(--c-text-muted)}"
                ".pb-tl-item:first-child::before{border-color:var(--c-text)}"
                ".pb-tl-head{font-weight:600;font-size:.95rem;color:var(--c-text);margin-bottom:.2rem}"
                ".pb-tl-item .finding-md h3:first-child{margin-top:.3rem}"
            )
            entries, original = findings.split_timeline(findings.tidy_sources(f.body, f.lang), f.lang)
            tl = [(e.split("\n", 1)[0][3:].strip(), e.split("\n", 1)[1] if "\n" in e else "") for e in entries]
            tl.append((findings.original_heading(f), original))
            with ui.element("div").classes("pb-tl w-full min-w-0"):
                for head, text in tl:
                    with ui.element("div").classes("pb-tl-item w-full min-w-0"):
                        ui.label(head).classes("pb-tl-head")
                        ui.markdown(_in_app_links(text.strip(), user)).classes("chat-md finding-md w-full min-w-0")

    # ── Dossier binder ───────────────────────────────────────────────────────
    def _dossier_view(pid: str) -> None:
        d = dmod.by_id(user, pid)
        with ui.column().classes("w-full gap-0"):
            if d is None:
                with ui.column().classes("pbm-panel"):
                    _back_btn()
                    ui.label(_("This dossier no longer exists.")).classes("pbm-hint")
                return
            items = dmod.findings_of(user, d.topic)
            with ui.column().classes("pbm-panel gap-1").style("padding-bottom:6px;"):
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    ui.icon("folder_open", size="md").classes("text-gray-300")
                    with ui.column().classes("gap-0 flex-1").style("min-width:0;"):
                        ui.label(d.display).classes("text-xl font-bold w-full").style(
                            "color:var(--c-text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;")
                        sub = [_("{n} documents").format(n=len(dmod.all_doc_ids(user, d))),  # TODO i18n-plural
                               _("{n} findings").format(n=len(items)),  # TODO i18n-plural
                               _("{n} insights").format(n=len(d.insights))]  # TODO i18n-plural
                        if d.revisit_after:
                            sub.append(_("revisit {date}").format(date=d.revisit_after))
                        if d.origin == "user":
                            sub.append(_("made by you"))
                        ui.label(" · ".join(sub)).classes("pbm-hint w-full")
                    with ui.button(icon="more_vert").props("flat dense round").classes("card-action-btn"):
                        with ui.menu():
                            ui.menu_item(_("Rescan — rewrite it from all its documents"), on_click=lambda: _relook(d))
                            ui.menu_item(_("Add documents"), on_click=lambda: _add_docs(d))
                            ui.separator()
                            ui.menu_item(_("Rename"), on_click=lambda: _rename_dossier(d))
                            ui.menu_item(_("Move to folder"), on_click=lambda: _move_dossier(d))
                            from services import experts as _ex

                            if _ex.enabled():
                                ui.menu_item(_("Default expert for its chat"), on_click=lambda: _dossier_expert(d))
                            ui.separator()
                            ui.menu_item(_("Archive — close the matter"), on_click=lambda: _archive_dossier(d))
                            ui.menu_item(_("Reset — delete and read its documents again"),
                                         on_click=lambda: _reset_dossier(d))
                            ui.menu_item(_("Delete dossier"), on_click=lambda: _delete_dossier(d))
                _relook_banner(d)
            all_docs = dmod.all_doc_ids(user, d)
            tabs = [("overview", _("Overview"), None), ("timeline", _("Timeline"), len(d.timeline)),
                    ("findings", _("Findings"), len(items)), ("insights", _("Insights"), len(d.insights)),
                    ("documents", _("Documents"), len(all_docs)), ("research", _("Research"), len(d.research))]
            if st["tab"] not in {k for k, _l, _n in tabs}:
                st["tab"] = "overview"
            with ui.element("div").classes("pbm-tabs w-full"):
                for key, label, n in tabs:
                    t = ui.element("div").classes("pbm-tab" + (" is-active" if key == st["tab"] else ""))
                    t.on("click", lambda _e, k=key: _set_tab(k))
                    with t:
                        ui.label(label if n is None else f"{label} {n}")
            with ui.column().classes("pbm-panel gap-3"):
                {"overview": _tab_overview, "timeline": _tab_timeline, "findings": _tab_findings,
                 "insights": _tab_insights, "documents": _tab_documents, "research": _tab_research}[st["tab"]](d, items)

    def _relook_state(d) -> str:
        from proactive import jobs as _jobs

        try:
            return "queued" if _jobs.is_pending(dmod.relook_key(user, d.topic)) else ""
        except Exception:  # proactive.db unreadable: no state, not an error page
            return ""

    def _relook_banner(d) -> None:
        pending = dmod.pending_docs(user, d)
        state = _relook_state(d)
        if not pending and not state:
            return
        with ui.element("div").classes("pbm-pending w-full"):
            if state:
                ui.spinner(size="xs").props("color=grey")
                ui.label(_("Rescan queued — the model rewrites this dossier from all documents of the matter; it gives way "
                           "while you chat.")).classes("flex-1 min-w-0")
                return
            ui.icon("inventory", size="xs")
            ui.label(_("{n} documents of this matter are not in the dossier yet (added by you or from "
                       "its findings).").format(n=len(pending))).classes("flex-1 min-w-0")  # TODO i18n-plural
            ui.button(_("Rescan"), icon="refresh", on_click=lambda: _relook(d)).props("flat dense no-caps")

    def _relook(d) -> None:
        from proactive import jobs as _jobs
        from proactive.models import resolve_model
        from services.background_access import token_for

        tok = token_for(user)
        if not tok:
            ui.notify(_("Allow background access first (Settings > Proactive) — the rescan runs in the "
                        "background, as you."), type="warning")
            return
        if not dmod.all_doc_ids(user, d):
            ui.notify(_("This dossier has no documents yet. Add some, or file a finding into it."), type="warning")
            return
        _jobs.enqueue("dossier_relook", user, {"topic": d.topic}, needs_llm=True,
                      model=resolve_model(user, tok), dedupe_key=dmod.relook_key(user, d.topic))
        ui.notify(_("Rescan queued. State, timeline and open questions are rewritten; your insights, research and added documents stay."), type="positive")
        viewer_body.refresh()

    def _add_docs(d) -> None:
        chosen: dict[int, str] = {}
        with ui.dialog() as dlg, ui.card().classes("gap-2").style(
                "background:var(--c-surface); width:min(560px,96vw); max-height:85vh;"
                "display:flex; flex-direction:column; flex-wrap:nowrap;"):
            ui.label(_("Add documents to “{name}”").format(name=d.display)).classes(
                "text-base font-semibold").style("color:var(--c-text)")
            ui.label(_("Search your documents by title or content, or type #123. Added documents are read "
                       "on the next rescan and never dropped by a rebuild.")).classes("pbm-hint")
            q = ui.input(placeholder=_("Search documents")).props("outlined dense clearable debounce=400").classes(
                "w-full")
            res = ui.element("div").classes("w-full").style("flex:1; min-height:120px; overflow-y:auto;")
            have = set(dmod.all_doc_ids(user, d))

            async def _search(e=None) -> None:
                text = (q.value or "").strip()
                res.clear()
                if len(text) < 2:
                    return
                try:
                    pl = get_session_paperless()
                    if re.fullmatch(r"#?\d+", text):
                        docs = await pl.list_documents(ids=[int(text.lstrip("#"))])
                    else:
                        docs = await pl.list_documents(query=text, fetch_all=False, page_size=25)
                except Exception:
                    with res:
                        ui.label(_("Paperless is not reachable.")).classes("pbm-hint")
                    return
                with res:
                    if not docs:
                        ui.label(_("Nothing found.")).classes("pbm-hint")
                    for doc in docs:
                        label = f"{doc.title} (#{doc.id}) · {doc.created.date().isoformat()}"
                        if doc.id in have:
                            ui.label(label + " — " + _("already in it")).classes("pbm-hint").style(
                                "padding:4px 0;")
                            continue
                        ui.checkbox(label, value=doc.id in chosen, on_change=lambda e, i=doc.id, t=doc.title: (
                            chosen.__setitem__(i, t) if e.value else chosen.pop(i, None))).props("dense").classes(
                            "text-sm w-full")

            q.on_value_change(_search)

            async def _ok() -> None:
                if chosen:
                    await dmod.add_documents(user, d.topic, list(chosen))
                    ui.notify(_("Added. Press Rescan to have the model read them."), type="positive")
                dlg.close()
                viewer_body.refresh()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button(_("Cancel"), on_click=dlg.close).props("flat no-caps")
                ui.button(_("Add"), on_click=_ok).props("unelevated no-caps").classes("bg-primary text-white")
        dlg.open()

    def _file_finding(f) -> None:
        """Put a finding into a dossier (it takes the dossier's topic)."""
        new_key = "__new__"
        ds = sorted(dmod.list_dossiers(user), key=lambda x: (x.folder.casefold(), x.display.casefold()))
        opts = {x.topic: (f"{x.folder} / {x.display}" if x.folder else x.display) for x in ds if x.topic != f.topic}
        opts[new_key] = _("New dossier …")
        with ui.dialog() as dlg, ui.card().classes("gap-2").style("background:var(--c-surface); width:min(440px,94vw);"):
            ui.label(_("File in a dossier")).classes("text-base font-semibold").style("color:var(--c-text)")
            ui.label(_("The finding moves to the dossier's topic; its documents join the dossier and are "
                       "read on the next rescan.")).classes("pbm-hint")
            sel = ui.select(opts, value=next(iter(opts)), label=_("Dossier"), with_input=True).props(
                "outlined dense options-dense").classes("w-full")
            name = ui.input(_("Name of the new dossier")).props("outlined dense").classes("w-full")
            name.set_visibility(sel.value == new_key)
            sel.on_value_change(lambda e: name.set_visibility(e.value == new_key))

            async def _ok() -> None:
                topic = sel.value
                try:
                    if topic == new_key:
                        nd = await dmod.create_manual(user, name.value or "", lang=get_pref("language", "en"))
                        topic = nd.topic
                    await dmod.file_finding(user, f.finding_id, topic)
                except dmod.DossierError as err:
                    ui.notify({"exists": _("That name is taken."), "name": _("Enter a name.")}.get(
                        str(err), _("Could not save.")), type="warning")
                    return
                dlg.close()
                target = dmod.load(user, topic)
                ui.notify(_("Filed in “{name}”.").format(name=target.display if target else topic), type="positive")
                nav_body.refresh()
                viewer_body.refresh()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button(_("Cancel"), on_click=dlg.close).props("flat no-caps")
                ui.button(_("File"), on_click=_ok).props("unelevated no-caps").classes("bg-primary text-white")
        dlg.open()

    def _ask_question(d, q: str) -> None:
        """The question goes into the dock's input — the user sends it (or
        edits it first); the dock opens on this dossier."""
        _dock_ask(("dossier", d.pbrain_id))
        ui.run_javascript(f"window.pbmPrefill && window.pbmPrefill({json.dumps(q)})")

    def _set_tab(k: str) -> None:
        st["tab"] = k
        viewer_body.refresh()
        ui.run_javascript(f"history.replaceState(null,'',{json.dumps(_url())})")

    def _tab_overview(d, items) -> None:
        with ui.element("div").classes("pbm-card"):
            ui.label(_("Current state")).classes("pbm-label")
            if d.current_state.strip():
                ui.markdown(_in_app_links(d.current_state, user)).classes("chat-md finding-md w-full")
            else:
                ui.label(_("Not written yet — it is built from the documents of this matter.")
                         if d.origin != "user" else _("Save insights from the chat to fill this dossier.")).classes("pbm-hint")
        if d.insights:
            # Right under the state: the newest thing the user learned about
            # the matter. The state is the model's, rewritten by a rescan
            # (which reads the insights); an insight is the user's own, kept.
            with ui.element("div").classes("pbm-card"):
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    ui.label(_("Latest insight")).classes("pbm-label flex-1")
                    if len(d.insights) > 1:
                        ui.button(_("All {n}").format(n=len(d.insights)), on_click=lambda: _set_tab("insights")).props(
                            "flat dense no-caps size=sm")
                _insight(d, d.insights[0], compact=True)
        with ui.element("div").classes("pbm-card"):
            ui.label(_("Open questions")).classes("pbm-label")
            ui.label(_("Tick one off when it is answered, ask the chat about it, or remove it. A rescan "
                       "never brings back what you ticked or removed.")).classes("pbm-hint")

            async def _q(fn, q: str) -> None:
                await fn(user, d.topic, q)
                viewer_body.refresh()

            if not d.open_questions and not d.resolved:
                ui.label(_("None.")).classes("pbm-hint")
            for q in d.open_questions:
                with ui.element("div").classes("pbm-q"):
                    ui.button(icon="radio_button_unchecked").props("flat dense round size=sm").classes(
                        "card-action-btn flex-shrink-0").on(
                        "click", lambda _e, q=q: _q(dmod.resolve_question, q)).tooltip(_("Answered"))
                    ui.label(q).classes("t")
                    ui.button(icon="forum").props("flat dense round size=sm").classes(
                        "card-action-btn flex-shrink-0").on(
                        "click", lambda _e, q=q: _ask_question(d, q)).tooltip(_("Ask the chat"))
                    ui.button(icon="close").props("flat dense round size=sm").classes(
                        "card-action-btn flex-shrink-0").on(
                        "click", lambda _e, q=q: _q(dmod.drop_question, q)).tooltip(_("Not a question here — remove"))
            for r in d.resolved:
                with ui.element("div").classes("pbm-q is-done"):
                    ui.button(icon="check_circle").props("flat dense round size=sm").classes(
                        "card-action-btn flex-shrink-0").on(
                        "click", lambda _e, q=r["question"]: _q(dmod.reopen_question, q)).tooltip(_("Open again"))
                    ui.label(r["question"]).classes("t")
                    if r.get("insight"):
                        ui.button(_("Answer"), on_click=lambda: _set_tab("insights")).props(
                            "flat dense no-caps size=sm").classes("flex-shrink-0")
            new_q = ui.input(placeholder=_("Add a question of your own")).props(
                "dense borderless").classes("w-full text-sm pbm-q-add")

            async def _add_q() -> None:
                if (new_q.value or "").strip():
                    await dmod.add_question(user, d.topic, new_q.value)
                    viewer_body.refresh()

            new_q.on("keydown.enter", _add_q)
            with new_q.add_slot("append"):
                ui.button(icon="add", on_click=_add_q).props("flat dense round size=sm").classes("card-action-btn")
        live = [f for f in items if f.status in fv.OPEN_STATUSES]
        if live:
            with ui.element("div").classes("pbm-card"):
                ui.label(_("Open findings")).classes("pbm-label")
                today = date.today()
                for f in live[:5]:
                    _card(f, today, topic=False, status=True, on_open=lambda fid: select("finding", fid))

    def _tab_timeline(d, items) -> None:
        from proactive.findings import paperless_link  # noqa: F401 — links rewritten below

        if not d.timeline:
            ui.label(_("No timeline yet.")).classes("pbm-hint")
            return
        with ui.element("div").classes("pb-tl w-full min-w-0"):
            for t in reversed(d.timeline):
                with ui.element("div").classes("pb-tl-item w-full min-w-0"):
                    ui.label(t["date"]).classes("pb-tl-head")
                    ui.markdown(f"{t['event']} — [#{t['doc_id']}](#pbdoc-{t['doc_id']})").classes(
                        "chat-md finding-md w-full")
        ui.add_css(
            ".pb-tl-item{position:relative;border-left:2px solid var(--c-border);padding:0 0 1rem 1rem;margin-left:.35rem}"
            ".pb-tl-item::before{content:'';position:absolute;left:-6px;top:.3rem;width:10px;height:10px;"
            "border-radius:50%;background:var(--c-surface);border:2px solid var(--c-text-muted)}"
            ".pb-tl-head{font-weight:600;font-size:.9rem;color:var(--c-text)}")

    def _tab_findings(d, items) -> None:
        if not items:
            ui.label(_("No findings on this matter.")).classes("pbm-hint")
            return
        today = date.today()
        for f in sorted(items, key=fv.sort_key("relevance", today)):
            _card(f, today, topic=False, status=True, on_open=lambda fid: select("finding", fid))

    def _evidence(kind: str) -> str:
        return {"documents": _("documents"), "web": _("web"), "documents+web": _("documents + web"),
                "reasoning": _("reasoning only")}.get(kind, kind)

    def _insight(d, ins, compact: bool = False) -> None:
        from app_ui.findings_view import insight_origin

        with ui.element("div").classes("pbm-ins w-full"):
            with ui.row().classes("w-full items-start gap-2 no-wrap"):
                with ui.column().classes("gap-0 flex-1").style("min-width:0;"):
                    ui.html(f"<h4>{_esc(ins.title)}</h4>", sanitize=False)
                    with ui.row().classes("items-center gap-2"):
                        ui.label(insight_origin(ins, _)).classes("pbm-hint")
                        if ins.conv_id and _conv_exists(ins.conv_id):
                            ui.link(_("Open the conversation"), f"/chat?conv={quote(ins.conv_id)}").classes(
                                "pbm-hint").style("text-decoration:underline")

                async def _del(i=ins.insight_id) -> None:
                    await dmod.delete_insight(user, d.topic, i)
                    ui.notify(_("Insight removed."), type="positive")
                    viewer_body.refresh()
                    nav_body.refresh()

                ui.button(icon="delete_outline", on_click=lambda: _confirm(
                    _("Remove this insight from the dossier? The chat conversation stays."), _del)).props(
                    "flat dense round size=sm").classes("card-action-btn").tooltip(_("Remove insight"))
            # The whole text: a long one is folded, never cut (it was cut at
            # 600 characters mid-word, with nothing to open the rest).
            body = ui.markdown(_in_app_links(ins.text, user)).classes(
                "chat-md finding-md w-full" + (" pbm-fold" if compact and len(ins.text) > 700 else ""))
            if compact and len(ins.text) > 700:
                more = ui.button(_("Show all"), icon="expand_more").props("flat dense no-caps size=sm").classes(
                    "self-start")

                def _unfold(b=body, m=more) -> None:
                    b.classes(remove="pbm-fold")
                    m.set_visibility(False)

                more.on_click(_unfold)
            parts = [f'<span class="pbm-evid">{_esc(_evidence(ins.evidence))}</span>']
            if ins.docs:
                parts.append(" · ".join(
                    f'<a href="#pbdoc-{int(x["id"])}">#{int(x["id"])}' + (f' p{int(x["page"])}' if x.get("page") else "")
                    + "</a>" for x in ins.docs))
            for w in ins.web[:4]:
                u = _esc(w["url"])
                parts.append(f'<a href="{u}" target="_blank" rel="noopener">{u}</a>')
            who = " · ".join(x for x in (ins.model, ins.expert) if x)
            if who:
                parts.append(_esc(who))
            if ins.unsupported:
                parts.append('<span style="color:var(--c-warn)">' + _esc(
                    _("unconfirmed numbers: {n}").format(n=", ".join(ins.unsupported))) + "</span>")
            ui.html('<div class="pbm-src finding-md">' + " · ".join(parts) + "</div>", sanitize=False)

    def _conv_exists(conv_id: str) -> bool:
        import services.chat_history_service as hist

        try:
            return any(c["id"] == conv_id for c in hist.list_conversations(user))
        except Exception:  # noqa: BLE001 — history unreadable: no link
            return False

    def _confirm(text: str, on_ok, ok_label: str | None = None, danger: bool = True) -> None:
        with ui.dialog() as dlg, ui.card().style("background:var(--c-surface); width:min(440px,94vw);"):
            ui.label(text).classes("text-sm").style("color:var(--c-text)")

            async def _ok() -> None:
                dlg.close()
                await on_ok()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button(_("Cancel"), on_click=dlg.close).props("flat no-caps")
                ui.button(ok_label or _("Remove"), on_click=_ok).props("unelevated no-caps").classes(
                    "text-white " + ("bg-negative" if danger else "bg-primary"))
        dlg.open()

    def _tab_insights(d, items) -> None:
        ui.label(_("Saved from chat answers with “Save to dossier”. Sources come from the answer's own tool "
                   "calls — the model cannot add any.")).classes("pbm-hint")
        if not d.insights:
            ui.button(_("Ask about this dossier"), icon="forum", on_click=lambda: _dock_ask(("dossier", d.pbrain_id))).props(
                "flat no-caps")
            return
        for ins in d.insights:
            _insight(d, ins)

    def _tab_documents(d, items) -> None:
        host = ui.column().classes("w-full gap-0")
        with host:
            ui.spinner()

        async def _load() -> None:
            ids = dmod.all_doc_ids(user, d)
            read, added = set(d.doc_ids), set(d.added_docs)
            host.clear()
            if not ids:
                with host:
                    ui.label(_("No documents linked yet.")).classes("pbm-hint")
                return
            try:
                docs = await get_session_paperless().list_documents(ids=ids)
            except Exception:
                with host:
                    ui.label(_("Paperless is not reachable.")).classes("pbm-hint")
                return
            with host:
                with ui.row().classes("w-full items-center gap-2"):
                    ui.label(_("{n} documents").format(n=len(docs))).classes("pbm-hint flex-1")  # TODO i18n-plural
                    ui.button(_("Add documents"), icon="add", on_click=lambda: _add_docs(d)).props(
                        "flat dense no-caps").classes("text-xs")
                for doc in sorted(docs, key=lambda x: x.created, reverse=True):
                    # Two lines, full width: the name wraps instead of being cut,
                    # and the remove button can never sit past the screen edge.
                    el = ui.element("div").classes("pbm-doc w-full")
                    el.on("click", lambda _e, i=doc.id: _open_doc_id(i))
                    with el:
                        with ui.element("div").classes("pbm-doc-main"):
                            ui.label(f"{doc.title} (#{doc.id})").classes("t")
                            meta = [doc.created.date().isoformat()] + ([doc.correspondent] if doc.correspondent else [])
                            with ui.element("div").classes("m"):
                                ui.label(" · ".join(meta))
                                tag = _("added by you") if doc.id in added else (
                                    "" if doc.id in read else _("not read yet"))
                                if tag:
                                    ui.label(tag).classes("pbm-evid")
                        if doc.id in added:
                            async def _rm(doc_id: int) -> None:
                                await dmod.remove_document(user, d.topic, doc_id)
                                ui.notify(_("Taken out of the dossier."), type="positive")
                                viewer_body.refresh()

                            # The id is bound in the lambda: a handler with a
                            # parameter of its own is handed the click event.
                            ui.button(icon="remove_circle_outline").props("flat dense round").classes(
                                "card-action-btn flex-shrink-0").on("click.stop", lambda _e, i=doc.id: _rm(i)).tooltip(
                                _("Take out of the dossier"))

        ui.timer(0.05, _load, once=True)

    def _tab_research(d, items) -> None:
        if not d.research:
            ui.label(_("No research on this matter yet. “Start research” on one of its findings.")).classes("pbm-hint")
            return
        for link in reversed(d.research):
            ui.markdown(_in_app_links(link, user)).classes("chat-md finding-md w-full")

    # ── Archive and trash ────────────────────────────────────────────────────
    def _shelf_view(shelf: str) -> None:
        archive = shelf == dmod.ARCHIVE
        rows = dmod.list_shelf(user, shelf)
        with ui.column().classes("pbm-panel gap-3"):
            _back_btn()
            ui.label(_("Archive") if archive else _("Deleted dossiers")).classes("text-xl font-bold").style(
                "color:var(--c-text)")
            ui.label(_("Closed matters. Their findings are done, nothing is sent for them; a new document of "
                       "the matter is only noted. Unarchive brings everything back.") if archive else _(
                "What was deleted, kept as a record: restore it, or reset it — the worker then reads its "
                "documents again as if new.")).classes("pbm-hint")
            if not rows:
                ui.label(_("Nothing here.")).classes("pbm-hint")
            for d in rows:
                with ui.element("div").classes("pbm-card"):
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        ui.icon("archive" if archive else "delete_outline", size="sm").classes("text-gray-400")
                        with ui.column().classes("gap-0 flex-1").style("min-width:0;"):
                            ui.label(d.display).classes("text-sm font-semibold w-full").style("color:var(--c-text)")
                            sub = [_("archived {date}").format(date=d.archived) if archive else (
                                _("reset {date}").format(date=d.reset) if d.reset
                                else _("deleted {date}").format(date=d.deleted)),
                                _("{n} documents").format(n=len(set(d.doc_ids) | set(d.added_docs)))]  # TODO i18n-plural
                            if d.new_since_archive:
                                sub.append(_("{n} new since").format(n=len(d.new_since_archive)))  # TODO i18n-plural
                            if d.insights:
                                sub.append(_("{n} insights").format(n=len(d.insights)))  # TODO i18n-plural
                            ui.label(" · ".join(sub)).classes("pbm-hint w-full")
                        with ui.row().classes("gap-1 no-wrap flex-shrink-0"):
                            if archive:
                                ui.button(_("Unarchive"), icon="unarchive",
                                          on_click=lambda x=d: _shelf_do(dmod.unarchive, x,
                                                                         _("Back in your dossiers."))).props(
                                    "flat dense no-caps").classes("text-xs")
                            else:
                                ui.button(_("Restore"), icon="restore",
                                          on_click=lambda x=d: _shelf_do(dmod.restore, x,
                                                                         _("Back in your dossiers."))).props(
                                    "flat dense no-caps").classes("text-xs")
                            with ui.button(icon="more_vert").props("flat dense round").classes("card-action-btn"):
                                with ui.menu():
                                    ui.menu_item(_("Reset — read its documents again"),
                                                 on_click=lambda x=d: _reset_dossier(x))
                                    if archive:
                                        ui.menu_item(_("Delete dossier"), on_click=lambda x=d: _delete_dossier(x))
                                    else:
                                        ui.menu_item(_("Remove the record for good"),
                                                     on_click=lambda x=d: _purge(x))
                    if d.current_state.strip():
                        with ui.expansion(_("Last state"), icon="notes").classes("w-full").props("dense"):
                            ui.markdown(_in_app_links(d.current_state, user)).classes("chat-md finding-md w-full")

    async def _shelf_do(fn, d, done: str) -> None:
        try:
            out = await fn(user, d.pbrain_id)
        except dmod.DossierError as err:
            ui.notify(_("A dossier of that matter exists already — delete or archive it first.")
                      if str(err) == "exists" else _("This dossier no longer exists."), type="warning")
            return
        ui.notify(done, type="positive")
        if out is not None and getattr(out, "pbrain_id", "") and not out.shelf:
            select("dossier", out.pbrain_id)
        else:
            nav_body.refresh()
            viewer_body.refresh()

    def _after_shelving() -> None:
        st["sel"] = ("home", "")
        nav_body.refresh()
        viewer_body.refresh()
        ui.run_javascript(f"history.replaceState(null,'',{json.dumps(_url())})")

    def _archive_dossier(d) -> None:
        async def _ok() -> None:
            try:
                await dmod.archive(user, d.topic)
            except dmod.DossierError:
                ui.notify(_("This dossier no longer exists."), type="warning")
                return
            ui.notify(_("Archived. Its findings are done; you find it under Archive."), type="positive")
            _after_shelving()

        _confirm(_("Archive “{name}”? Its open findings and deadlines are closed and nothing is sent for "
                   "them any more. A new document of the matter is only noted. You can unarchive it at any "
                   "time.").format(name=d.display), _ok, ok_label=_("Archive"), danger=False)

    def _delete_dossier(d) -> None:
        async def _ok() -> None:
            try:
                await dmod.delete_dossier(user, d.pbrain_id)
            except dmod.DossierError:
                ui.notify(_("This dossier no longer exists."), type="warning")
                return
            ui.notify(_("Deleted. A record is kept under “Deleted dossiers”."), type="positive")
            _after_shelving()

        _confirm(_("Delete “{name}”? Its findings stay (they show as not in a dossier), and nothing is read "
                   "again. A record — name, documents, insights — is kept under “Deleted dossiers”, so you "
                   "can restore or reset it later.").format(name=d.display), _ok, ok_label=_("Delete"))

    def _reset_dossier(d) -> None:
        from services.background_access import token_for

        if not token_for(user):
            ui.notify(_("Allow background access first (Settings > Proactive) — the worker reads the "
                        "documents in the background, as you."), type="warning")
            return
        n_find = len(dmod.findings_of(user, d.topic))

        async def _ok() -> None:
            from proactive import jobs as _jobs
            from proactive.models import resolve_model
            from proactive.tasks.documents import DOC_REPROCESS, reprocess_key

            tok = token_for(user)
            if not tok:
                return
            try:
                ids = await dmod.reset_dossier(user, d.pbrain_id)
            except dmod.DossierError as err:
                ui.notify(_("A dossier of that matter exists already — delete or archive it first.")
                          if str(err) == "exists" else _("This dossier no longer exists."), type="warning")
                return
            model = resolve_model(user, tok)
            for i in ids:
                _jobs.enqueue(DOC_REPROCESS, user, {"doc_id": int(i)}, needs_llm=True, model=model,
                              dedupe_key=reprocess_key(user, i))
            ui.notify(_("Reset. {n} documents are queued; the worker reads them as soon as the model is "
                        "free, and writes findings — and a dossier — again.").format(n=len(ids)),
                      type="positive")  # TODO i18n-plural
            _after_shelving()

        _confirm(_("Reset “{name}”? The dossier and its {n} findings are deleted — notes and research "
                   "results in those findings too — and the worker reads its documents again as if new. "
                   "A record of the dossier is kept under “Deleted dossiers”.").format(
            name=d.display, n=n_find), _ok, ok_label=_("Reset"))  # TODO i18n-plural

    def _purge(d) -> None:
        async def _ok() -> None:
            await dmod.purge(user, d.pbrain_id)
            ui.notify(_("Record removed."), type="positive")
            nav_body.refresh()
            viewer_body.refresh()

        _confirm(_("Remove the record of “{name}” for good? This cannot be undone.").format(name=d.display),
                 _ok, ok_label=_("Remove"))

    # ── Dossier actions ──────────────────────────────────────────────────────
    def _ask_text(title: str, label: str, value: str, on_ok, options: dict | None = None, hint: str = "") -> None:
        with ui.dialog() as dlg, ui.card().style("background:var(--c-surface); width:min(420px,94vw);"):
            ui.label(title).classes("text-base font-semibold").style("color:var(--c-text)")
            if hint:
                ui.label(hint).classes("pbm-hint")
            if options is not None:
                inp = ui.select(options, value=value, label=label, with_input=True,
                                new_value_mode="add-unique").props("outlined dense").classes("w-full")
            else:
                inp = ui.input(label, value=value).props("outlined dense autofocus").classes("w-full")

            async def _ok() -> None:
                try:
                    await on_ok(inp.value or "")
                except dmod.DossierError as err:
                    ui.notify({"exists": _("That name is taken."), "name": _("Enter a name."),
                               "folder": _("Not a valid folder.")}.get(str(err), _("Could not save.")), type="warning")
                    return
                dlg.close()
                nav_body.refresh()
                viewer_body.refresh()

            with ui.row().classes("w-full justify-end gap-2"):
                ui.button(_("Cancel"), on_click=dlg.close).props("flat no-caps")
                ui.button(_("OK"), on_click=_ok).props("unelevated no-caps").classes("bg-primary text-white")
        dlg.open()

    def _new_dossier(folder: str) -> None:
        async def _ok(name: str) -> None:
            d = await dmod.create_manual(user, name, folder, lang=get_pref("language", "en"))
            st["sel"] = ("dossier", d.pbrain_id)

        _ask_text(_("New dossier"), _("Name"), "", _ok, hint=_(
            "A dossier is a binder for one matter. It starts empty: add documents to it, file findings into "
            "it, or save chat answers to it — then “Rescan” has the model write its state, timeline and open "
            "questions from those documents. New findings whose topic matches its name land in it by themselves."))

    def _new_folder() -> None:
        async def _ok(name: str) -> None:
            if not name.strip():
                raise dmod.DossierError("name")
            await dmod.ensure_folder(user, name)

        _ask_text(_("New folder"), _("Folder (use / for subfolders)"), "", _ok, hint=_(
            "Folders only sort your dossiers, like folders in Obsidian (they are folders in your vault). "
            "Nothing reads them, and moving a dossier changes nothing about what lands in it."))

    def _rename_dossier(d) -> None:
        async def _ok(name: str) -> None:
            if not name.strip():
                raise dmod.DossierError("name")
            await dmod.rename(user, d.topic, name)

        _ask_text(_("Rename dossier"), _("Name"), d.display, _ok, hint=_(
            "Only the name changes. The dossier keeps its topic, so new findings and documents of this "
            "matter still land in it."))

    def _move_dossier(d) -> None:
        async def _ok(folder: str) -> None:
            await dmod.move(user, d.topic, folder)

        opts = {"": _("(top level)"), **{p: p for p in dmod.folders(user)}}
        _ask_text(_("Move to folder"), _("Folder"), d.folder, _ok, options=opts)

    def _dossier_expert(d) -> None:
        """Pick the expert this dossier's chat starts with — each with its icon;
        "" = the user's default expert. The dock follows at once."""
        from services import experts

        default = experts.get_or_universal(user, experts.default_id(user))
        rows = [("", default.icon or "psychology", _("Your default expert ({name})").format(name=default.name))]
        rows += [(e.expert_id, e.icon or "psychology", e.name) for e in experts.list_experts(user)]
        chosen = {"v": d.expert or ""}
        with ui.dialog() as dlg, ui.card().classes("gap-1").style(
                "background:var(--c-surface); width:min(420px,94vw); max-height:80vh; overflow-y:auto;"):
            ui.label(_("Default expert for this dossier's chat")).classes("text-base font-semibold").style(
                "color:var(--c-text)")
            host = ui.column().classes("w-full gap-0")

            def _rows() -> None:
                host.clear()
                with host:
                    for eid, icon, name in rows:
                        el = ui.element("div").classes("pbm-nres" + (" is-on" if chosen["v"] == eid else ""))
                        el.on("click", lambda _e, i=eid: (chosen.update(v=i), _rows()))
                        with el:
                            ui.icon("radio_button_checked" if chosen["v"] == eid else "radio_button_unchecked",
                                    size="xs").style("color:var(--c-text-muted)")
                            ui.icon(icon, size="sm").style("color:var(--c-text-2)")
                            ui.label(name).classes("t")

            _rows()

            async def _ok() -> None:
                await dmod.set_expert(user, d.topic, chosen["v"])
                dlg.close()
                viewer_body.refresh()
                if st["dock_ctx"] == ("dossier", d.pbrain_id):
                    _dock_load(("dossier", d.pbrain_id), force=True)  # the chat switches with it

            with ui.row().classes("w-full justify-end gap-2 mt-2"):
                ui.button(_("Cancel"), on_click=dlg.close).props("flat no-caps")
                ui.button(_("OK"), on_click=_ok).props("unelevated no-caps").classes("bg-primary text-white")
        dlg.open()

    ui.on("pb_insight_saved", lambda _e: (viewer_body.refresh(), nav_body.refresh()))
    ui.on("pb_dock_max", lambda e: _dock_max(bool(e.args)))

    # ── Chat dock ────────────────────────────────────────────────────────────
    with dock:
        # No "Chat about …" title: the dock sits right under what it is about.
        with ui.element("div").classes("pbm-dock-head") as dock_head:
            retarget = ui.button("", icon="swap_horiz").props("flat dense no-caps size=sm").classes("text-xs")
            retarget.set_visibility(False)
            ui.element("div").classes("flex-1")
            # Maximize / back live in the chat's own header row (pb-dock-max).

        frame_host = ui.element("div").classes("pbm-dock-frame")

    def _ctx_of_selection():
        kind, key = st["sel"]
        if kind == "dossier":
            return ("dossier", key)
        if kind == "finding":
            # Always the finding itself (its dossier comes along, `_src`):
            # with the dossier as the context, clicking through its findings
            # kept one conversation that knew none of them.
            return ("finding", key)
        return None

    def _src(ctx) -> str:
        if ctx is None:
            return "/chat?embed=1"
        if ctx[0] == "dossier":
            d = dmod.by_id(user, ctx[1])
            q = f"/chat?embed=1&dossier={quote(ctx[1])}"
            if d is not None and d.expert:  # else the chat takes the default expert
                q += f"&expert={quote(d.expert)}"
            return q
        q = f"/chat?embed=1&finding={quote(ctx[1])}"
        f = findings.get(user, ctx[1])
        d = next((x for x in dmod.list_dossiers(user) if x.topic == f.topic), None) if f and f.topic else None
        if d is not None:
            q += f"&dossier={quote(d.pbrain_id)}" + (f"&expert={quote(d.expert)}" if d.expert else "")
        return q

    def _dock_load(ctx=None, *, force: bool = False) -> None:
        if ctx is None and st["dock_ctx"] is None:
            ctx = _ctx_of_selection()
        if ctx is None and st["dock_ctx"] is not None and not force:
            return
        if ctx is not None and ctx == st["dock_ctx"] and not force and frame_host.default_slot.children:
            return
        st["dock_ctx"] = ctx
        frame_host.clear()
        with frame_host:
            ui.html(f'<iframe src="{_esc(_src(ctx))}" allow="microphone" title="chat"></iframe>', sanitize=False)
        _dock_suggest()

    async def _dock_follow() -> None:
        """The chat follows the selection — another dossier opens that
        dossier's chat — unless it is busy: an answer is running or a message
        is half-typed. Then it stays and offers "Talk about the selection"."""
        want = _ctx_of_selection()
        if want is None or want == st["dock_ctx"] or not frame_host.default_slot.children:
            _dock_suggest()
            return
        try:
            busy = await ui.run_javascript(
                "(function(){var f=document.querySelector('.pbm-dock-frame iframe');"
                "var d=f&&f.contentDocument;if(!d)return false;"
                "var t=d.querySelector('.chat-main-input textarea');"
                "var b=d.querySelector('.chat-send-btn i');"
                "return !!((t&&t.value.trim())||(b&&b.textContent.trim()==='stop'));})()", timeout=2)
        except Exception:  # noqa: BLE001 — no answer: treat as busy, never drop input
            busy = True
        if busy:
            _dock_suggest()
        else:
            _dock_load(want, force=True)

    def _dock_suggest() -> None:
        """Busy or not loaded: the dock does not re-target on its own. It
        offers the switch when the selection is elsewhere."""
        want = _ctx_of_selection()
        # Also when the chat was opened on the overview, without a context.
        show = bool(frame_host.default_slot.children) and want is not None and want != st["dock_ctx"]
        retarget.set_visibility(show)
        # On a phone the head row exists only for this offer.
        dock_head.classes(add="has-retarget") if show else dock_head.classes(remove="has-retarget")
        if show:
            retarget.set_text(_("Talk about the selection"))

    def _dock_max(on: bool) -> None:
        st["dock_max"] = on
        root.classes(add="dock-max") if on else root.classes(remove="dock-max")
        if on:
            if not st["dock_open"]:
                _dock_open(True)
            if _is_mobile["v"] and st["zone"] != "chat":
                _zone("chat")
            if not frame_host.default_slot.children:
                _dock_load()  # never reload a frame that is there: it holds the input
        ui.run_javascript("window.pbmDockMode && window.pbmDockMode()")

    def _dock_open(on: bool) -> None:
        st["dock_open"] = True  # always open now; kept for the callers
        if not frame_host.default_slot.children:
            _dock_load()

    def _dock_ask(ctx) -> None:
        _dock_load(ctx, force=ctx != st["dock_ctx"])
        if st["zone"] != "chat" and _is_mobile["v"]:
            _zone("chat")
        elif not st["dock_open"]:
            _dock_open(True)

    _is_mobile = {"v": False}

    async def _detect() -> None:
        try:
            _is_mobile["v"] = bool(await ui.run_javascript("window.innerWidth < 1024", timeout=3))
        except Exception:
            pass
        if st["dock_open"] and not _is_mobile["v"]:
            _dock_load()

    retarget.on_click(lambda: _dock_load(_ctx_of_selection(), force=True))

    # ── Build ────────────────────────────────────────────────────────────────
    if finding:
        await _adopt_research_note(user, findings.get(user, finding))
    with nav_host:
        nav_body()
    with viewer:
        viewer_body()
    ui.timer(0.2, _detect, once=True)

    # A deep dive finishes, the worker writes a finding: refresh without a reload.
    def _key() -> tuple:
        items = findings.list_findings(user)
        return tuple(sorted((f.finding_id, f.status, f.research_note or "") for f in items)) + tuple(
            sorted((d.pbrain_id, d.sha) for d in dmod.list_dossiers(user)))

    def _sel_key() -> tuple:
        kind, key = st["sel"]
        if kind == "finding":
            f = findings.get(user, key)
            return (kind, key, f.status if f else "", f.research_run if f else "", f.research_note if f else "")
        if kind == "dossier":
            d = dmod.by_id(user, key)
            return (kind, key, d.sha if d else "")
        return (kind, key)

    seen = {"k": _key(), "sel": _sel_key()}

    def _watch() -> None:
        k = _key()
        if k == seen["k"]:
            return
        seen["k"] = k
        nav_body.refresh()
        # The viewer only when what it shows changed: a refresh would wipe a
        # note the user is typing.
        sk = _sel_key()
        moved = tuple(seen["sel"][:2]) != tuple(st["sel"])  # the user picked something else meanwhile
        if st["sel"][0] in ("home", "list") or (sk != seen["sel"] and not moved):
            viewer_body.refresh()
        seen["sel"] = sk

    ui.timer(20.0, _watch)


def _esc(s) -> str:
    import html

    return html.escape(str(s or ""))
