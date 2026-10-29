"""App-side research for the proactive system: deep dives and dossiers.

Deep dive (`start` + `result`, Phase 8): a full Werkbank v2 run for the
finding — planner, agents, the D-checks, the report — started headless with the
user's token from their background-access grant. `start` puts the run in the
Werkbank list at once (status "briefing", then "running") and returns its id;
the run goes on in the background, so the user sees it immediately and the
worker's request does not hang for the length of a run. The proactive query
guard is set for the run (`werkbank.v2.tools.web_query_guard`), so every web
search of every agent is screened in code, and `llm_lane.yield_to_chat`, so a
user who starts chatting gets the GPU after the call in progress.

`result` is what the worker polls: the run's state, and once the report is
written, the report condensed into summary / recommendation / evidence /
sources with the pages the run actually fetched, and the report's note in
`PaperlessBrain Research/` (`vault/research.py`); the worker validates and
writes. Nothing here writes the finding.

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
Something was noticed in my documents. Find out what it means for me and \
whether acting on it is worth it.

What was noticed: {title}
{summary}
{quotes}
Noticed in: {docs}

These are where it was noticed, not necessarily where the answer is. First \
search my archive for the documents that belong to it — the signed contract \
behind an offer, later statements, letters from the same sender — and work \
from the binding one. Something missing from the documents above is not \
missing from my archive until that search found nothing.

Answer the question this raises — nothing broader, no catalogue of options \
that do not apply. Work with the numbers in the documents: where a step has a \
cost or a benefit, calculate it (interest saved per year, fees, the difference \
to current offers) instead of describing it. Look up the current market and \
legal situation only as far as it decides that question. End with what to do \
next and by when — or that nothing needs doing. Web searches only with generic \
terms (category, product, year), never my names, numbers or amounts."""


def research_request(finding) -> str:
    """The assignment for the finding's deep dive: what was noticed, in the
    finding's own words, and the quotes behind it.

    Not the whole finding body: its sources are whatever triage happened to
    fetch (a portal's home page, an unrelated press release) and its
    disclaimer is boilerplate — handed over, both were taken as the run's own
    sources and listed in the report. The old wording also named four generic
    options ("switch, cancel, renegotiate, claim"), and the planner dutifully
    spent a subtask on the law behind each, whether or not it applied.
    """
    from i18n import translator_for
    from proactive import findings

    _ = translator_for(finding.lang or "en")
    summary = findings._section(finding.body, _("Summary")) or finding.title
    quotes = [f'- "{e["quote"]}" (#{e["doc_id"]})' for e in findings.parse_evidence(finding.body)]
    return REQUEST.format(
        title=finding.title, summary=summary,
        quotes=("From the documents, verbatim:\n" + "\n".join(quotes[:8])) if quotes else "",
        docs=", ".join(f"#{d}" for d in finding.doc_ids),
    )


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


async def start(user: str, token: str, finding, model) -> str:
    """Create the Werkbank run for `finding`, launch it in the background and
    return its id at once."""
    import asyncio

    from werkbank.llm_lane import yield_to_chat
    from werkbank.v2 import pipeline, store
    from werkbank.v2.tools import web_query_guard

    pipeline.register_token(user, token)
    request = research_request(finding)
    run_id = pipeline.create_draft(request, user, model.name)  # listed from now on
    guard = _names_guard(await _names(user, token))

    async def go() -> None:
        # Set inside the task: these hold for every call the run makes.
        g, y = web_query_guard.set(guard), yield_to_chat.set(True)
        try:
            brief = await pipeline.brief_draft(run_id, user, model.name)
            if brief is None:  # deleted while briefing
                return
            pipeline.confirm_draft(run_id, user, brief)
            store.set_run_status(run_id, user, "running")
            # The note in PaperlessBrain Research links back to the finding.
            from vault.research import wikilink

            await pipeline.start_run(run_id, user, model.name, notify=False,
                                     note_meta={"finding": wikilink(finding.rel, finding.title)})
            log.info("%s: deep dive for %s finished (Werkbank run %s)", user, finding.finding_id, run_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("%s: deep dive run %s failed", user, run_id)
            if store.run_status(run_id, user) not in (None, "done"):
                pipeline.fail_run(run_id, user, exc)
        finally:
            web_query_guard.reset(g)
            yield_to_chat.reset(y)

    pipeline.track(run_id, asyncio.create_task(go()))
    return run_id


async def result(user: str, token: str, run_id: str, finding, model) -> dict:
    """`{"state": …}` of the run. "gone" = deleted, "running" = working on it
    in this process, "stalled" = not finished and nothing works on it (the app
    restarted, or it failed — the user can resume it in the Werkbank), "done"
    with the condensed result."""
    from i18n import language_name
    from vault import research
    from werkbank.llm_lane import complete_structured
    from werkbank.v2 import pipeline, store

    status = store.run_status(run_id, user)
    if status is None:
        return {"state": "gone"}
    if status in ("failed", "briefing_failed") and not pipeline.is_executing(run_id):
        state = store.load_state(run_id, user)
        return {"state": "failed", "error": state.error if state else ""}
    if status != "done":
        return {"state": "running" if pipeline.is_executing(run_id) else "stalled"}
    who, tok = (user, token) if model.uses_registry else ("", "")
    report = store.load_report(run_id, user)
    lang = language_name(finding.lang or "en")
    raw = await complete_structured(
        "You condense research reports. Answer only through the tool.",
        [{"role": "user", "content": CONDENSE.format(language=lang, report=report[:16000])}],
        model=model.name, user_id=who, token=tok, json_schema=RESULT_SCHEMA, tool_name="deep_dive_result",
    )
    return {
        "state": "done",
        "summary": str(raw.get("summary") or ""),
        "recommendation": str(raw.get("recommendation") or ""),
        "evidence": raw.get("evidence") or [],
        "sources": raw.get("sources") or [],
        "fetched": store.fetched_pages(run_id, user),
        "run_id": run_id,
        # The report's note in the vault — what the finding links to, and
        # what is left once the run is deleted.
        "note": await research.note_for_run(user, run_id) or "",
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
