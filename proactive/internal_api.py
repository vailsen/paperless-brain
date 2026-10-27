"""App side of the internal proactive API — work that needs Chroma or the chat
tools, which live in the app process and are not loaded by the worker.

Registered by `proactive.app.register()` only with `PROACTIVE_ENABLED=true`.
Every request is HMAC-signed (`proactive.internal_auth`); it names a user, and
the token is looked up here from that user's background-access grant — no
grant, no work. Everything then runs with the user's own Paperless access.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request
from nicegui import app

from proactive.internal_auth import SIG_HEADER, TS_HEADER, verify

log = logging.getLogger("proactive.internal")


async def _authorized(request: Request) -> dict:
    body = await request.body()
    if not verify(body, request.headers.get(TS_HEADER), request.headers.get(SIG_HEADER)):
        raise HTTPException(status_code=401, detail="bad signature")
    import json

    try:
        data = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="bad json") from None
    return data


def _token(user: str) -> str:
    from services import background_access

    tok = background_access.token_for(user or "")
    if not tok:
        raise HTTPException(status_code=403, detail="no background access")
    return tok


@app.post("/api/internal/proactive/similar")
async def _similar(request: Request) -> dict:
    data = await _authorized(request)
    user, doc_id, k = data.get("user", ""), int(data.get("doc_id", 0)), min(int(data.get("k", 5)), 20)
    tok = _token(user)
    from config.settings import settings
    from pipelines.similar import find_similar_documents
    from services.paperless import PaperlessClient

    client = PaperlessClient(settings.paperless_url, tok)
    # The source document first: its neighbours say something about it.
    if not await client.list_documents(ids=[doc_id]):
        return {"docs": []}
    hits = await find_similar_documents(doc_id, n_results=k, paperless_client=client)
    return {"docs": [{"doc_id": h.document.id, "title": h.document.title} for h in hits]}


@app.post("/api/internal/proactive/deep_dive")
async def _deep_dive(request: Request) -> dict:
    data = await _authorized(request)
    user = data.get("user", "")
    tok = _token(user)
    from proactive import findings
    from proactive.deep_dive_app import run
    from proactive.models import ResolvedModel

    f = findings.get(user, str(data.get("finding_id", "")))
    if f is None:
        raise HTTPException(status_code=404, detail="no such finding")
    m = data.get("model") or {}
    model = ResolvedModel(m.get("name", ""), m.get("lane", "local"), m.get("source", "default"), m.get("model_id", ""))
    if not model.name:
        raise HTTPException(status_code=400, detail="no model")
    return await run(user, tok, f, model)


@app.post("/api/internal/proactive/dossier")
async def _dossier(request: Request) -> dict:
    data = await _authorized(request)
    user = data.get("user", "")
    tok = _token(user)
    from proactive.deep_dive_app import dossier
    from proactive.models import ResolvedModel

    m = data.get("model") or {}
    model = ResolvedModel(m.get("name", ""), m.get("lane", "local"), m.get("source", "default"), m.get("model_id", ""))
    if not model.name:
        raise HTTPException(status_code=400, detail="no model")
    return await dossier(
        user, tok, str(data.get("topic", "")), int(data.get("seed_doc_id") or 0), data.get("existing"), model,
    )


@app.post("/api/internal/proactive/vault_sync")
async def _vault_sync(request: Request) -> dict:
    """The worker's findings, committed and indexed after its batch
    (`proactive.tasks.vault_sync`). The same sync a chat turn runs, under the
    same per-user lock — no Paperless token involved, so no grant needed."""
    data = await _authorized(request)
    user = str(data.get("user") or "")
    if not user:
        raise HTTPException(status_code=400, detail="no user")
    from vault.sync import sync_user

    await sync_user(user, force=True)
    return {"ok": True}
