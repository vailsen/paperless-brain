"""werkbank/roles/planner.py — reformulates a raw user request into a precise goal."""

from __future__ import annotations

import json
import re
from typing import Callable, Awaitable

DEFAULT_SYSTEM_PROMPT = """\
You are a planning assistant for autonomous research tasks.

Your job: rephrase the raw user request into a precise, self-contained work order.

Rules:
- Clarify the goal: what should be known or produced in the end?
- Name explicitly which sources should be used \
(documents, web, emails, calendar — only the relevant ones).
- State what makes a good result.
- Keep it short: max. 3–5 sentences.
- Do NOT decompose into sub-tasks — that is the next step's job.
- Write "title" and "refined" in the same language as the user request.

Respond exclusively with a JSON object, no explanation or preamble:
{"title": "<short title, max. 6 words>", "refined": "<rephrased work order>"}\
"""

_THINK_RE = re.compile(r"<think(?:ing)?>(.*?)</think(?:ing)?>", re.DOTALL | re.IGNORECASE)
_JSON_RE  = re.compile(r"\{.*\}", re.DOTALL)


def derive_title(refined_request: str, original_request: str) -> str:
    """Fallback title: first 6 words of first sentence of refined_request."""
    text = (refined_request or original_request or "").strip()
    for sep in (".", "!", "?", "\n"):
        idx = text.find(sep)
        if idx > 10:
            text = text[:idx].strip()
            break
    words = text.split()[:6]
    title = " ".join(words)
    return title[:80] if title else (original_request[:60] if original_request else "Task")


async def run(
    original_request: str,
    *,
    llm: Callable[[str, list[dict]], Awaitable[str]],
    system_prompt: str | None = None,
) -> tuple[str, str]:
    """Reformulate original_request. Returns (refined_request, short_title)."""
    if system_prompt is None:
        from werkbank.settings_store import get_prompt, PROMPT_PLANNER
        system_prompt = get_prompt(PROMPT_PLANNER, DEFAULT_SYSTEM_PROMPT)
    from datetime import date

    messages = [
        {
            "role": "user",
            "content": f"Today's date: {date.today().isoformat()}\n\n{original_request}",
        }
    ]
    from werkbank.settings_store import get_tokens, TOKENS_PLANNER
    raw = await llm(system_prompt, messages, max_tokens=get_tokens(TOKENS_PLANNER), temperature=0.3, think=False)

    # Strip thinking tokens (qwen3 etc.)
    cleaned = _THINK_RE.sub("", raw).strip()

    # Parse JSON response
    m = _JSON_RE.search(cleaned)
    if m:
        try:
            data = json.loads(m.group())
            refined = str(data.get("refined", "")).strip()
            title   = str(data.get("title",   "")).strip()
            if refined:
                return refined, title or derive_title(refined, original_request)
        except (json.JSONDecodeError, KeyError):
            pass

    # Fallback: treat entire output as refined, derive title
    refined = cleaned or original_request
    return refined, derive_title(refined, original_request)
