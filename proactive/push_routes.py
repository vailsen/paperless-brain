"""`/api/push/subscribe`, `/api/push/unsubscribe` — a device turns Web Push on
or off for the signed-in user (Settings > Proactive). Registered by
`proactive.app.register()` only.

Same-origin only: the session cookie would let another site register *its*
endpoint for a signed-in user and read their notifications. A JSON body
already forces a CORS preflight that a foreign origin fails; the Origin check
is the explicit guard.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import JSONResponse
from nicegui import app as ng_app

log = logging.getLogger("proactive.push_routes")

SUBSCRIBE_PATH = "/api/push/subscribe"
UNSUBSCRIBE_PATH = "/api/push/unsubscribe"


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True  # same-origin GET/POST from some browsers omit it; JSON still preflights
    return urlparse(origin).netloc == request.headers.get("host", "")


def _user() -> str:
    try:
        return ng_app.storage.user.get("paperless_user", "")
    except Exception:
        return ""


async def _body(request: Request) -> dict | None:
    if "application/json" not in request.headers.get("content-type", ""):
        return None
    try:
        data = await request.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


@ng_app.post(SUBSCRIBE_PATH)
async def push_subscribe(request: Request) -> JSONResponse:
    from proactive.notify import webpush

    user = _user()
    data = await _body(request)
    if not _same_origin(request) or data is None:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not user:
        return JSONResponse({"error": "not signed in"}, status_code=401)
    try:
        webpush.subscribe(user, data.get("subscription") or {}, str(data.get("device") or ""))
    except webpush.SubscriptionRefused:
        return JSONResponse({"error": "not a push subscription"}, status_code=400)
    log.info("push device added for %s", user)
    return JSONResponse({"ok": True})


@ng_app.post(UNSUBSCRIBE_PATH)
async def push_unsubscribe(request: Request) -> JSONResponse:
    from proactive.notify import webpush

    user = _user()
    data = await _body(request)
    if not _same_origin(request) or data is None:
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not user:
        return JSONResponse({"error": "not signed in"}, status_code=401)
    webpush.unsubscribe(user, str(data.get("endpoint") or ""))
    return JSONResponse({"ok": True})
