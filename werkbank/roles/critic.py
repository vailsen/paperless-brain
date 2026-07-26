"""werkbank/roles/critic.py — groundedness gate for sub-task results.

Checks whether a raw result satisfies the success_criteria.
Returns a boolean verdict plus actionable feedback for retries.
This is a groundedness/completeness check, not a quality TÜV.

Verdicts are produced via forced tool-use (complete_structured), so weak local
models cannot bypass the gate by answering in prose. If the structured call
fails twice, the result is accepted but visibly flagged as unreviewed —
blocking all progress on critic infrastructure failure would violate the
failed-policy ("weitermachen").
"""

from __future__ import annotations

# Raw results larger than this are truncated for the critic call so small
# local context windows judge a coherent excerpt instead of silent truncation.
_MAX_RESULT_CHARS = 24_000

_STRUCTURED_ATTEMPTS = 2

UNREVIEWED_FEEDBACK = (
    "[unreviewed] Critic verdict not parseable — result accepted without review."
)

VERDICT_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["ok", "redo"]},
        "feedback": {"type": "string"},
    },
    "required": ["verdict", "feedback"],
}

_TOOL_NAME = "submit_verdict"

# Moved to Settings in Phase 7.
DEFAULT_SYSTEM_PROMPT = """\
You are a critical reviewer of research results.
Check whether the given result meets the success criteria and report your
verdict via the submit_verdict tool.

Rules:
- "ok" only when the success criteria are clearly met.
- "redo" when essential information is missing, the task was misunderstood,
  or the result is empty/meaningless.
- Today's date is stated in the request. Judge the recency and plausibility
  of dates ONLY relative to that date — NEVER by your training knowledge.
- Web research results can be newer than your knowledge. Do NOT reject a result
  because events, dates or years seem unfamiliar to you —
  instead check whether sources (URLs/senders) are cited.
- Don't be a perfectionist: if the core question is answered, "ok" is correct.
- On "redo": name CONCRETELY in the feedback which criterion is not met
  and what the next attempt must do differently.
- ALWAYS fill in feedback — even on "ok" give a short justification (1 sentence)
  so the user can understand why the self-check passed.
- Write the feedback in the same language as the task.\
"""


async def run(
    instruction: str,
    success_criteria: str,
    raw_result: str,
    *,
    model: str,
    user_id: str,
    token: str,
    system_prompt: str | None = None,
) -> tuple[bool, str]:
    """Check whether raw_result meets success_criteria for the given instruction.

    Args:
        instruction:       The original sub-task instruction.
        success_criteria:  The Splitter's verifiable criterion.
        raw_result:        The Worker's output.
        model:             LLM model (determines backend + lane).
        user_id:           Paperless username.
        token:             Paperless session token.
        system_prompt:     Override for Settings integration (Phase 7).

    Returns:
        (ok, feedback) — ok=True means the result passes; feedback guides retry.
    """
    from werkbank.llm_lane import complete_structured
    from werkbank.settings_store import (
        PROMPT_CRITIC, TOKENS_CRITIC, get_prompt, get_tokens,
    )

    if system_prompt is None:
        system_prompt = get_prompt(PROMPT_CRITIC, DEFAULT_SYSTEM_PROMPT)

    excerpt = raw_result
    if len(excerpt) > _MAX_RESULT_CHARS:
        excerpt = excerpt[:_MAX_RESULT_CHARS] + "\n[… truncated for review …]"

    from datetime import date

    user_content = (
        f"Today's date: {date.today().isoformat()}\n\n"
        f"Task: {instruction}\n\n"
        f"Success criterion: {success_criteria}\n\n"
        f"Result:\n{excerpt}"
    )
    messages = [{"role": "user", "content": user_content}]

    for _ in range(_STRUCTURED_ATTEMPTS):
        try:
            data = await complete_structured(
                system_prompt,
                messages,
                model=model,
                user_id=user_id,
                token=token,
                json_schema=VERDICT_JSON_SCHEMA,
                tool_name=_TOOL_NAME,
                max_tokens=get_tokens(TOKENS_CRITIC),
                temperature=0.1,
            )
            verdict = str(data.get("verdict", "")).strip().lower()
            feedback = str(data.get("feedback", "")).strip()
            if verdict in ("ok", "redo"):
                return verdict == "ok", feedback
        except Exception as exc:
            print(f"[critic] structured verdict failed: {exc}")

    # Critic infrastructure broken (model can't do forced tool-use, backend
    # error, …) — accept, but flag so the verdict is visible in the UI.
    return True, UNREVIEWED_FEEDBACK
