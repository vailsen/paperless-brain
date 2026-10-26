"""Loop 1 — documents (architecture §5): new documents and the backlog sweep.

New documents: poll Paperless with `id__gt={cursor}` (superuser token, ids
only) every `PROACTIVE_NEWDOC_POLL_SECONDS`. The first poll only sets the
cursor to the newest id — the existing archive is the sweep's job, within its
windows and budget. Each new id becomes a `doc_new` job: analyse once, then
for every eligible user who can read it (their own token) derive their
deadline findings and (Phase 7) triage. Not budgeted.

Backlog: the sweep (`proactive/tasks/sweep.py`) calls `process_for_user` for one
(user, document) at a time, inside the windows.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from proactive import analysis, clock, deadlines, jobs, users
from proactive.db import connect
from proactive.jobs import Defer, Job, handler
from proactive.models import ResolvedModel, resolve_model
from proactive.paperless_ro import TokenRejected, can_read, document, max_id_admin, newest_ids_admin

log = logging.getLogger("proactive.documents")

NEWDOC_POLL = "newdoc_poll"
DOC_NEW = "doc_new"
#: A new document's sidecar appears when the app's sync has ingested it. Wait
#: this long for it, then give up on that document (the sweep still gets it).
SIDECAR_WAIT = timedelta(days=3)

#: Per-user work after the analysis (Phase 7 adds triage). Each returns whether
#: it used the LLM.
_after_analysis: list = []


def after_analysis(fn):
    """Register `async fn(user, token, analysis, doc, model) -> bool`."""
    _after_analysis.append(fn)
    return fn


def _cursor() -> int | None:
    with connect() as conn:
        r = conn.execute("SELECT last_doc_id FROM newdoc_cursor WHERE id = 1").fetchone()
    return r["last_doc_id"] if r else None


def _set_cursor(doc_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO newdoc_cursor (id, last_doc_id) VALUES (1, ?)"
            " ON CONFLICT(id) DO UPDATE SET last_doc_id = MAX(last_doc_id, excluded.last_doc_id)",
            (int(doc_id),),
        )


@handler(NEWDOC_POLL)
async def newdoc_poll(job: Job) -> None:
    cur = _cursor()
    if cur is None:
        _set_cursor(await max_id_admin())
        log.info("new-document cursor initialised at %s", _cursor())
        return
    ids = await newest_ids_admin(cur, limit=100)
    for doc_id in ids:
        jobs.enqueue(DOC_NEW, "", {"doc_id": doc_id, "first_seen": clock.now_iso()},
                     needs_llm=True, model=_operator_model(), dedupe_key=f"doc_new:{doc_id}")
        _set_cursor(doc_id)
    if ids:
        log.info("%d new document(s) after #%s", len(ids), cur)


def _operator_model() -> ResolvedModel | None:
    from config.proactive import proactive_settings

    cfg = proactive_settings()
    name = cfg.proactive_model_fixed or cfg.proactive_model_default
    if not name:
        return None
    return ResolvedModel(name, "local", "fixed" if cfg.proactive_model_fixed else "default", name)


async def _readers(doc_id: int) -> list[tuple[str, str, dict]]:
    """Eligible users who can read the document, with its metadata as they see it."""
    out = []
    for user, tok in users.eligible():
        try:
            if not await can_read(tok, doc_id):
                continue
            meta = await document(tok, doc_id) or {}
        except TokenRejected:
            users.token_rejected(user)
            continue
        out.append((user, tok, meta))
    return out


@handler(DOC_NEW, needs_llm=True)
async def doc_new(job: Job) -> None:
    doc_id = int(job.payload["doc_id"])
    readers = await _readers(doc_id)
    if not readers:
        return  # nobody opted in can read it — nothing to do, no LLM spent
    user0, tok0, meta0 = readers[0]
    model = _operator_model() or resolve_model(user0, tok0)
    if model is None:
        log.info("doc %s: no allowed model for %s — skipped", doc_id, user0)
        return
    try:
        a, _used = await analysis.ensure(doc_id, model, user0, tok0, doc_date=str(meta0.get("created", ""))[:10])
    except analysis.NoText:
        first = clock.parse(job.payload.get("first_seen") or clock.now_iso())
        if clock.utcnow() - first > SIDECAR_WAIT:
            log.warning("doc %s: no extracted text after %s — left to the sweep", doc_id, SIDECAR_WAIT)
            return
        raise Defer(f"doc {doc_id} not ingested yet", 1800)
    for user, tok, meta in readers:
        await per_user(user, tok, a, meta, resolve_model(user, tok) or model)


async def per_user(user: str, token: str, a: analysis.Analysis, meta: dict, model: ResolvedModel) -> bool:
    """Everything one user gets from one analysed document. Returns used_llm."""
    from proactive import prefs

    lang = prefs.get(user).language or "en"
    await deadlines.findings_for(user, a, doc_title=str(meta.get("title") or ""), lang=lang)
    used = False
    for fn in _after_analysis:
        used = bool(await fn(user, token, a, meta, model)) or used
    return used


async def process_for_user(user: str, token: str, doc_id: int, model: ResolvedModel) -> bool:
    """The sweep's processor: one backlog document for one user."""
    meta = await document(token, doc_id)
    if meta is None:
        return False  # no access (any more)
    try:
        a, used = await analysis.ensure(doc_id, model, user, token, doc_date=str(meta.get("created", ""))[:10])
    except analysis.NoText:
        return False  # never ingested: nothing to read
    return await per_user(user, token, a, meta, model) or used


def install() -> None:
    from proactive.tasks import sweep

    sweep.set_processor(process_for_user)
