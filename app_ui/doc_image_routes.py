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
