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

from nicegui import app as ng_app
from nicegui import ui

from app_ui.layout import page_layout, require_auth
from i18n import N_, get_translator

STATUS_LABELS = {
    "new": N_("New"),
    "surfaced": N_("Sent"),
    "held": N_("Held back"),
    "interested": N_("Interesting"),
    "researched": N_("Researched"),
    "done": N_("Done"),
    "dismissed": N_("Dismissed"),
    "snoozed": N_("Snoozed"),
}
KIND_LABELS = {"deadline": N_("Deadline"), "insight": N_("Insight")}
OPEN_STATUSES = {"new", "surfaced", "held", "interested", "researched", "snoozed"}


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

    base = re.escape(settings.paperless_url.rstrip("/"))
    body = re.sub(base + r"/documents/(\d+)/details/?", r"#pbdoc-\1", body)
    # markdown2 does not autolink bare URLs; Obsidian does.
    return _BARE_URL.sub(lambda m: f"<{m.group(0)}>", body)


def _user() -> str:
    return str(ng_app.storage.user.get("paperless_user") or "")


def _meta_line(f) -> str:
    from proactive.notify import URGENCY_LABELS

    _ = get_translator()
    parts = [_(KIND_LABELS.get(f.kind, f.kind)), _(STATUS_LABELS.get(f.status, f.status))]
    parts.append(_("Urgency: {urgency}").format(urgency=_(URGENCY_LABELS.get(f.urgency, f.urgency))))
    if f.deadline:
        parts.append(_("Due {date}").format(date=f.deadline))
    if f.topic:
        parts.append(f.topic)
    return " · ".join(parts)


@ui.page("/findings")
async def findings_page() -> None:
    if not require_auth():
        return
    page_layout()
    _ = get_translator()
    from proactive import findings

    user = _user()
    state = {"status": "open", "topic": ""}

    with ui.column().classes("w-full max-w-3xl mx-auto p-4 gap-3"):
        ui.label(_("Findings")).classes("text-xl font-bold text-gray-100")

        @ui.refreshable
        def body() -> None:
            items = findings.list_findings(user)
            topics = sorted({f.topic for f in items if f.topic})
            with ui.row().classes("w-full gap-2 flex-nowrap"):
                status_opts = {"open": _("Open"), "all": _("All")}
                status_opts.update({k: _(v) for k, v in STATUS_LABELS.items()})

                def _on_status(e) -> None:
                    state["status"] = e.value
                    body.refresh()

                def _on_topic(e) -> None:
                    state["topic"] = e.value or ""
                    body.refresh()

                ui.select(status_opts, value=state["status"], label=_("Status"), on_change=_on_status).props(
                    "outlined dark dense"
                ).classes("flex-1 min-w-0")
                ui.select({"": _("All topics"), **{t: t for t in topics}}, value=state["topic"],
                          label=_("Topic"), on_change=_on_topic).props("outlined dark dense").classes("flex-1 min-w-0")

            def _keep(f) -> bool:
                st = state["status"]
                if st == "open" and f.status not in OPEN_STATUSES:
                    return False
                if st not in ("open", "all") and f.status != st:
                    return False
                return not state["topic"] or f.topic == state["topic"]

            shown = sorted(
                (f for f in items if _keep(f)),
                key=lambda f: (f.deadline or "9999", f.created),
            )
            if not shown:
                ui.label(_("Nothing here yet.")).classes("text-sm text-gray-500 mt-4")
            for f in shown:
                with ui.card().classes(
                    "w-full bg-gray-800 border border-gray-700 p-3 cursor-pointer min-w-0"
                ).on("click", lambda fid=f.finding_id: ui.navigate.to(f"/findings/{fid}")):
                    ui.label(f.title).classes("text-sm font-semibold text-gray-100 w-full truncate")
                    ui.label(_meta_line(f)).classes("text-xs text-gray-400 w-full")

        body()


@ui.page("/findings/{finding_id}")
async def finding_detail_page(finding_id: str, action: str = "") -> None:
    if not require_auth():
        return
    page_layout()
    _ = get_translator()
    from proactive import findings, reactions
    from proactive.notify import ACTION_LABELS
    from proactive.states import reactions_for

    user = _user()

    from app_ui.document_dialog import create_document_dialog
    from models.result_document import DocumentResult
    from services.clients import get_session_paperless

    open_document, _dlg = create_document_dialog()

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
        ui.button(_("All findings"), icon="arrow_back", on_click=lambda: ui.navigate.to("/findings")).props(
            "flat dense"
        )

        @ui.refreshable
        def body(pending_action: str) -> None:
            f = findings.get(user, finding_id)
            if f is None:
                ui.label(_("This item no longer exists.")).classes("text-sm text-gray-400")
                return
            ui.label(f.title).classes("text-xl font-bold text-gray-100 w-full break-words")
            ui.label(_meta_line(f)).classes("text-xs text-gray-400 w-full")
            if f.doc_ids:
                with ui.row().classes("gap-2 flex-wrap"):
                    for d in f.doc_ids:
                        ui.link(f"#{d}", f"#pbdoc-{d}").classes("text-xs")

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
            ui.markdown(_in_app_links(f.body)).classes("chat-md finding-md w-full min-w-0")

        body(action)
