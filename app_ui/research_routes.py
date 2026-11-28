"""GET /api/research/pdf?path=… — a research note from the user's vault as a PDF
(and /api/research/docx as a Word document; the same for a finding, /api/findings/…).

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


def docx_url(rel: str) -> str:
    return "/api/research/docx?path=" + quote(rel, safe="")


def finding_docx_url(finding_id: str) -> str:
    return "/api/findings/docx?id=" + quote(finding_id, safe="")


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


def _user_and_token() -> tuple[str, str]:
    try:
        return str(ng_app.storage.user.get("paperless_user") or ""), get_session_token() or ""
    except Exception:  # no session context
        return "", ""


def _finding_doc(user: str, fid: str):
    """(title, body, file name stem, label) of a finding of ``user``, or None."""
    from proactive import findings

    f = findings.get(user, fid) if user and fid else None
    if f is None:
        return None
    from i18n import translator_for
    from proactive.notify import URGENCY_LABELS
    from vault import research

    _ = translator_for(f.lang)
    meta = [_("Urgency: {urgency}").format(urgency=_(URGENCY_LABELS.get(f.urgency, f.urgency)))]
    if f.topic:
        meta.append(f.topic_display)
    if f.doc_ids:
        meta.append(", ".join(f"#{d}" for d in f.doc_ids))
    body = "_" + " · ".join(meta) + "_\n\n" + findings.timeline_markdown(f)
    body = research.strip_wikilinks(_absolute_links(body))
    return f.title, body, f"{(f.created or '')[:10]} {f.title}"[:120], _("Finding")


async def _research_doc(user: str, path: str):
    """(title, body, file name stem, model, created) of a research note in
    ``user``'s own research folder, or None."""
    from vault import research

    try:
        meta, body = await asyncio.to_thread(research.read_note, user, path)
    except Exception:  # noqa: BLE001 — outside the folder, missing, a traversal attempt
        return None
    stem = PurePosixPath(path).stem
    title = next((line[2:].strip() for line in body.splitlines() if line.startswith("# ")), stem)
    # The note's title is the document's title block; leave it out of the body.
    lines = body.splitlines()
    if lines and lines[0].startswith("# "):
        body = "\n".join(lines[1:])
    created = None
    try:
        created = datetime.fromisoformat(str(meta.get("created") or ""))
    except ValueError:
        pass
    return title, research.strip_wikilinks(body), stem, str(meta.get("model") or ""), created


def _file_response(data: bytes, stem: str, *, docx: bool) -> Response:
    from services.docx_export import DOCX_MEDIA_TYPE

    return Response(
        content=data,
        media_type=DOCX_MEDIA_TYPE if docx else "application/pdf",
        headers={
            # A PDF opens in the browser's viewer; a Word file is a download.
            "Content-Disposition": f"{'attachment' if docx else 'inline'}; "
                                   f"filename*=UTF-8''{quote(stem)}.{'docx' if docx else 'pdf'}",
            "Cache-Control": "private, no-store",
        },
    )


def _lang() -> str:
    from werkbank.settings_store import get_archive_language

    try:
        return get_archive_language()
    except Exception:  # noqa: BLE001
        return "en"


@ng_app.get("/api/findings/pdf")
async def finding_pdf(id: str = "") -> Response:  # noqa: A002 — the query parameter's name
    """The finding — its timeline of research results, notes and the original —
    as a PDF. Only from the signed-in user's own ledger (`findings.get`)."""
    user, _token = _user_and_token()
    found = _finding_doc(user, id)
    if found is None:
        return Response(status_code=404)
    title, body, stem, label = found
    from services.pdf_generator import generate_chat_pdf

    try:
        pdf = await asyncio.to_thread(generate_chat_pdf, body, title, user, label, None)
    except Exception:  # noqa: BLE001
        log.exception("finding PDF for %s failed", id)
        return Response(status_code=500)
    return _file_response(pdf, stem, docx=False)


@ng_app.get("/api/findings/docx")
async def finding_docx(id: str = "") -> Response:  # noqa: A002
    """The finding as a Word document — the same content as its PDF."""
    user, _token = _user_and_token()
    found = _finding_doc(user, id)
    if found is None:
        return Response(status_code=404)
    title, body, stem, label = found
    from services.docx_export import markdown_to_docx

    try:
        data = await asyncio.to_thread(markdown_to_docx, body, title, username=user, model_name=label,
                                       dt=datetime.now(), lang=_lang())
    except Exception:  # noqa: BLE001
        log.exception("finding DOCX for %s failed", id)
        return Response(status_code=500)
    return _file_response(data, stem, docx=True)


@ng_app.get("/api/research/pdf")
async def research_pdf(path: str = "") -> Response:
    user, token = _user_and_token()
    if not (user and token and path):
        return Response(status_code=404)
    found = await _research_doc(user, path)
    if found is None:
        return Response(status_code=404)
    title, body, stem, model, created = found
    from services.pdf_generator import generate_chat_pdf

    try:
        pdf = await asyncio.to_thread(generate_chat_pdf, body, title, user, model, created)
    except Exception:  # noqa: BLE001
        log.exception("research PDF for %s failed", path)
        return Response(status_code=500)
    return _file_response(pdf, stem, docx=False)


@ng_app.get("/api/research/docx")
async def research_docx(path: str = "") -> Response:
    """A research note as a Word document — the same content as its PDF."""
    user, token = _user_and_token()
    if not (user and token and path):
        return Response(status_code=404)
    found = await _research_doc(user, path)
    if found is None:
        return Response(status_code=404)
    title, body, stem, model, created = found
    from services.docx_export import markdown_to_docx

    try:
        data = await asyncio.to_thread(markdown_to_docx, body, title, username=user, model_name=model,
                                       dt=created, lang=_lang())
    except Exception:  # noqa: BLE001
        log.exception("research DOCX for %s failed", path)
        return Response(status_code=500)
    return _file_response(data, stem, docx=True)
