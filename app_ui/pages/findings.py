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


def _in_app_links(body: str, user: str = "") -> str:
    """`…/documents/241/details` (Paperless) → `#pbdoc-241` (the dialog).
    With `user`, links to a research the user deleted (its run, its note)
    are shown as deleted instead of leading to "not found"."""
    from config.settings import settings

    from config.proactive import proactive_settings

    base = re.escape(settings.paperless_url.rstrip("/"))
    body = re.sub(base + r"/documents/(\d+)/details/?", r"#pbdoc-\1", body)
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
.pb-finding-card.has-topic{border-color:var(--card-topic)}
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


@ui.page("/findings")
async def findings_page() -> None:
    if not require_auth():
        return
    page_layout()
    _ = get_translator()
    from proactive import findings

    from datetime import date

    from app_ui import findings_view as fv
    from services.user_prefs import get_pref, set_pref

    user = _user()
    # The view is remembered per user: whoever groups by topic once wants it next time.
    stored = get_pref("findings_view") or {}
    state = {k: stored.get(k, v) for k, v in fv.DEFAULT_VIEW.items()}
    ui.add_css(_LIST_CSS)

    with ui.column().classes("w-full max-w-3xl mx-auto p-4 gap-3"):
        ui.label(_("Findings")).classes("text-xl font-bold text-gray-100")

        def _set(key: str, value) -> None:
            state[key] = value or ""
            set_pref("findings_view", dict(state))
            body.refresh()

        @ui.refreshable
        def body() -> None:
            items = findings.list_findings(user)
            today = date.today()
            # key → label; a finding with a label wins over one without
            topics: dict[str, str] = {}
            for f in sorted(items, key=lambda f: bool(f.topic_label)):
                if f.topic:
                    topics[f.topic] = f.topic_display
            if state["topic"] and state["topic"] not in topics:
                state["topic"] = ""  # the topic's last finding is gone
            # Two selects a row: four in one squeeze each to a word on a phone.
            with ui.grid(columns=2).classes("w-full gap-2"):
                _status_select(state["status"], lambda e: _set("status", e.value))
                ui.select({"": _("All topics"), **dict(sorted(topics.items(), key=lambda kv: kv[1].lower()))},
                          value=state["topic"], label=_("Topic"),
                          on_change=lambda e: _set("topic", e.value)).props(
                    "outlined dark dense options-dense").classes("min-w-0")
                groups = dict(fv.GROUPS)
                if state["topic"]:  # one topic: grouping by topic is one heading
                    groups.pop("topic"), groups.pop("topic_status")
                group = fv.effective_group(state["group"], state["topic"])
                ui.select({k: _(v) for k, v in groups.items()}, value=group, label=_("Group"),
                          on_change=lambda e: _set("group", e.value)).props(
                    "outlined dark dense options-dense").classes("min-w-0")
                ui.select({k: _(v) for k, v in fv.SORTS.items()}, value=state["sort"], label=_("Sort"),
                          on_change=lambda e: _set("sort", e.value)).props(
                    "outlined dark dense options-dense").classes("min-w-0")

            with ui.row().classes("w-full justify-end -mt-1"):
                ui.button(_("Topic colours"), icon="palette", on_click=lambda: _topic_colors_dialog(
                    topics, body.refresh)).props("flat dense no-caps").classes("text-xs")

            c = fv.counts(items, today)
            if c["open"] or c["relevant"]:
                with ui.row().classes("w-full items-center gap-x-4 gap-y-1 text-xs").style(
                        "color:var(--c-text-2)"):
                    _summary_item("inbox", _("{n} open").format(n=c["open"]))  # TODO i18n-plural
                    if c["relevant"]:
                        _summary_item("bookmark", _("{n} relevant").format(n=c["relevant"]))  # TODO i18n-plural
                    if c["pressing"]:
                        _summary_item("priority_high", _("{n} pressing").format(n=c["pressing"]), warn=True)
                    if c["researching"]:
                        _summary_item("travel_explore", _("{n} being researched").format(n=c["researching"]))

            _overview(items, today, state, _set)

            arranged = fv.arrange(items, status=state["status"], topic=state["topic"],
                                  group=state["group"], sort=state["sort"], today=today)
            if not arranged:
                ui.label(_("Nothing here yet.")).classes("text-sm text-gray-500 mt-4")
            single_status = state["status"] not in ("open", "all")
            for g in arranged:
                if g.kind != "none":
                    _heading(g, top=True)
                for sub in g.subgroups:
                    _heading(sub, top=False)
                    for f in sub.items:
                        _card(f, today, topic=False, status=False)
                for f in g.items:
                    _card(f, today, topic=g.kind != "topic" and not state["topic"],
                          status=g.kind != "status" and not single_status)

        body()


