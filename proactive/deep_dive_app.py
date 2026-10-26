"""The deep dive, app side (Phase 7: the existing agent loop with documents and
web; Phase 8 swaps in a full Werkbank run).

The model works with a Werkbank `ToolBelt` restricted to read-only document
tools and the web. The belt applies the proactive query guard to every web
search before it runs, and records every fetched page — the worker's
validator accepts only those URLs as sources. The result is returned to the
worker, which validates and writes it; nothing here writes the finding.
"""

from __future__ import annotations

import logging

from werkbank.v2.tools import ToolBelt

log = logging.getLogger("proactive.deep_dive")

TOOLS = ["search", "get_document_details", "get_document_page_text", "web_search", "web_fetch_page"]

SYSTEM = """\
You take a closer look at one item from a person's document archive and work \
out whether there is something concrete they could do — for example switch a \
tariff, cancel or renegotiate a contract, claim money back — and what the \
current market or legal situation says about it.

Use the document tools to read the documents named in the task. Use web_search \
with GENERIC queries only: category, product and year (e.g. "Riester Kosten \
Kritik 2026", "Kfz-Versicherung Wechsel November 2026"). Never put names, \
numbers, amounts, dates or anything else from the documents into a web query. \
Read the pages you rely on with web_fetch_page; a page you did not fetch is not \
a source. Answer in {language}.\
"""

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

RESULT_PROMPT = """\
Write the result of your research.
summary: what you found, 3–6 sentences.
recommendation: one concrete step the person could take, or "" if there is none.
evidence: sentences copied EXACTLY from the documents that the recommendation \
rests on, each with its document id.
sources: the URLs of the web pages you fetched and relied on.
Your notes so far:

{notes}"""


class GuardedBelt(ToolBelt):
    """A ToolBelt whose web_search refuses personal identifiers (code, not prompt)."""

    names: list[str] = []

    async def execute(self, name: str, args: dict) -> str:
        if name == "web_search":
            from proactive import guard

            rejected = guard.check(str(args.get("query") or ""), self.names)
            if rejected:
                return rejected
        return await super().execute(name, args)


async def run(user: str, token: str, finding, model) -> dict:
    from i18n import language_name
    from proactive.paperless_ro import whoami
    from werkbank.llm_lane import complete_structured
    from werkbank.v2 import agent_loop, registry

    from proactive import guard

    try:
        names = guard.user_names(await whoami(token))
    except Exception:
        names = [user.lower()]
    belt = GuardedBelt(
        registry=registry.load_defaults(),
        allowed_tools=list(TOOLS),
        user_id=user,
        token=token,
        persist=False,
    )
    belt.names = names
    who, tok = (user, token) if model.uses_registry else ("", "")
    lang = language_name(finding.lang or "en")
    task = (
        f"Item: {finding.title}\nTopic: {finding.topic}\nDocuments: "
        + ", ".join(f"#{d}" for d in finding.doc_ids)
        + f"\n\n{finding.body}"
    )
    notes = await agent_loop.gather(
        SYSTEM.format(language=lang), task, belt, model=model.name, user_id=who, token=tok,
    )
    raw = await complete_structured(
        SYSTEM.format(language=lang),
        [{"role": "user", "content": RESULT_PROMPT.format(notes=notes[:12000]) + "\n\nSources:\n" + belt.catalogue()}],
        model=model.name, user_id=who, token=tok, json_schema=RESULT_SCHEMA, tool_name="deep_dive_result",
    )
    fetched = {
        str(r.args.get("url")): (r.retrieved_at or "")[:10]
        for r in belt.records
        if r.tool == "web_fetch_page" and r.outcome == "ok" and r.args.get("url")
    }
    return {
        "summary": str(raw.get("summary") or ""),
        "recommendation": str(raw.get("recommendation") or ""),
        "evidence": raw.get("evidence") or [],
        "sources": raw.get("sources") or [],
        "fetched": fetched,
    }
