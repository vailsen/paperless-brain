"""App-side research for the proactive system: deep dives and dossiers.

Deep dive (`run`, Phase 8): a full Werkbank v2 run for the finding — planner,
agents, the D-checks, the report — started headless with the user's token from
their background-access grant. The proactive query guard is set for the run
(`werkbank.v2.tools.web_query_guard`), so every web search of every agent is
screened in code. The report is then condensed into summary / recommendation /
evidence / sources, and returned with the pages the run actually fetched; the
worker validates and writes. Nothing here writes the finding.

Dossier (`dossier`, Phase 8): candidates from the index (semantic search on the
topic, neighbours of the seed document, same correspondent), a relevance pass,
then the dossier itself — or, for an existing dossier, an incremental update
that reads only the new document.
"""

from __future__ import annotations

import logging

log = logging.getLogger("proactive.research")

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "recommendation": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"doc_id": {"type": "integer"}, "quote": {"type": "string"}},
                "required": ["doc_id", "quote"],
            },
        },
        "sources": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "recommendation", "evidence", "sources"],
}

CONDENSE = """\
Below is a research report about an item from a person's documents. Condense it, \
in {language}:
summary: what was found, 3–6 sentences.
recommendation: one concrete step the person could take, or "" if the report \
does not support one.
evidence: sentences from the person's documents that the report quotes and the \
recommendation rests on — copied exactly, each with its document id.
sources: the URLs of web pages the report relies on.

{report}"""

REQUEST = """\
Take a closer look at this item from my documents and tell me whether there is \
something concrete I could do about it — switch, cancel, renegotiate, claim — \
and what the current market or legal situation says. Use web searches only \
with generic terms (category, product, year), never my names, numbers or \
amounts.

Item: {title}
Topic: {topic}
Documents: {docs}

{body}"""


def _names_guard(names: list[str]):
    from proactive import guard

    return lambda query: guard.check(query, names)


async def _names(user: str, token: str) -> list[str]:
    from proactive import guard
    from proactive.paperless_ro import whoami

    try:
        return guard.user_names(await whoami(token))
    except Exception:
        return [user.lower()]


async def run(user: str, token: str, finding, model) -> dict:
    from i18n import language_name
    from werkbank.llm_lane import complete_structured
    from werkbank.v2 import pipeline, store
    from werkbank.v2.tools import web_query_guard

    who, tok = (user, token) if model.uses_registry else ("", "")
    pipeline.register_token(user, token)
    reset = web_query_guard.set(_names_guard(await _names(user, token)))
    try:
        run_id, _state = await pipeline.run_from_request(
            REQUEST.format(
                title=finding.title, topic=finding.topic,
                docs=", ".join(f"#{d}" for d in finding.doc_ids), body=finding.body[:6000],
            ),
            user,
            model.name,
            notify=False,  # the deep dive's own result message follows
        )
    finally:
        web_query_guard.reset(reset)
    report = store.load_report(run_id, user)
    lang = language_name(finding.lang or "en")
    raw = await complete_structured(
        "You condense research reports. Answer only through the tool.",
        [{"role": "user", "content": CONDENSE.format(language=lang, report=report[:16000])}],
        model=model.name, user_id=who, token=tok, json_schema=RESULT_SCHEMA, tool_name="deep_dive_result",
    )
    log.info("%s: deep dive for %s done (Werkbank run %s)", user, finding.finding_id, run_id)
    return {
        "summary": str(raw.get("summary") or ""),
        "recommendation": str(raw.get("recommendation") or ""),
        "evidence": raw.get("evidence") or [],
        "sources": raw.get("sources") or [],
        "fetched": store.fetched_pages(run_id, user),
        "run_id": run_id,
    }


# ── Dossiers ─────────────────────────────────────────────────────────────────

RELEVANCE_SCHEMA = {
    "type": "object",
    "properties": {"relevant_doc_ids": {"type": "array", "items": {"type": "integer"}}},
    "required": ["relevant_doc_ids"],
}

DOSSIER_SCHEMA = {
    "type": "object",
    "properties": {
        "current_state": {"type": "string"},
        "timeline": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "event": {"type": "string"},
                    "doc_id": {"type": "integer"},
                },
                "required": ["date", "event", "doc_id"],
            },
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["current_state", "timeline", "open_questions"],
}

