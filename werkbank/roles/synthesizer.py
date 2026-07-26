"""werkbank/roles/synthesizer.py — produces the final Markdown document.

Receives all sub-task results (including failed ones) and synthesises them
into a coherent, structured Markdown document. Gets no tools.
"""

from __future__ import annotations

from typing import Callable, Awaitable

# Moved to Settings in Phase 7.
DEFAULT_SYSTEM_PROMPT = """\
You are a synthesis assistant for multi-step research tasks.

Your job: combine all partial results into a coherent, structured
Markdown document that answers the original question.

Rules:
- Start with a short summary: order + core result (2–3 sentences).
- Structure the document clearly: headings (##), bullet lists, tables where useful.
- Reference sources (document IDs, URLs) when the partial results name them.
- If a sub-step failed (marked "[ERROR]" in the result):
  note the gap explicitly with a hint about what could not be determined.
- No boilerplate, no filler sentences — only what the partial results actually support.\
"""


def _format_subtask_block(index: int, item: dict) -> str:
    """Format one sub-task result for the Synthesizer's context window."""
    status = item.get("status", "DONE")
    result = item.get("result_compacted") or item.get("result_raw") or ""
    instruction = item.get("instruction", "")

    if status == "FAILED" or not result:
        result = "[ERROR] No reliable data determined."

    return (
        f"### Sub-task {index + 1}: {instruction}\n"
        f"{result}"
    )


async def run(
    task_goal: str,
    subtask_results: list[dict],
    *,
    llm: Callable[[str, list[dict]], Awaitable[str]],
    system_prompt: str | None = None,
    language: str = "en",
) -> str:
    """Synthesise all sub-task results into a final Markdown document.

    Args:
        task_goal:        The refined_request from agent_tasks.
        subtask_results:  List of dicts with keys: instruction, status,
                          result_compacted, result_raw.
        llm:              Bound complete() callable (no tools).
        system_prompt:    Override for Settings integration (Phase 7).

    Returns:
        Final Markdown string for agent_tasks.result_md.
    """
    if system_prompt is None:
        from werkbank.settings_store import get_prompt, PROMPT_SYNTHESIZER
        system_prompt = get_prompt(PROMPT_SYNTHESIZER, DEFAULT_SYSTEM_PROMPT)
    from i18n import language_directive
    system_prompt = f"{system_prompt}\n\n{language_directive(language)}"
    blocks = "\n\n".join(
        _format_subtask_block(i, item) for i, item in enumerate(subtask_results)
    )
    user_content = (
        f"## Original order\n{task_goal}\n\n"
        f"## Partial results\n\n{blocks}"
    )
    messages = [{"role": "user", "content": user_content}]
    from werkbank.settings_store import get_tokens, TOKENS_SYNTHESIZER
    result = await llm(system_prompt, messages, max_tokens=get_tokens(TOKENS_SYNTHESIZER), temperature=0.4)
    return result.strip()
