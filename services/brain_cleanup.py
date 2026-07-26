"""services/brain_cleanup.py — LLM-powered memory cleanup ('Träumen').

Loads all brain facts for a user, sends them to an LLM, receives a list of
proposed cleanup actions (delete / update), returns them for UI review.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

from services.brain_service import BrainFact

_THINK_RE = re.compile(
    r"<think(?:ing)?>(.*?)</think(?:ing)?>", re.DOTALL | re.IGNORECASE
)
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

SYSTEM_PROMPT = "You are a memory curator. Analyze memory entries and propose cleanup actions.\n\nCheck in particular:\n1. Entries with nearly identical content → delete the less complete one (delete)\n2. Entries on the same topic → merge: update the more important one, delete the other\n3. Entries with overly long text → shorten (update)\n4. Entries with wrong, missing or excessive tags → fix tags (update_tags)\n5. Clearly outdated or redundant entries → delete (delete)\n\nActions:\n- delete:       {\"action\": \"delete\",       \"fact_id\": \"N\", \"reason\": \"...\"}\n- update:       {\"action\": \"update\",       \"fact_id\": \"N\", \"reason\": \"...\", \"new_text\": \"...\"}\n- update_tags:  {\"action\": \"update_tags\",  \"fact_id\": \"N\", \"reason\": \"...\", \"new_tags\": [\"tag1\", \"tag2\"]}\n\nRules:\n- fact_id = the number in square brackets (e.g. \"0\", \"3\")\n- When merging: update the more complete entry, delete the other\n- Respond ONLY with JSON:\n\n{\"actions\": [\n  {\"action\": \"delete\", \"fact_id\": \"3\", \"reason\": \"duplicate of [1]\"},\n  {\"action\": \"update\", \"fact_id\": \"0\", \"reason\": \"text too long\", \"new_text\": \"...\"},\n  {\"action\": \"update_tags\", \"fact_id\": \"5\", \"reason\": \"tags too generic\", \"new_tags\": [\"debeka\", \"bausparvertrag\"]}\n]}\n\nIf nothing to do: {\"actions\": []}"


@dataclass
class CleanupAction:
    action: str  # "delete" | "update" | "update_tags"
    fact_id: str  # real UUID
    fact_idx: str  # display index "[N]" shown in UI
    reason: str
    original_text: str
    original_tags: list = None  # type: ignore[assignment]
    new_text: str | None = None  # for "update"
    new_tags: list | None = None  # for "update_tags"
    selected: bool = True


async def run(
    facts: list[BrainFact],
    *,
    llm: Callable[..., Awaitable[str]],
    system_prompt: str | None = None,
) -> tuple[list[CleanupAction], str]:
    """Returns (actions, llm_raw_response)."""
    """Send all facts to LLM, return proposed cleanup actions.

    Args:
        facts:         All brain facts for the user.
        llm:           Bound complete() callable (create_llm result).
        system_prompt: Optional override.

    Returns:
        List of CleanupAction, pre-selected=True. Empty list if nothing to do.
    """
    if not facts:
        return [], ""

    _MAX_FACT_CHARS = 400  # truncate long facts in the LLM prompt

    prompt = system_prompt or SYSTEM_PROMPT
    # Use short numeric keys in the prompt to avoid UUID confusion
    idx_to_fact = {str(i): f for i, f in enumerate(facts)}

    lines = []
    for idx, f in idx_to_fact.items():
        tag_str = ", ".join(f.tags) if f.tags else "–"
        text_preview = (
            f.text if len(f.text) <= _MAX_FACT_CHARS else f.text[:_MAX_FACT_CHARS] + "…"
        )
        lines.append(f"[{idx}]\nText: {text_preview}\nTags: {tag_str}")
    facts_block = "\n\n".join(lines)

    user_content = (
        f"Memory entries ({len(facts)} entries):\n{facts_block}\n\n"
        'Respond with JSON. Use the numbers in square brackets as fact_id (e.g. "0", "3").'
    )
    raw = await llm(
        prompt,
        [{"role": "user", "content": user_content}],
        max_tokens=64_000,
        temperature=0.1,
        think=False,  # suppress reasoning tokens — we need structured JSON, not CoT
    )

    # Strip thinking tokens
    cleaned = _THINK_RE.sub("", raw).strip()

    m = _JSON_RE.search(cleaned)
    if not m:
        raise ValueError(f"No JSON found in LLM response. Raw output:\n{raw[:800]}")

    try:
        data = json.loads(m.group())
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"JSON-Parsing fehlgeschlagen: {exc}\nJSON-Kandidat:\n{m.group()[:400]}"
        ) from exc

    actions: list[CleanupAction] = []
    for item in data.get("actions", []):
        idx = str(item.get("fact_id", "")).strip()
        action = str(item.get("action", "")).strip()
        fact = idx_to_fact.get(idx)
        if not fact or action not in ("delete", "update", "update_tags"):
            continue

        new_text = str(item.get("new_text", "")).strip() if action == "update" else None
        if action == "update" and not new_text:
            continue

        raw_tags = item.get("new_tags")
        new_tags = (
            [str(t).strip() for t in raw_tags if str(t).strip()]
            if isinstance(raw_tags, list)
            else None
        )
        if action == "update_tags" and not new_tags:
            continue

        actions.append(
            CleanupAction(
                action=action,
                fact_id=fact.id,
                fact_idx=f"[{idx}]",
                reason=str(item.get("reason", "")).strip(),
                original_text=fact.text,
                original_tags=list(fact.tags),
                new_text=new_text,
                new_tags=new_tags,
            )
        )

    return actions, cleaned


async def apply(
    actions: list[CleanupAction],
    *,
    brain,  # kept for backwards compat signature; unused
) -> tuple[int, int]:
    """Apply selected actions. Returns (deleted, updated) counts."""
    from services.clients import vault_brain_writer
    deleted = updated = 0
    for a in actions:
        if not a.selected:
            continue
        if a.action == "delete":
            await vault_brain_writer.delete_memory(a.fact_id)
            deleted += 1
        elif a.action == "update" and a.new_text:
            await vault_brain_writer.update_memory(a.fact_id, a.new_text)
            updated += 1
        elif a.action == "update_tags" and a.new_tags is not None:
            await vault_brain_writer.update_tags(a.fact_id, a.new_tags)
            updated += 1
    return deleted, updated
