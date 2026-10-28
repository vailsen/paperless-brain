"""Triage: is a document worth telling this user about? (architecture §10)

Per user, per document they can read, after its analysis. Input: the analysis,
an excerpt of the text (quotes must come from it), the topic labels the user
already has (the model must reuse one when it fits), what is muted, and a few
similar documents (the app's index, best effort). Output: a JSON object; the
code then decides everything that matters:

- evidence quotes must occur in the document — invented quotes are dropped;
- a recommendation survives only with a verbatim quote *and* a web page
  fetched in this run (`scoring.validate_advice`); otherwise the finding is
  written as facts with `advice: false`;
- the disclaimer is appended by code (`findings.render_body`);
- priority and push/hold are decided later by the ledger scan, from
  `_interests.md` — not here and not by the model.

Web lookups go through `proactive.web`, i.e. through the query guard.
"""

from __future__ import annotations

import logging

from proactive import findings, interests, llm, scoring
from proactive.analysis import Analysis, sidecar_text
from proactive.interests import slugify

log = logging.getLogger("proactive.triage")

EXCERPT_CHARS = 8000

SCHEMA = {
    "type": "object",
    "properties": {
        "worth_finding": {"type": "boolean"},
        "topic": {"type": "string"},
        "urgency": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "evidence_quotes": {"type": "array", "items": {"type": "string"}},
        "needs_web": {"type": "boolean"},
        "web_query": {"type": "string"},
        "dossier_update": {"type": "boolean"},
    },
    "required": ["worth_finding", "topic", "urgency", "title", "summary", "evidence_quotes",
                 "needs_web", "web_query", "dossier_update"],
}

SYSTEM = """\
You look at one document from a person's archive and decide whether it holds \
something worth telling them about without being asked: money they could save \
or claim, a contract or insurance worth reviewing, a change of conditions, a \
risk. Ordinary invoices, receipts, statements and letters that need nothing \
are NOT worth a finding — say so. Deadlines are handled elsewhere; do not \
report a deadline alone.

Judge it from TODAY (given below), not from the document's date. An option \
bound to a window that has already closed — a special termination right "within \
one month", an objection period, an offer valid until a past date — is history: \
never present it as something the person can still use. If the matter itself \
still counts (a premium that went up, conditions that got worse), say what can \
be done now instead — e.g. compare offers before the next regular renewal. If \
nothing of it matters any more, it is not worth a finding.

topic: a short subject label (1–3 words) in {language}, spelled as in normal \
text (German nouns capitalised, umlauts kept). If one of the existing labels \
fits, use it exactly.
urgency: how soon it matters — low, medium, high, critical.
title: one line, in {language}.
summary: 2–4 sentences in {language}: what you noticed and why it may matter.
evidence_quotes: sentences copied EXACTLY from the document text that support it.
needs_web: true if current market or legal information would make it concrete.
web_query: then a GENERIC query — category, product, year (e.g. "Riester \
Kosten Kritik 2026"). Never a name, number, amount or date from the document.
dossier_update: true if this document continues a longer matter (a contract \
over years, a building society savings plan, a court case).\
"""

LANGUAGE_REMINDER = (
    "\nWrite title and summary in {language}, even if the document is in another "
    "language. Evidence quotes stay exactly as in the document."
)

