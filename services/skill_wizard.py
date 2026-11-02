"""The skill wizard (docs/experts.md §7.2): a model helps write a skill.

Interview → draft → lint (code) → try it side by side → the user saves.
The model drafts; it never saves. The lint is code: an invented tool name in a
skill would be followed by the chat model and fail silently, so every
snake_case word that looks like a tool must be one.

"Try it" runs the user's example question twice on the chosen expert — once
without the skill, once with it — with read-only tools only, so a trial can
never write a memory, a note or a document comment.
"""

from __future__ import annotations

import difflib
import re

#: Tools a trial may call: reading only.
TRY_TOOLS = (
    "search", "search_exact", "get_document_details", "get_document_table",
    "get_document_page_text", "get_actions", "calculate", "get_current_date",
    "search_memory", "vault_search",
)
_TOOLISH = re.compile(r"`?\b([a-z]{3,}(?:_[a-z]+)+)\b`?")

INTERVIEW_SYSTEM = """\
You help a user write a "skill": a short instruction pack that tells a \
document assistant how to handle one kind of task in the user's private \
document archive (invoices, contracts, letters, notices). Ask 3 to 5 short \
clarifying questions that would most improve the skill — about what a good \
result looks like, which documents are involved, the output format, edge \
cases. Offer 2–4 likely answers for each as suggestions. Write in {lang}."""

INTERVIEW_SCHEMA = {
    "type": "object",
    "properties": {"questions": {"type": "array", "items": {
        "type": "object",
        "properties": {"question": {"type": "string"},
                       "suggestions": {"type": "array", "items": {"type": "string"}}},
        "required": ["question", "suggestions"]}}},
    "required": ["questions"],
}

DRAFT_SYSTEM = """\
You write a skill for a document assistant. A skill has:
- name: 2–6 words, what it does ("Read an insurance policy").
- description: ONE sentence starting with "Use when", saying when it applies.
- instructions: numbered steps the assistant follows, then the expected \
output format. Concrete and short (at most 25 lines).
The assistant has these tools and no others: {tools}. Name a tool only if it \
is in that list. Never instruct it to state facts without a tool result, to \
guess amounts or dates, or to skip citing documents. Write in {lang}."""

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "description": {"type": "string"},
                   "instructions": {"type": "string"}},
    "required": ["name", "description", "instructions"],
}


def tool_names() -> list[str]:
    from services.chat_service import TOOL_DEFINITIONS

    return [t["name"] for t in TOOL_DEFINITIONS if t["name"] != "use_skill"]


def lint(name: str, description: str, body: str, *, disabled_tools: set[str] | None = None) -> list[dict]:
    """Findings `{level: warn|info, code, text}`; `text` is an English detail
    (tool names, numbers) the page puts into a translated sentence."""
    out: list[dict] = []
    from services.skills import MAX_BODY_CHARS

    if not (name or "").strip():
        out.append({"level": "warn", "code": "no_name", "text": ""})
    desc = (description or "").strip()
    if not desc:
        out.append({"level": "warn", "code": "no_description", "text": ""})
    elif not re.match(r"^(use when|verwenden,? wenn|nutzen,? wenn|wenn )", desc, re.I):
        out.append({"level": "info", "code": "description_trigger", "text": ""})
    if len(body or "") > MAX_BODY_CHARS:
        out.append({"level": "warn", "code": "too_long", "text": str(MAX_BODY_CHARS)})
    known = set(tool_names())
    for word in sorted({m.group(1) for m in _TOOLISH.finditer(body or "")}):
        if word in known:
            if disabled_tools and word in disabled_tools:
                out.append({"level": "warn", "code": "tool_off", "text": word})
            continue
        close = difflib.get_close_matches(word, known, n=2, cutoff=0.6)
        if close or word.endswith(("_search", "_document", "_documents", "_page")):
            out.append({"level": "warn", "code": "unknown_tool",
                        "text": word + (f" → {', '.join(close)}" if close else "")})
    return out


