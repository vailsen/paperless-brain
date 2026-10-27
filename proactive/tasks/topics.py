"""Loop 2 — topics (architecture §5, Phase 8): dossier build and incremental
update, topic revisits, rebuild after an analysis-version bump.

Revisits and rebuilds are backlog work: they run only inside a sweep window
and each counts one document against the window's budget. An update for a new
document is new-document work and runs any time.
"""

from __future__ import annotations

import logging

from proactive import analysis, clock, dossiers, jobs, llm, prefs, users, windows
from proactive.jobs import Defer, Job, handler
from proactive.models import resolve_model
from proactive.tasks import daily

log = logging.getLogger("proactive.topics")


def _model_payload(m) -> dict:
    return {"name": m.name, "lane": m.lane, "source": m.source, "model_id": m.model_id}


async def _build_or_update(user: str, topic: str, seed: int, *, rebuild: bool = False) -> None:
    from proactive.internal_client import InternalUnavailable
    from proactive.internal_client import dossier as remote

    tok = users.token(user)
    if not tok:
        return
    model = resolve_model(user, tok)
    if model is None:
        raise Defer("no allowed model", 3600)
    llm.check_cloud_cap(user, model)
    existing = dossiers.load(user, topic)
    payload = None if (existing is None or rebuild) else existing.existing_payload()
    try:
        res = await remote(user, topic, seed, payload, _model_payload(model))
    except InternalUnavailable as exc:
        raise Defer(f"app not reachable ({exc})", 600) from exc
    if res.get("error"):
        log.info("%s: dossier %s not written: %s", user, topic, res["error"])
        return
    llm.record_cloud_doc(user, model)
    lang = prefs.get(user).content_language
    d = dossiers.from_result(user, topic, res, lang=lang, version=analysis.version(), existing=existing)
    await dossiers.write(user, d)
    log.info(
        "%s: dossier %s %s — read %d document(s), %d timeline entries",
        user, topic, res.get("mode", "?"), len(res.get("read_doc_ids") or []), len(d.timeline),
    )


@handler("dossier_update", needs_llm=True)
async def dossier_update(job: Job) -> None:
    """Triage said: this document continues a longer matter."""
    await _build_or_update(job.user, job.payload["topic"], int(job.payload.get("doc_id") or 0))


def _in_window_or_defer() -> windows.OpenWindow:
    win = windows.current()
    if win is None:
        raise Defer("outside the sweep windows", 1800)
    if windows.processed(win.start_iso) >= win.window.max_docs:
        raise Defer("window budget used up", 1800)
    return win


@handler("dossier_build", needs_llm=True)
async def dossier_build(job: Job) -> None:
    """Full rebuild after `PROACTIVE_ANALYSIS_VERSION` was bumped."""
    win = _in_window_or_defer()
    d = dossiers.load(job.user, job.payload["topic"])
    if d is None or d.analysis_version >= analysis.version():
        return
    await _build_or_update(job.user, d.topic, d.last_doc_id, rebuild=True)
    windows.add_processed(win.start_iso, job.model_key or "dossier")