ADVICE_SCHEMA = {
    "type": "object",
    "properties": {
        "recommendation": {"type": "string"},
        "sources": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["recommendation", "sources"],
}

ADVICE_SYSTEM = """\
Given a finding from a person's documents and the web pages below, suggest one \
concrete step they could take, in {language}, or "" if the pages do not support \
one. sources: the URLs of the pages below you relied on — only those.\
"""


async def triage(user: str, token: str, a: Analysis, meta: dict, model) -> bool:
    """Run triage for one (user, document). Returns True (it used the LLM)."""
    from proactive import guard, prefs
    from proactive.paperless_ro import whoami
    from proactive.web import QueryRejected, WebRun

    text = sidecar_text(a.doc_id) or ""
    if not text:
        return False
    lang_code = prefs.get(user).content_language
    from i18n import language_name

    lang = language_name(lang_code)
    known = interests.load(user)
    similar = await _similar(user, a.doc_id)
    from proactive import clock

    context = [
        f"Today: {clock.local_now().date().isoformat()}",
        f"Document #{a.doc_id}: {meta.get('title', '')} (dated {str(meta.get('created', ''))[:10]})",
        f"Analysis: {a.summary}",
        "Existing topic labels: " + (", ".join(sorted(_labels(user, known.rows, lang_code))) or "(none)"),
    ]
    if similar:
        context.append("Similar documents: " + "; ".join(f"#{d['doc_id']} {d.get('title', '')}" for d in similar))
    corr = meta.get("correspondent") if isinstance(meta.get("correspondent"), int) else None
    known_series = [f for f in findings.list_findings(user)
                    if f.kind == "insight" and corr is not None and f.correspondent == corr]
    if known_series:
        # The same sender's earlier findings: a recurring letter (each monthly
        # invoice announcing the same price change) is that finding again,
        # under its topic, not a new one.
        context.append("Already reported for this sender (reuse the topic if it is the same matter): " + "; ".join(
            f"topic {f.topic_display!r}: {f.title}" for f in known_series[:5]))
    context.append("\nDocument text:\n" + text[:EXCERPT_CHARS])
    # Last, where it is followed: given a German document, the model otherwise
    # drifts into German for title and summary whatever the system prompt says.
    context.append(LANGUAGE_REMINDER.format(language=lang))

    llm.check_cloud_cap(user, model)
    raw = await llm.structured(
        model, user, token,
        system=SYSTEM.format(language=lang),
        messages=[{"role": "user", "content": "\n".join(context)}],
        json_schema=SCHEMA, tool_name="triage",
    )
    if not raw or not raw.get("worth_finding"):
        return True
    label = str(raw.get("topic") or a.topic_hint)
    topic = slugify(label)
    quotes = [q for q in (raw.get("evidence_quotes") or []) if isinstance(q, str)]
    evidence = [{"doc_id": a.doc_id, "quote": q} for q in quotes]
    rec, sources = "", []
    _, evidence, _ = scoring.validate_advice("", evidence, [], doc_texts={a.doc_id: text}, fetched={})
    if not evidence:
        log.info("%s: triage of doc %s had no verifiable quote — no finding", user, a.doc_id)
        return True

    if raw.get("needs_web") and str(raw.get("web_query") or "").strip():
        try:
            names = guard.user_names(await whoami(token))
        except Exception:
            names = [user.lower()]
        run = WebRun(names=names)
        pages = []
        try:
            for hit in (await run.search(str(raw["web_query"]), max_results=4))[:4]:
                page = await run.fetch(hit["url"])
                if page:
                    pages.append((hit["url"], page))
                if len(pages) >= 2:
                    break
        except QueryRejected as exc:
            log.info("%s: triage web query refused by the guard: %s", user, exc)
        except Exception as exc:
            log.info("%s: triage web lookup failed: %s", user, type(exc).__name__)
        if pages:
            advice = await llm.structured(
                model, user, token,
                system=ADVICE_SYSTEM.format(language=lang),
                messages=[{"role": "user", "content": (
                    f"Finding: {raw.get('title', '')}\n{raw.get('summary', '')}\n\n"
                    + "\n\n".join(f"Page {u}:\n{p[:4000]}" for u, p in pages)
                    + f"\n\nWrite the recommendation in {lang}."
                )}],
                json_schema=ADVICE_SCHEMA, tool_name="advice",
            )
            rec, evidence, sources = scoring.validate_advice(
                str(advice.get("recommendation") or ""), evidence, advice.get("sources") or [],
                doc_texts={a.doc_id: text}, fetched=run.fetched,
            )
    llm.record_cloud_doc(user, model)

    f = findings.Finding(
        finding_id="",
        kind="insight",
        title=str(raw.get("title") or meta.get("title") or topic)[:150],
        topic=topic,
        topic_label=findings.topic_label_for(label, topic),
        urgency=str(raw.get("urgency") or "medium"),
        advice=bool(rec),
        doc_ids=[a.doc_id],
        evidence_hash=findings.evidence_hash([a.doc_id], [e["quote"] for e in evidence] + [topic]),
        analysis_version=a.version,
        finding_key=findings.series_key(topic, corr, a.doc_id),
        correspondent=corr,
        lang=lang_code,
        body=findings.render_body(
            lang_code, summary=str(raw.get("summary") or ""), evidence=evidence,
            sources=sources, recommendation=rec,
        ),
    )
    made, outcome = await findings.upsert_series(user, f)
    log.info("%s: triage of doc %s → finding %s (%s, advice=%s)", user, a.doc_id, made.finding_id, outcome, bool(rec))
    if raw.get("dossier_update"):
        from proactive import jobs

        jobs.enqueue("dossier_update", user, {"topic": topic, "doc_id": a.doc_id},
                     needs_llm=True, model=model, dedupe_key=f"dossier:{user}:{topic}:{a.doc_id}")
    return True


def _labels(user: str, topics, lang: str) -> list[str]:
    """The known topics as the model should reuse them: the label a finding
    carries, else the key made readable. Reusing a label keeps the key."""
    seen = {f.topic: f.topic_label for f in findings.list_findings(user) if f.topic_label}
    return [findings.topic_display(t, seen.get(t, ""), lang) for t in topics]


async def _similar(user: str, doc_id: int) -> list[dict]:
    from proactive.internal_client import InternalUnavailable, similar

    try:
        return await similar(user, doc_id, k=4)
    except (InternalUnavailable, Exception) as exc:
        log.info("similar documents unavailable: %s", type(exc).__name__)
        return []