DOSSIER_SYSTEM = """\
You keep a running file on one matter in a person's documents (topic: {topic}) \
— a contract, a savings plan, an insurance, a dispute. Answer only through the \
tool, in {language}.
current_state: where the matter stands now: balance, conditions, obligations.
timeline: one entry per document event, oldest first: date (YYYY-MM-DD), what \
happened, and the id of the document that says so. Only documents given below.
open_questions: what is unclear or still to be done.\
"""


def _doc_brief(doc_id: int, limit: int = 1500) -> str:
    from proactive import analysis

    a = analysis.get(doc_id)
    text = analysis.sidecar_text(doc_id) or ""
    head = f"#{doc_id}"
    if a is not None:
        head += f" — {a.summary}"
    return f"{head}\n{text[:limit]}"


async def _candidates(user: str, token: str, topic: str, seed: int) -> dict[int, str]:
    """doc id → title, all readable by the user."""
    from config.settings import settings
    from pipelines.search import search
    from pipelines.similar import find_similar_documents
    from services.paperless import PaperlessClient

    client = PaperlessClient(settings.paperless_url, token)
    out: dict[int, str] = {}
    for r in await search({}, semantic_query=topic.replace("-", " "), n_results=30, paperless_client=client):
        out[r.document.id] = r.document.title
    if seed:
        seed_docs = await client.list_documents(ids=[seed])
        if seed_docs:
            out[seed] = seed_docs[0].title
            for r in await find_similar_documents(seed, n_results=15, paperless_client=client):
                out[r.document.id] = r.document.title
            name = seed_docs[0].correspondent
            corr = next((i for i, n in (await client.get_correspondent_map()).items() if n == name), None) if name else None
            if corr:
                for d in await client.list_documents(correspondent=[corr], fetch_all=False, page_size=50):
                    out.setdefault(d.id, d.title)
    return out


async def dossier(user: str, token: str, topic: str, seed: int, existing: dict | None, model) -> dict:
    from i18n import language_name
    from werkbank.llm_lane import complete_structured

    who, tok = (user, token) if model.uses_registry else ("", "")
    from proactive import prefs

    # The user's findings language, not the stored one: a new dossier had no
    # stored language and was always written in English.
    lang = language_name(prefs.get(user).content_language)
    system = DOSSIER_SYSTEM.format(topic=topic, language=lang)

    if existing and existing.get("doc_ids"):
        # Incremental: the dossier so far plus the one new document.
        from config.settings import settings
        from services.paperless import PaperlessClient

        if not await PaperlessClient(settings.paperless_url, token).list_documents(ids=[seed]):
            return {"error": "no access"}
        content = (
            f"Dossier so far:\nCurrent state: {existing.get('current_state', '')}\n"
            f"Timeline:\n" + "\n".join(
                f"- {t['date']} — {t['event']} — #{t['doc_id']}" for t in existing.get("timeline", [])
            )
            + "\nOpen questions:\n" + "\n".join(f"- {q}" for q in existing.get("open_questions", []))
            + f"\n\nNew document:\n{_doc_brief(seed, 4000)}\n\n"
            "Return the complete updated dossier."
        )
        raw = await complete_structured(system, [{"role": "user", "content": content}], model=model.name,
                                        user_id=who, token=tok, json_schema=DOSSIER_SCHEMA, tool_name="dossier")
        return {**raw, "doc_ids": sorted(set(existing["doc_ids"]) | {seed}), "read_doc_ids": [seed],
                "mode": "update"}

    cands = await _candidates(user, token, topic, seed)
    if not cands:
        return {"error": "no documents"}
    listing = "\n".join(f"#{d} {t}" for d, t in sorted(cands.items()))
    rel = await complete_structured(
        f"Which of these documents belong to the matter '{topic}'? Answer only through the tool.",
        [{"role": "user", "content": listing}],
        model=model.name, user_id=who, token=tok, json_schema=RELEVANCE_SCHEMA, tool_name="relevance",
    )
    relevant = sorted({int(d) for d in rel.get("relevant_doc_ids") or [] if int(d) in cands} | ({seed} if seed in cands else set()))
    relevant = relevant[:40]
    content = "Documents:\n\n" + "\n\n".join(_doc_brief(d) for d in relevant)
    raw = await complete_structured(system, [{"role": "user", "content": content}], model=model.name,
                                    user_id=who, token=tok, json_schema=DOSSIER_SCHEMA, tool_name="dossier")
    return {**raw, "doc_ids": relevant, "read_doc_ids": relevant, "mode": "build"}
