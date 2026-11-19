"""Plan §6.3 — what the real agent would send as `keywords`.

One single-turn conversation per eval query with the production system prompt,
the production tool list (default groups: everything but `visual`) and an
eval-only `keywords` parameter on `search`. The model is the user's chat model
(local lane only — the harness makes no cloud call). Run once; cached in
keywords.v1.jsonl so every later config is deterministic.
"""

from __future__ import annotations

import asyncio
import copy
import json
import time

import httpx

from eval.harness.common import KEYWORDS, load_jsonl, load_queries, write_jsonl

KEYWORDS_PARAM = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "Exact terms, synonyms, abbreviations and spelling variants likely to appear "
        "verbatim in the target documents (e.g. 'KFZ' for 'Auto'). 3–8 items."
    ),
}

# app_ui/pages/chat.py _TOOL_GROUPS with _TOOL_GROUP_DEFAULTS (all on but `visual`).
# Copied, not imported: importing the page module registers NiceGUI routes.
DEFAULT_GROUP_TOOLS = {
    "documents": ["search", "search_exact", "find_similar_documents", "get_document_details",
                  "get_document_table", "get_document_page_text", "show_document_image", "get_actions"],
    "email": ["search_emails"], "calendar": ["search_calendar"], "web": ["web_search", "web_fetch_page"],
    "calculate": ["calculate"],
    "memory": ["remember_fact", "create_deadline", "search_memory", "update_brain_fact", "delete_brain_fact"],
    "vault": ["vault_search"], "document_notes": ["create_note"],
}


def _tools() -> list[dict]:
    from services.chat_service import TOOL_DEFINITIONS, _to_ollama_tools

    names = {"get_current_date"} | {t for ts in DEFAULT_GROUP_TOOLS.values() for t in ts}
    defs = [copy.deepcopy(t) for t in TOOL_DEFINITIONS if t["name"] in names]
    for t in defs:
        if t["name"] == "search":
            t["input_schema"]["properties"]["keywords"] = KEYWORDS_PARAM
    return _to_ollama_tools(defs)


def _system(user: str, lang: str) -> str:
    from config.chat_prompts import build_system_prompt
    from werkbank.settings_store import get_archive_language

    return build_system_prompt(set(DEFAULT_GROUP_TOOLS), user, lang, archive_language=get_archive_language())


def _model(user: str) -> dict:
    from pathlib import Path

    from services import background_access, model_registry

    tok = background_access.token_for(user)
    # Read-only: NiceGUI's general store is not loaded outside the running app.
    general = json.loads(Path(".nicegui/storage-general.json").read_text())
    name = ((general.get("user_prefs") or {}).get(user) or {}).get("chat_model_name")
    m = model_registry.get_by_name(name, user, tok)
    if not m or m.get("lane") != "local":
        raise SystemExit(f"{user}'s chat model {name!r} is not on the local lane — no cloud calls in the eval.")
    return m


async def _ask(client: httpx.AsyncClient, m: dict, system: str, tools: list, query: str) -> dict:
    from services.inference_activity import track

    payload = {
        "model": m["model"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": query}],
        "tools": tools,
        "stream": False,
    }
    t0 = time.perf_counter()
    async with track(m["base_url"], m["model"], "eval keywords"):
        r = await client.post(m["base_url"].rstrip("/") + "/chat/completions", json=payload,
                              headers={"Authorization": f"Bearer {m.get('api_key') or 'ollama'}"})
    r.raise_for_status()
    data = r.json()
    msg = data["choices"][0]["message"]
    calls = msg.get("tool_calls") or []
    first = calls[0]["function"] if calls else None
    args = {}
    if first:
        try:
            args = json.loads(first.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"_raw": first.get("arguments")}
    search_call = next((c["function"] for c in calls if c["function"]["name"] == "search"), None)
    sargs = json.loads(search_call["arguments"]) if search_call else {}
    kws = sargs.get("keywords") or []
    return {
        "first_tool": first["name"] if first else None,
        "first_args": args,
        "semantic_query": sargs.get("semantic_query"),
        "keywords": kws,
        # Output-token cost of the parameter itself (≈ 3.5 chars/token for this tokenizer
        # family); the call's whole completion includes the thinking.
        "kw_tokens_est": round(len(json.dumps({"keywords": kws}, ensure_ascii=False)) / 3.5, 1) if kws else 0,
        "completion_tokens": (data.get("usage") or {}).get("completion_tokens"),
        "seconds": round(time.perf_counter() - t0, 1),
    }


def generate(user_lang: str = "en") -> None:
    queries = load_queries()
    done = {r["id"]: r for r in load_jsonl(KEYWORDS)}
    tools = _tools()

    async def main():
        async with httpx.AsyncClient(timeout=600) as client:
            for q in queries:
                if q["id"] in done:
                    continue
                m = _model(q["user"])
                res = await _ask(client, m, _system(q["user"], user_lang), tools, q["query"])
                done[q["id"]] = {"id": q["id"], "model": m["model"], **res}
                write_jsonl(KEYWORDS, [done[x["id"]] for x in queries if x["id"] in done])
                print(f"{q['id']}: {res['first_tool']} kw={res['keywords']} ({res['seconds']} s)")

    asyncio.run(main())
