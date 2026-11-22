"""GET /api/doc-image/{doc_id}/{page}[?i=n] — a document page, or one picture on it.

What the chat's ``show_document_image`` tool points the user's browser at. The
access check and the cache live in ``services.doc_image_service``; this only
reads the session. No session, no access and no such page all answer 404 — a
403 would confirm that the document exists.
"""

from fastapi import Response
from nicegui import app as ng_app

from services import doc_image_service
from services.session_auth import get_session_token


def _not_found() -> Response:
    return Response(status_code=404)


@ng_app.get("/api/doc-image/{doc_id}/{page}")
async def doc_image(doc_id: int, page: int, i: int | None = None):
    try:
        token = get_session_token()
    except Exception:  # no session context
        token = ""
    if not token:
        return _not_found()
    data = await doc_image_service.get_image(doc_id, page, i, token)
    if data is None:
        return _not_found()
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=86400"},
    )


# ── Text layer ───────────────────────────────────────────────────────────────
# GET /api/doc-words/{doc_id}/{page} — the words of a page with their boxes,
# from the PDF's own text layer (`citations.pdf_words`), behind the same access
# check as the page image. The page is shown as a picture; the browser lays
# these words over it, invisible, so text can be selected and copied
# (`page_viewer.TEXT_LAYER_JS`). A scan without a text layer has none.

_WORDS_CACHE: dict[tuple[int, int], list] = {}
_WORDS_MAX = 64


@ng_app.get("/api/doc-words/{doc_id}/{page}")
async def doc_words(doc_id: int, page: int):
    import asyncio

    from fastapi.responses import JSONResponse

    from services.citations import pdf_words

    try:
        token = get_session_token()
    except Exception:  # no session context
        token = ""
    if not token:
        return _not_found()
    src = await doc_image_service.fetch_source(doc_id, token)  # the access check, every time
    if src is None:
        return _not_found()
    key = (doc_id, page)
    words = _WORDS_CACHE.get(key)
    if words is None:
        _doc, pdf_bytes = src

        def _read() -> list:
            with doc_image_service.pdfium_lock:
                return [[w.text, *(round(v, 5) for v in w.box)] for w in pdf_words(pdf_bytes, page)]

        try:
            words = await asyncio.to_thread(_read) if pdf_bytes else []
        except Exception:  # noqa: BLE001 — unreadable text layer: the picture alone
            words = []
        if len(_WORDS_CACHE) >= _WORDS_MAX:
            _WORDS_CACHE.pop(next(iter(_WORDS_CACHE)))
        _WORDS_CACHE[key] = words
    return JSONResponse({"words": words}, headers={"Cache-Control": "private, max-age=86400"})


# ── Original file ────────────────────────────────────────────────────────────
# GET /api/doc-download/{doc_id} — the original file, as the download button in
# a chat answer's source box. Same access check as the page image (the user's
# own token, one `list_documents` lookup); the file is fetched with that token
# too, so Paperless enforces access a second time.


@ng_app.get("/api/doc-download/{doc_id}")
async def doc_download(doc_id: int):
    from urllib.parse import quote

    try:
        token = get_session_token()
    except Exception:  # no session context
        token = ""
    found = await doc_image_service._visible_doc(doc_id, token)
    if found is None:
        return _not_found()
    client, _doc = found
    try:
        data, filename = await client.download_document_named(doc_id)
    except Exception:  # noqa: BLE001 — gone or forbidden: the same 404
        return _not_found()
    ascii_name = filename.encode("ascii", "replace").decode().replace('"', "_").replace("?", "_")
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}",
            "Cache-Control": "private, no-store",
        },
    )