async def interview(model: str, user: str, token: str, goal: str, example: str, lang: str) -> list[dict]:
    from i18n import language_name
    from werkbank import llm_lane

    content = f"What the skill should do:\n{goal.strip()}"
    if example.strip():
        content += f"\n\nExample question / good answer:\n{example.strip()}"
    res = await llm_lane.complete_structured(
        INTERVIEW_SYSTEM.format(lang=language_name(lang)), [{"role": "user", "content": content}],
        model=model, user_id=user, token=token, json_schema=INTERVIEW_SCHEMA, tool_name="questions",
        max_tokens=1500, temperature=0.3,
    )
    out = []
    for q in (res.get("questions") or [])[:5]:
        if isinstance(q, dict) and str(q.get("question") or "").strip():
            out.append({"question": str(q["question"]).strip()[:300],
                        "suggestions": [str(s)[:120] for s in (q.get("suggestions") or [])[:4] if str(s).strip()]})
    return out


async def draft(model: str, user: str, token: str, goal: str, example: str,
                answers: list[tuple[str, str]], lang: str) -> dict:
    from i18n import language_name
    from werkbank import llm_lane

    parts = [f"Goal:\n{goal.strip()}"]
    if example.strip():
        parts.append(f"Example question / good answer:\n{example.strip()}")
    qa = "\n".join(f"- {q}\n  → {a}" for q, a in answers if a.strip())
    if qa:
        parts.append(f"Clarifications:\n{qa}")
    res = await llm_lane.complete_structured(
        DRAFT_SYSTEM.format(tools=", ".join(tool_names()), lang=language_name(lang)),
        [{"role": "user", "content": "\n\n".join(parts)}],
        model=model, user_id=user, token=token, json_schema=DRAFT_SCHEMA, tool_name="skill",
        max_tokens=2500, temperature=0.2,
    )
    return {
        "name": " ".join(str(res.get("name") or "").split())[:80],
        "description": " ".join(str(res.get("description") or "").split())[:300],
        "body": str(res.get("instructions") or "").strip(),
    }


def make_backend(rm: dict, fallback_key: str = ""):
    """A chat backend for one registry model (as the chat page builds them)."""
    from services.chat_service import ClaudeChatBackend, OpenAICompatibleChatBackend

    think = rm.get("think")
    if rm.get("backend") == "anthropic":
        return ClaudeChatBackend(
            api_key=rm.get("api_key") or fallback_key, model=rm["model"], base_url=rm.get("base_url", ""),
            think=bool(think) if think is not None else None,
            thinking_budget=int(rm.get("thinking_budget") or 0), effort=str(rm.get("effort") or ""),
            max_output_tokens=int(rm.get("max_output_tokens") or 0) or None,
        )
    from services.model_registry import tool_config

    return OpenAICompatibleChatBackend(
        base_url=rm.get("base_url", ""), api_key=rm.get("api_key", ""), model=rm["model"],
        max_output_tokens=int(rm.get("max_output_tokens") or 0) or None,
        think=bool(think) if think is not None else None, **tool_config(rm),
    )


async def try_once(rm: dict, user: str, token: str, expert, question: str, skill_block: str,
                   lang: str) -> str:
    """One answer of `expert` to `question`, read-only tools, with or without
    a skill block. Returns the answer text (or an error line)."""
    from config.chat_prompts import build_system_prompt
    from services import chat_service as cs
    from services.chat_service import DoneEvent, TextTokenEvent
    from services.expert_prompt import scope_block

    cs._current_owner.set(user)
    cs._current_token.set(token)
    cs._current_scope.set(None if expert.builtin else expert.scope)
    cs._current_skills.set([])
    blocks = [b for b in (scope_block(expert), skill_block) if b]
    system = build_system_prompt({"documents", "calculate", "memory", "vault"}, user, lang,
                                 custom_instructions=expert.personality, expert_blocks=blocks)
    tools = [t for t in cs.TOOL_DEFINITIONS if t["name"] in TRY_TOOLS]
    text = []
    try:
        async for ev in make_backend(rm).run_turn(
            [{"role": "user", "content": question}], system, float(rm.get("temperature") or 0.3),
            tools=tools, max_iterations=8, answer_language=lang,
        ):
            if isinstance(ev, TextTokenEvent):
                text.append(ev.text)
            elif isinstance(ev, DoneEvent):
                break
    except Exception as exc:  # shown in the trial column
        return f"*{type(exc).__name__}: {exc}*"
    return "".join(text).strip()
