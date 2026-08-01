# pipelines/text_push.py
"""Push the vision-extracted text back into Paperless-ngx.

Paperless stores whatever its OCR engine produced. The vision model reads the
same pages far more accurately on scans, tables and poor originals, so the text
in our sidecars is usually the better one — but it only lives here, which means
Paperless' own full-text search keeps hitting the weaker OCR.

This step closes that gap: for every document whose sidecar text differs from
what Paperless has, PATCH the sidecar text in. Opt-in per user, because it is
destructive — Paperless keeps no history of the replaced text.

Idempotent by construction: after a push both sides hold the same string, so the
next run finds no difference and does nothing. Paperless re-running OCR
(reprocess, rotate, split, merge) puts its own text back, and the next sync
pushes ours again.
"""

from __future__ import annotations

import re
from typing import Callable

from services.paperless import PaperlessClient
from services.sidecar_service import SidecarService

#: Below this many characters the vision text is treated as a failed extraction
#: (empty page, model refusal) and never overwrites what Paperless has.
MIN_TEXT_LENGTH = 40

_WS = re.compile(r"\s+")


def _normalized(text: str) -> str:
    """Whitespace-insensitive form used only for the "did it change?" test.

    Paperless and the vision model wrap lines differently for text that is
    otherwise identical; comparing raw strings would push every document on
    every sync.
    """
    return _WS.sub(" ", (text or "")).strip()


def find_text_diffs(
    paperless_docs: list[dict],
    sidecar_service: SidecarService,
) -> list[int]:
    """IDs of documents whose sidecar text differs from the Paperless content.

    ``paperless_docs`` are raw API dicts carrying at least ``id`` and
    ``content`` (see :meth:`PaperlessClient.list_specific_fields`).
    """
    out: list[int] = []
    for raw in paperless_docs:
        doc_id = raw.get("id")
        if doc_id is None:
            continue
        sidecar = sidecar_service.load_sidecar(int(doc_id))
        if not sidecar:
            continue  # not ingested yet — nothing of ours to push
        vision_text = sidecar.get("full_text") or ""
        if len(vision_text.strip()) < MIN_TEXT_LENGTH:
            continue
        if _normalized(vision_text) == _normalized(raw.get("content") or ""):
            continue
        out.append(int(doc_id))
    return out


async def push_vision_texts(
    paperless: PaperlessClient,
    sidecar_service: SidecarService,
    log: Callable[[str], None] | None = None,
) -> tuple[int, int]:
    """Push differing sidecar texts into Paperless. Returns (pushed, failed).

    ``paperless`` must be the client of the user who enabled the option: the
    PATCH needs write permission on each document, and running it as superuser
    would edit documents the user cannot even see.
    """

    def _log(msg: str) -> None:
        if log is not None:
            log(msg)

    raw_docs = await paperless.list_specific_fields(fields=["id", "content"])
    candidates = find_text_diffs(raw_docs, sidecar_service)
    if not candidates:
        _log("Text comparison: no differences")
        return 0, 0

    _log(f"Text comparison: {len(candidates)} document(s) differ")
    pushed = failed = 0
    for doc_id in candidates:
        sidecar = sidecar_service.load_sidecar(doc_id) or {}
        try:
            await paperless.update_document_content(doc_id, sidecar.get("full_text", ""))
            pushed += 1
            _log(f"  ✓ #{doc_id} text pushed")
        except Exception as exc:  # noqa: BLE001 — one bad document must not stop the run
            failed += 1
            _log(f"  ✗ #{doc_id}: {exc}")
    return pushed, failed
