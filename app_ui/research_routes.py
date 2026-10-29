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
from datetime import datetime
from pathlib import PurePosixPath
from urllib.parse import quote

from fastapi import Response
from nicegui import app as ng_app

from services.session_auth import get_session_token

log = logging.getLogger(__name__)


def pdf_url(rel: str) -> str:
    return "/api/research/pdf?path=" + quote(rel, safe="")


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
