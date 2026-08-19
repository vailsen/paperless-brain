"""Werkbank v2 — the tool loop a runner executes in.

Mirrors the message formatting of `werkbank/roles/worker.py` (three backends
speak three dialects of "here is a tool result") but routes every execution
through a `ToolBelt`, so the retrieved text is captured and `trust` is assigned
at the point of retrieval.

The duplication with v1's worker is deliberate and temporary: v1 stays running
until Phase 7, and refactoring the loop it depends on to serve v2 would put the
working module at risk for no gain. It goes when v1 does.

The loop only *gathers*. It never asks for the answer: the model explores with
tools, and the structured `SubtaskResult` is a separate call afterwards, made
against a schema. Mixing the two is how you get prose with a JSON block stapled
to the end.
"""

from __future__ import annotations

import json
import logging

from werkbank.v2.tools import ToolBelt

_log = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 10


async def gather(
    system: str,
    task: str,
    belt: ToolBelt,
    *,
    model: str,
    user_id: str,
    token: str,
    temperature: float = 0.4,
    max_iterations: int = MAX_TOOL_ITERATIONS,
) -> str:
    """Let the model work with its tools. Returns its closing text.

    That text is scratch — a place for the model to think out loud. What
    survives is the tool log and, afterwards, the structured facts.
    """
    from werkbank.llm_lane import (
        complete_turn,
        is_anthropic_backend,
        is_openai_compat_model,
    )

    anthropic = is_anthropic_backend(model, user_id, token)
    oai_compat = not anthropic and is_openai_compat_model(model, user_id, token)
    definitions = belt.definitions()

    messages: list[dict] = [{"role": "user", "content": task}]
    last_text = ""

    for _ in range(max_iterations):
        text, tool_calls, stop_reason = await complete_turn(
            system,
            messages,
            model=model,
            user_id=user_id,
            token=token,
            tools=definitions,
            temperature=temperature,
        )
        last_text = text or last_text
        if stop_reason == "end_turn" or not tool_calls:
            return last_text

        messages.append(_assistant_message(text, tool_calls, anthropic, oai_compat))

        results = []
        for call in tool_calls:
            output = await belt.execute(call["name"], call.get("input") or {})
            results.append((call, output))
        messages.extend(_result_messages(results, anthropic, oai_compat))

    _log.info("werkbank v2: tool loop hit its iteration cap")
    return last_text


def _assistant_message(
    text: str, tool_calls: list[dict], anthropic: bool, oai_compat: bool
) -> dict:
    if anthropic:
        content: list[dict] = []
        if text:
            content.append({"type": "text", "text": text})
        content += [
            {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]}
            for c in tool_calls
        ]
        return {"role": "assistant", "content": content}
    if oai_compat:
        return {
            "role": "assistant",
            "content": text or "",
            "tool_calls": [
                {
                    "id": c["id"],
                    "type": "function",
                    "function": {
                        "name": c["name"],
                        "arguments": json.dumps(c["input"], ensure_ascii=False),
                    },
                }
                for c in tool_calls
            ],
        }
    return {
        "role": "assistant",
        "content": text or "",
        "tool_calls": [
            {"function": {"name": c["name"], "arguments": c["input"]}} for c in tool_calls
        ],
    }


def _result_messages(
    results: list[tuple[dict, str]], anthropic: bool, oai_compat: bool
) -> list[dict]:
    if anthropic:
        return [{
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": call["id"], "content": output}
                for call, output in results
            ],
        }]
    if oai_compat:
        return [
            {"role": "tool", "tool_call_id": call["id"], "content": output}
            for call, output in results
        ]
    return [{"role": "tool", "content": output} for _call, output in results]