def _overview(items: list, today, state: dict, set_state) -> None:
    """Per topic: what waits and what calls for action (`app_ui/findings_chart`).
    Collapsible, and remembered with the rest of the view."""
    from app_ui import findings_chart
    from app_ui.theme import current_theme

    _ = get_translator()
    data = findings_chart.rows(items, today)
    if not data:
        return
    with ui.expansion(_("Overview"), icon="bar_chart", value=state.get("chart", True),
                      on_value_change=lambda e: set_state("chart", bool(e.value))).classes(
            "w-full pb-overview").props("dense"):
        ui.label(_("Per topic: right of the line, action is recommended — high or critical urgency, "
                   "or a deadline within a week.")).classes("text-xs w-full").style("color:var(--c-text-muted)")
        labels = {"calm": _("In view"), "act": _("Action recommended"), "other": _("Other")}
        ui.echart(findings_chart.options(data, current_theme(), labels)).classes("w-full").style(
            f"height:{findings_chart.height_px(data)}px")


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


def _card(f, today, *, topic: bool, status: bool) -> None:
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
            "click", lambda fid=f.finding_id: ui.navigate.to(f"/findings/{fid}")):
        _status_icon(f.status)
        with ui.column().classes("gap-0 flex-1 min-w-0"):
            ui.label(f.title).classes("text-sm font-semibold text-gray-100 w-full truncate")
            ui.label(_meta_line(f, topic=topic, status=status)).classes("text-xs text-gray-400 w-full")
        with ui.row().classes("items-center gap-1 no-wrap flex-shrink-0"):
            _hints(f)


