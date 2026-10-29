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
# Web sources open in a new tab — in the installed PWA a plain link would
# replace the app.
_DOC_CLICK_JS = (
    "<script>document.addEventListener('click',function(e){"
    "var a=e.target.closest&&e.target.closest('.finding-md a[href]');if(!a)return;"
    "var h=a.getAttribute('href');"
    "if(h.indexOf('#pbdoc-')===0){e.preventDefault();"
    "emitEvent('" + _DOC_EVENT + "',parseInt(h.slice(7),10));}"
    "else if(/^https?:/.test(h)){e.preventDefault();window.open(h,'_blank','noopener');}"
    "});</script>"
)
# A bare URL (the "Sources" list) — not one already inside a Markdown link,
# an <autolink> or an attribute. Trailing punctuation is not part of it.
_BARE_URL = re.compile(r"(?<![(<\[\"'=])\bhttps?://[^\s<>()\[\]]+[^\s<>()\[\].,;:!?'\"]")


def _in_app_links(body: str) -> str:
    """`…/documents/241/details` (Paperless) → `#pbdoc-241` (the dialog)."""
    from config.settings import settings

    from config.proactive import proactive_settings

    base = re.escape(settings.paperless_url.rstrip("/"))
    body = re.sub(base + r"/documents/(\d+)/details/?", r"#pbdoc-\1", body)
    # Links into the app itself (the deep dive's report) stay in the app.
    app = proactive_settings().proactive_app_base_url.rstrip("/")
    if app:
        body = re.sub(re.escape(app) + r"(?=/)", "", body)
    # markdown2 does not autolink bare URLs; Obsidian does.
    return _BARE_URL.sub(lambda m: f"<{m.group(0)}>", body)


def _user() -> str:
    return str(ng_app.storage.user.get("paperless_user") or "")


def _meta_line(f, *, topic: bool = True, status: bool = True) -> str:
    """Kind · status · urgency · due · topic. The list leaves out what its
    heading already says, and the status, which its icon shows."""
    from proactive.notify import URGENCY_LABELS

    _ = get_translator()
    parts = [_(KIND_LABELS.get(f.kind, f.kind))]
    if status:
        parts.append(_(STATUS_LABELS.get(f.status, f.status)))
    if f.status == "snoozed" and f.snoozed_until:
        parts.append(_("until {date}").format(date=f.snoozed_until))
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
.pb-finding-card.is-closed{opacity:.6}
.pb-finding-card .pb-status-icon{color:var(--c-text-2)}
.pb-finding-head{color:var(--c-text);border-bottom:1px solid var(--c-border)}
.pb-finding-sub{color:var(--c-text-2)}
.pb-finding-count{color:var(--c-text-muted);font-weight:400}
"""


def _status_icon(status: str, size: str = "1.25rem"):
    _ = get_translator()
    icon = ui.icon(STATUS_ICONS.get(status, "circle")).classes("pb-status-icon flex-shrink-0").style(
        f"font-size:{size}")
    icon.tooltip(_(STATUS_LABELS.get(status, status)))
    return icon


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
                status_opts = {"open": _("Open"), "all": _("All statuses")}
                status_opts.update({k: _(fv.STATUS_LABELS[k]) for k in fv.STATUS_ORDER})
                ui.select(status_opts, value=state["status"], label=_("Status"),
                          on_change=lambda e: _set("status", e.value)).props(
                    "outlined dark dense options-dense").classes("min-w-0")
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

            c = fv.counts(items, today)
            if c["open"]:
                with ui.row().classes("w-full items-center gap-x-4 gap-y-1 text-xs").style(
                        "color:var(--c-text-2)"):
                    _summary_item("inbox", _("{n} open").format(n=c["open"]))  # TODO i18n-plural
                    if c["waiting"]:
                        _summary_item("notifications_active", _("{n} waiting for you").format(n=c["waiting"]))
                    if c["pressing"]:
                        _summary_item("priority_high", _("{n} pressing").format(n=c["pressing"]), warn=True)
                    if c["researching"]:
                        _summary_item("travel_explore", _("{n} being researched").format(n=c["researching"]))

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

    cls = "pb-finding-card w-full p-3 min-w-0"
    if fv.is_pressing(f, today):
        cls += " is-pressing"
    if f.status in fv.CLOSED_STATUSES:
        cls += " is-closed"
    with ui.row().classes(cls + " items-start gap-3 no-wrap").on(
            "click", lambda fid=f.finding_id: ui.navigate.to(f"/findings/{fid}")):
        _status_icon(f.status)
        with ui.column().classes("gap-0 flex-1 min-w-0"):
            ui.label(f.title).classes("text-sm font-semibold text-gray-100 w-full truncate")
            ui.label(_meta_line(f, topic=topic, status=status)).classes("text-xs text-gray-400 w-full")


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
                ui.label(_meta_line(f)).classes("text-xs text-gray-400 min-w-0 flex-1")
            if f.doc_ids:
                with ui.row().classes("gap-2 flex-wrap"):
                    for d in f.doc_ids:
                        ui.link(f"#{d}", f"#pbdoc-{d}").classes("text-xs")

            if f.research_run and f.status in ("interested", "researched"):
                running = f.status == "interested"
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    if running:
                        ui.spinner(size="sm").props("color=primary")
                    ui.label(
                        _("Research running — it gives way while you chat.") if running
                        else _("Researched — the result is at the top.")
                    ).classes("text-xs text-gray-400 flex-1 min-w-0")
                    ui.button(_("Open in deep research"), icon="auto_awesome",
                              on_click=lambda r=f.research_run: ui.navigate.to(f"/werkbank?run={quote(r)}")).props(
                        "flat dense no-caps"
                    ).classes("text-xs flex-shrink-0")
            elif f.status == "interested" and _jobs.is_pending(f"deep_dive:{f.finding_id}"):
                ui.label(_("Research queued — it starts as soon as the model is free.")).classes(
                    "text-xs text-gray-400")

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
                        ui.button(_(ACTION_LABELS[a]), on_click=lambda a=a: _do(a)).props("flat dense")
            ui.separator()
            ui.add_css(
                ".finding-md h2{font-size:1rem;font-weight:600;margin:1rem 0 .4rem}"
                ".finding-md{overflow-x:auto}"
                ".finding-md a{overflow-wrap:anywhere}"
            )
            ui.markdown(_in_app_links(findings.tidy_sources(f.body, f.lang))).classes("chat-md finding-md w-full min-w-0")

        body(action)

        # A deep dive finishes while the page is open: show the result without a reload.
        def _key(f) -> tuple:
            return (f.status, f.research_run) if f else ()

        seen = {"key": _key(findings.get(user, finding_id))}

        def _watch() -> None:
            k = _key(findings.get(user, finding_id))
            if k and k != seen["key"]:
                seen["key"] = k
                body.refresh("")

        ui.timer(15.0, _watch)