REVISIT_SCHEMA = {
    "type": "object",
    "properties": {
        "worth_finding": {"type": "boolean"},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "urgency": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "recommendation": {"type": "string"},
        "evidence_quotes": {"type": "array", "items": {
            "type": "object",
            "properties": {"doc_id": {"type": "integer"}, "quote": {"type": "string"}},
            "required": ["doc_id", "quote"],
        }},
        "sources": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["worth_finding", "title", "summary", "urgency", "recommendation", "evidence_quotes", "sources"],
}

REVISIT_SYSTEM = """\
You keep watch over one matter in a person's documents. Below are the dossier \
and current web pages about the topic. Is there something NEW — a changed law, \
a better market offer, a deadline in the dossier that now matters — worth \
telling them? If not, worth_finding = false. Write in {language}. evidence_quotes: \
sentences copied exactly from the dossier's documents (with their id). sources: \
URLs of the pages below you relied on, only those.\
"""


@handler("dossier_revisit", needs_llm=True)
async def dossier_revisit(job: Job) -> None:
    """The web may have changed without a new document (architecture §5, loop 2)."""
    from i18n import language_name
    from proactive import findings, guard, scoring
    from proactive.paperless_ro import whoami
    from proactive.web import QueryRejected, WebRun

    win = _in_window_or_defer()
    user, topic = job.user, job.payload["topic"]
    tok = users.token(user)
    d = dossiers.load(user, topic)
    if not tok or d is None:
        return
    today = clock.local_now().date()
    if d.revisit_after is None or d.revisit_after > today.isoformat():
        return
    model = resolve_model(user, tok)
    if model is None:
        raise Defer("no allowed model", 3600)
    llm.check_cloud_cap(user, model)
    try:
        names = guard.user_names(await whoami(tok))
    except Exception:
        names = [user.lower()]
    run = WebRun(names=names)
    pages = []
    try:
        for hit in await run.search(f"{topic.replace('-', ' ')} {today.year}", max_results=4):
            page = await run.fetch(hit["url"])
            if page:
                pages.append((hit["url"], page))
            if len(pages) >= 2:
                break
    except QueryRejected:
        pass
    except Exception as exc:
        log.info("%s: revisit web lookup failed: %s", user, type(exc).__name__)
    lang = language_name(d.lang)
    raw = await llm.structured(
        model, user, tok,
        system=REVISIT_SYSTEM.format(language=lang),
        messages=[{"role": "user", "content": f"Today: {clock.local_now().date().isoformat()}\n\n"
                   + dossiers.render_body(d) + "\n\n" + "\n\n".join(
            f"Page {u}:\n{p[:4000]}" for u, p in pages) + f"\n\nWrite in {lang}."}],
        json_schema=REVISIT_SCHEMA, tool_name="revisit",
    )
    llm.record_cloud_doc(user, model)
    windows.add_processed(win.start_iso, model.key)
    if raw.get("worth_finding"):
        texts = {i: analysis.sidecar_text(i) or "" for i in d.doc_ids}
        rec, ev, src = scoring.validate_advice(
            str(raw.get("recommendation") or ""), raw.get("evidence_quotes") or [], raw.get("sources") or [],
            doc_texts=texts, fetched=run.fetched,
        )
        if ev:
            f = findings.Finding(
                finding_id="", kind="insight", title=str(raw.get("title") or topic)[:150], topic=topic,
                urgency=str(raw.get("urgency") or "medium"), advice=bool(rec),
                doc_ids=sorted({e["doc_id"] for e in ev}),
                evidence_hash=findings.evidence_hash([e["doc_id"] for e in ev], [e["quote"] for e in ev] + [topic]),
                analysis_version=analysis.version(), finding_key=f"revisit:{topic}:{today:%Y-%m}", lang=d.lang,
                body=findings.render_body(d.lang, summary=str(raw.get("summary") or ""), evidence=ev,
                                          sources=src, recommendation=rec),
            )
            await findings.upsert(user, f)
    d.revisit_after = dossiers.revisit_after(user, topic, today)
    await dossiers.write(user, d)


@daily.also
async def schedule_topics(user: str) -> None:
    """Nightly: rebuilds after a version bump, revisits that are due."""
    today = clock.local_now().date().isoformat()
    ver = analysis.version()
    tok = users.token(user)
    model = resolve_model(user, tok) if tok else None
    for d in dossiers.list_dossiers(user):
        if d.analysis_version < ver:
            jobs.enqueue("dossier_build", user, {"topic": d.topic}, model=model,
                         dedupe_key=f"dossier_build:{user}:{d.topic}:{ver}")
        elif d.revisit_after and d.revisit_after <= today:
            jobs.enqueue("dossier_revisit", user, {"topic": d.topic}, model=model,
                         dedupe_key=f"dossier_revisit:{user}:{d.topic}:{today}")