@ui.page("/findings/{finding_id}")
async def finding_detail_page(finding_id: str, action: str = "") -> None:
    if not require_auth():
        return
    page_layout()
    _ = get_translator()
    from proactive import findings, reactions
    from proactive import jobs as _jobs
    from proactive.notify import ACTION_LABELS
    from proactive.states import reactions_for

    user = _user()

    from app_ui.document_dialog import create_document_dialog
    from models.result_document import DocumentResult
    from services.clients import get_session_paperless

    from services.user_prefs import get_pref, set_pref

    def _pinned_ids() -> set[int]:
        return set(get_pref("pinned_doc_ids") or [])

    def _pin(result: DocumentResult) -> None:
        # Same prefs as the browser and the chat: a pin here shows up in the chat.
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

    async def _open_doc(e) -> None:
        try:
            # The user's own token: a finding names only their documents, but
            # the dialog must not become a way round Paperless' permissions.
            doc = await get_session_paperless().get_document(int(e.args))
        except Exception:
            ui.notify(_("#{id} not found").format(id=e.args), type="warning")
            return
        await open_document(DocumentResult(document=doc))

    ui.add_body_html(_DOC_CLICK_JS)
    ui.on(_DOC_EVENT, _open_doc)

    with ui.column().classes("w-full max-w-3xl mx-auto p-4 gap-3"):
        with ui.row().classes("w-full items-center justify-between gap-2"):
            ui.button(_("All findings"), icon="arrow_back", on_click=lambda: ui.navigate.to("/findings")).props(
                "flat dense"
            )
            with ui.row().classes("items-center gap-1 no-wrap"):
                from app_ui.research_routes import finding_pdf_url

                # The whole finding — research results, notes, the original —
                # as one PDF. The full report stays a link in its entry.
                ui.button(icon="picture_as_pdf",
                          on_click=lambda: ui.navigate.to(finding_pdf_url(finding_id), new_tab=True)).props(
                    "flat dense round").classes("card-action-btn").tooltip(_("Finding as PDF"))
                # A new conversation with this finding as context and its documents pinned.
                ui.button(_("Chat about this"), icon="forum",
                          on_click=lambda: ui.navigate.to(f"/chat?finding={quote(finding_id)}")).props(
                    "unelevated dense no-caps color=primary"
                )

        @ui.refreshable
        def body(pending_action: str) -> None:
            f = findings.get(user, finding_id)
            if f is None:
                ui.label(_("This item no longer exists.")).classes("text-sm text-gray-400")
                return
            ui.label(f.title).classes("text-xl font-bold text-gray-100 w-full break-words")
            with ui.row().classes("w-full items-center gap-2 no-wrap"):
                _status_icon(f.status, "1.1rem")
                _hints(f)
                ui.label(_meta_line(f)).classes("text-xs text-gray-400 min-w-0 flex-1")
            if f.doc_ids:
                with ui.row().classes("gap-2 flex-wrap"):
                    for d in f.doc_ids:
                        ui.link(f"#{d}", f"#pbdoc-{d}").classes("text-xs")

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
                    # Nothing works on it: its run failed, was deleted, or never started.
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
                await findings.update(user, finding_id, lambda x: (
                    {"research_run": None, "research_note": None} if x.status == "interested" else None))
                _jobs.enqueue("deep_dive", user, {"finding_id": finding_id}, dedupe_key=f"deep_dive:{finding_id}")
                ui.notify(_("Research queued — it starts as soon as the model is free."), type="positive")
                body.refresh("")

            def _note_box(f) -> None:
                """A note of the user's own, dated, on top of the timeline."""
                with ui.expansion(_("Add a note"), icon="edit_note").classes("w-full").props("dense"):
                    box = ui.textarea(placeholder=_("e.g. The operation took place in April 2024.")).props(
                        "outlined dense autogrow").classes("w-full")

                    async def _save() -> None:
                        if not (box.value or "").strip():
                            return
                        await findings.add_note(user, finding_id, box.value)
                        ui.notify(_("Note added."), type="positive")
                        body.refresh("")

                    with ui.row().classes("w-full justify-end"):
                        ui.button(_("Save note"), on_click=_save).props("unelevated dense no-caps color=primary")

            allowed = reactions_for(f.status, f.kind)

            async def _do(a: str) -> None:
                msg = await reactions.react(user, finding_id, a, channel="app")
                ui.notify(msg, type="positive")
                body.refresh("")

            if pending_action and pending_action in allowed:
                with ui.card().classes("w-full bg-gray-900 border border-gray-700 p-3"):
                    ui.label(_("Apply “{action}” to this item?").format(action=_(ACTION_LABELS[pending_action]))).classes(
                        "text-sm text-gray-200"
                    )
                    ui.button(_("Confirm"), on_click=lambda a=pending_action: _do(a)).props(
                        "unelevated dense color=primary"
                    )
            elif pending_action:
                ui.label(_("Already handled.")).classes("text-xs text-gray-500")

            if allowed:
                with ui.row().classes("gap-2 flex-wrap"):
                    for a in allowed:
                        if a == "interested":
                            # Research is an action, not a status any more.
                            label = _("Research again") if (f.status == "researched" or f.research_note) \
                                else _("Start research")
                        else:
                            label = _(ACTION_LABELS[a])
                        ui.button(label, on_click=lambda a=a: _do(a)).props("flat dense")

            _note_box(f)
            ui.separator()
            ui.add_css(
                ".finding-md h2{font-size:1rem;font-weight:600;margin:1rem 0 .4rem}"
                # The research result's sections, one level below its heading.
                ".finding-md h3{font-size:.9rem;font-weight:600;margin:.8rem 0 .3rem}"
                ".finding-md{overflow-x:auto}"
                ".finding-md a{overflow-wrap:anywhere}"
                # Timeline: research results, notes and the original finding,
                # newest first — one dated entry each, so two summaries that
                # disagree read as then and now.
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
            items = [(e.split("\n", 1)[0][3:].strip(), e.split("\n", 1)[1] if "\n" in e else "") for e in entries]
            items.append((findings.original_heading(f), original))
            with ui.element("div").classes("pb-tl w-full min-w-0"):
                for head, text in items:
                    with ui.element("div").classes("pb-tl-item w-full min-w-0"):
                        ui.label(head).classes("pb-tl-head")
                        ui.markdown(_in_app_links(text.strip(), user)).classes("chat-md finding-md w-full min-w-0")

        await _adopt_research_note(user, findings.get(user, finding_id))
        body(action)

        # A deep dive finishes while the page is open: show the result without a reload.
        def _key(f) -> tuple:
            return (f.status, f.research_run, f.research_note) if f else ()

        seen = {"key": _key(findings.get(user, finding_id))}

        def _watch() -> None:
            k = _key(findings.get(user, finding_id))
            if k and k != seen["key"]:
                seen["key"] = k
                body.refresh("")

        ui.timer(15.0, _watch)
