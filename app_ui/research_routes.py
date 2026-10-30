"""GET /api/research/pdf?path=… — a research note from the user's vault as a PDF.

The finding page's "Full report" link points here: the report opens as a PDF,
made on demand from the note in `PaperlessBrain Research/` (`vault/research.py`),
not from the Werkbank run — the run may be long deleted. Only notes in that
folder of the signed-in user's own vault are served; anything else, no session
and no such note all answer 404.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from pathlib import PurePosixPath
from urllib.parse import quote

from fastapi import Response
from nicegui import app as ng_app

from services.session_auth import get_session_token

log = logging.getLogger(__name__)


def pdf_url(rel: str) -> str:
    return "/api/research/pdf?path=" + quote(rel, safe="")


def finding_pdf_url(finding_id: str) -> str:
    return "/api/findings/pdf?id=" + quote(finding_id, safe="")


def _absolute_links(body: str) -> str:
    """In a PDF only a full URL leads anywhere: a research note's Obsidian link
    becomes the link to its PDF, an in-app link gets the app's address."""
    from proactive.notify import app_link
    from vault.research import _WIKILINK, research_rel, wikilink_target

    root = research_rel().strip("/") + "/"

    def note(m) -> str:
        target, label = m.group(1), m.group(2) or m.group(1).rsplit("/", 1)[-1]
        if not target.startswith(root):
            return m.group(0)
        return f"[{label}]({app_link(pdf_url(wikilink_target(target)))})"

    body = _WIKILINK.sub(note, body)
    return re.sub(r"\]\((/[^)\s]*)\)", lambda m: f"]({app_link(m.group(1))})", body)


@ng_app.get("/api/findings/pdf")
async def finding_pdf(id: str = "") -> Response:  # noqa: A002 — the query parameter's name
    """The finding — its timeline of research results, notes and the original —
    as a PDF. Only from the signed-in user's own ledger (`findings.get`)."""
    try:
        user = str(ng_app.storage.user.get("paperless_user") or "")
    except Exception:  # no session context
        user = ""
    from proactive import findings

    f = findings.get(user, id) if user and id else None
    if f is None:
        return Response(status_code=404)
    from i18n import translator_for
    from proactive.notify import URGENCY_LABELS
    from vault import research

    _ = translator_for(f.lang)
    meta = [_("Urgency: {urgency}").format(urgency=_(URGENCY_LABELS.get(f.urgency, f.urgency)))]
    if f.topic:
        meta.append(f.topic_display)
    if f.doc_ids:
        meta.append(", ".join(f"#{d}" for d in f.doc_ids))
    body = "_" + " · ".join(meta) + "_\n\n" + findings.tidy_sources(findings.timeline_markdown(f), f.lang)
    body = _absolute_links(body)
    from services.pdf_generator import generate_chat_pdf

    try:
        pdf = await asyncio.to_thread(
            generate_chat_pdf, research.strip_wikilinks(body), f.title, user, _("Finding"), None,
        )
    except Exception:  # noqa: BLE001
        log.exception("finding PDF for %s failed", id)
        return Response(status_code=500)
    name = f"{(f.created or '')[:10]} {f.title}"[:120]
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(name)}.pdf",
            "Cache-Control": "private, no-store",
        },
    )


@ng_app.get("/api/research/pdf")
async def research_pdf(path: str = "") -> Response:
    try:
        user = str(ng_app.storage.user.get("paperless_user") or "")
        token = get_session_token()
    except Exception:  # no session context
        user, token = "", ""
    if not (user and token and path):
        return Response(status_code=404)
    from vault import research

    try:
        meta, body = await asyncio.to_thread(research.read_note, user, path)
    except Exception:  # noqa: BLE001 — outside the folder, missing, a traversal attempt
        return Response(status_code=404)
    stem = PurePosixPath(path).stem
    title = next((line[2:].strip() for line in body.splitlines() if line.startswith("# ")), stem)
    # The note's title is the PDF's title block; leave it out of the body.
    lines = body.splitlines()
    if lines and lines[0].startswith("# "):
        body = "\n".join(lines[1:])
    created = None
    try:
        created = datetime.fromisoformat(str(meta.get("created") or ""))
    except ValueError:
        pass
    from services.pdf_generator import generate_chat_pdf

    try:
        pdf = await asyncio.to_thread(
            generate_chat_pdf, research.strip_wikilinks(body), title, user, str(meta.get("model") or ""), created,
        )
    except Exception:  # noqa: BLE001
        log.exception("research PDF for %s failed", path)
        return Response(status_code=500)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            # inline: the browser's viewer, not a download
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(stem)}.pdf",
            "Cache-Control": "private, no-store",
        },
    )
