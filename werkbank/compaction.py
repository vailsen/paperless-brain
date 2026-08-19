"""DEPRECATED — v1 execution path, retired 2026-08-18.

Nothing imports this any more: `/werkbank` is `werkbank/v2/ui/page.py`, the chat
hand-off goes to `werkbank.v2.pipeline`, and `main.py` no longer starts the v1
scheduler. The files stay on disk for exactly one release because
`docs/werkbank-tasks.md` (Phase 7) conditions removal on v2 passing a complete
run on real data, and that run has not happened yet — the first real run *is*
the test. If it fails, re-adding two imports in `main.py` brings v1 back.

Delete this module once v2 has completed a run against the live archive.

werkbank/compaction.py — condenses raw Worker output for downstream sub-tasks.

Compacted result is what dependency sub-tasks see as context. Keeps all
concrete facts, numbers, source references. Strips verbose explanations.
"""

from __future__ import annotations

from typing import Awaitable, Callable

_MAX_COMPACTED_TOKENS = 600  # target word budget (rough guide for the LLM)

# Moved to Settings in Phase 7.
DEFAULT_SYSTEM_PROMPT = """\
You are a compaction assistant for research results.

Summarize the given result concisely — with respect to the overarching goal.

Rules:
- Keep all concrete facts, numbers, dates, document IDs (#NNN) and URLs.
- Shorten to max. 600 words.
- Keep the language of the original result.
- No introductory sentence ("Here is a summary…"), no closing sentence.
- Respond ONLY with the compacted text.\
"""


async def run(
    raw_result: str,
    goal: str,
    *,
    llm: Callable[[str, list[dict]], Awaitable[str]],
    system_prompt: str | None = None,
) -> str:
    """Compact raw Worker output to a goal-relevant summary.

    Args:
        raw_result:    The Worker's full output (after Critic approval).
        goal:          The task's refined_request — anchors relevance judgment.
        llm:           Bound complete() callable (no tools).
        system_prompt: Override for Settings integration (Phase 7).

    Returns:
        Compacted text. Falls back to truncated raw_result on LLM error.
    """
    # Skip LLM if already short enough
    if system_prompt is None:
        from werkbank.settings_store import PROMPT_COMPACTION, get_prompt

        system_prompt = get_prompt(PROMPT_COMPACTION, DEFAULT_SYSTEM_PROMPT)

    if len(raw_result.split()) <= _MAX_COMPACTED_TOKENS:
        return raw_result

    user_content = f"Ziel: {goal}\n\nZu kompaktierendes Ergebnis:\n{raw_result}"
    messages = [{"role": "user", "content": user_content}]

    try:
        from werkbank.settings_store import get_tokens, TOKENS_COMPACTION
        result = await llm(
            system_prompt, messages, max_tokens=get_tokens(TOKENS_COMPACTION), temperature=0.2, think=False
        )
        return result.strip() or raw_result
    except Exception:
        # Fallback: hard truncate rather than lose everything
        words = raw_result.split()
        return " ".join(words[:_MAX_COMPACTED_TOKENS]) + " …"
