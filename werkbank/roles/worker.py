"""werkbank/roles/worker.py — executes one sub-task using its archetype's tool subset.

Runs an agentic loop (up to MAX_WORKER_ITERATIONS) using complete_turn():
  1. LLM decides what to do.
  2. If stop_reason == tool_use: execute tools, append results, repeat.
  3. If stop_reason == end_turn: return final text.

Context vars _current_owner / _current_token from chat_service are set for the
duration of the call so user-scoped tools (brain, IMAP, CalDAV) work correctly.
"""

from __future__ import annotations

MAX_WORKER_ITERATIONS = 12


async def run(
    instruction: str,
    soul_text: str,
    tool_subset: list[dict],
    dep_results: list[str],
    *,
    model: str,
    user_id: str,
    token: str,
) -> str:
    """Execute the sub-task described by instruction using the archetype's tools.

    Args:
        instruction:  Sub-task instruction text.
        soul_text:    Archetype system prompt.
        tool_subset:  TOOL_DEFINITIONS entries filtered to enabled_tools.
        dep_results:  Compacted results from dependency sub-tasks.
        model:        LLM model name (determines backend + lane).
        user_id:      Paperless username (for user-scoped tools).
        token:        Paperless session token (for credential_store).

    Returns:
        Raw result text (before Critic review and compaction).
    """
    import json as _json

    from services.chat_service import (
        _current_owner,
        _current_token,
        execute_tool,
    )
    from werkbank.llm_lane import complete_turn, is_anthropic_backend, is_openai_compat_model

    _anthropic_fmt = is_anthropic_backend(model, user_id, token)
    _oai_compat = not _anthropic_fmt and is_openai_compat_model(model, user_id, token)

    # Scope context vars so user-scoped tools work correctly
    owner_tok = _current_owner.set(user_id)
    cred_tok = _current_token.set(token)

    try:
        context_block = ""
        if dep_results:
            parts = "\n\n".join(f"- {r}" for r in dep_results if r.strip())
            context_block = f"\n\n## Kontext aus vorherigen Teilaufgaben:\n{parts}"

        from datetime import date

        messages = [
            {
                "role": "user",
                "content": f"Heutiges Datum: {date.today().isoformat()}\n\n"
                f"## Aufgabe:\n{instruction}{context_block}",
            }
        ]
        last_text = ""

        for _ in range(MAX_WORKER_ITERATIONS):
            text, tool_calls, stop_reason = await complete_turn(
                soul_text,
                messages,
                model=model,
                user_id=user_id,
                token=token,
                tools=tool_subset,
            )
            last_text = text or last_text

            if stop_reason == "end_turn" or not tool_calls:
                return last_text or "Keine Ergebnisse ermittelt."

            # Append assistant message (format differs per backend)
            if _anthropic_fmt:
                # Anthropic format (claude-* or registry backend=anthropic)
                content: list[dict] = []
                if text:
                    content.append({"type": "text", "text": text})
                for tc in tool_calls:
                    content.append(
                        {
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["name"],
                            "input": tc["input"],
                        }
                    )
                messages.append({"role": "assistant", "content": content})
            elif _oai_compat:
                # OpenAI-compatible format (MiniMax, Moonshot, Ollama /v1, …)
                oai_calls = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": _json.dumps(tc["input"], ensure_ascii=False),
                        },
                    }
                    for tc in tool_calls
                ]
                messages.append(
                    {
                        "role": "assistant",
                        "content": text or "",
                        "tool_calls": oai_calls,
                    }
                )
            else:
                # Ollama native format
                ollama_calls = [
                    {"function": {"name": tc["name"], "arguments": tc["input"]}}
                    for tc in tool_calls
                ]
                messages.append(
                    {
                        "role": "assistant",
                        "content": text or "",
                        "tool_calls": ollama_calls,
                    }
                )

            # Execute tools and append results
            if _anthropic_fmt:
                # Anthropic tool_result block
                tool_result_contents: list[dict] = []
                for tc in tool_calls:
                    result_text, _, _ = await execute_tool(tc["name"], tc["input"])
                    tool_result_contents.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tc["id"],
                            "content": result_text,
                        }
                    )
                messages.append({"role": "user", "content": tool_result_contents})
            elif _oai_compat:
                # OpenAI-compatible: one tool message per call, with tool_call_id
                for tc in tool_calls:
                    result_text, _, _ = await execute_tool(tc["name"], tc["input"])
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": result_text,
                        }
                    )
            else:
                # Ollama native: simple role=tool, no id
                for tc in tool_calls:
                    result_text, _, _ = await execute_tool(tc["name"], tc["input"])
                    messages.append({"role": "tool", "content": result_text})

        # Hit iteration cap — force a final summary call using whatever was collected
        if messages:
            try:
                summary_text, _, _ = await complete_turn(
                    soul_text
                    + "\n\nNow summarize ALL information found so far, completely.",
                    messages,
                    model=model,
                    user_id=user_id,
                    token=token,
                    tools=[],  # no tools — force text output
                    max_tokens=12_000,
                    temperature=0.3,
                )
                if summary_text and summary_text.strip():
                    return summary_text.strip()
            except Exception:
                pass
        return last_text or "No usable results after maximum iterations."

    finally:
        _current_owner.reset(owner_tok)
        _current_token.reset(cred_tok)
