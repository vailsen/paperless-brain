"""Working on a stored conversation: remove messages, add notes, compact a
selection (the chat page's "Edit conversation", `app_ui/pages/chat.py`).

Pure functions over the stored message list (`{"role", "content"}` dicts, in
order). Everything keyed by a message's position — provenance in
`chat_turn_meta`, kept tool histories (`app_ui/tool_trace`) — moves with the
message: every edit returns an `index_map` (old position → new position; a
removed message has none), and the caller re-keys with it instead of dropping.

**A note** is the user's own words, stored as a user message wrapped in
`<pb-note>`: shown as a bar across the conversation, never as a question, and
sent to the model with a line saying it is a note (`for_model`). It costs no
model call when added.

**What the model is sent** is the stored list made regular (`for_model`):
notes framed, neighbours of the same role joined (removing one message can
leave two questions in a row), and a conversation that starts with an answer
(a summary, or after removing the first question) opened by a short user line —
the Anthropic API wants the first message from the user.
"""

from __future__ import annotations

import re

NOTE_OPEN, NOTE_CLOSE = "<pb-note>", "</pb-note>"
_NOTE_RE = re.compile(r"^\s*<pb-note>\n?(.*?)\n?</pb-note>\s*$", re.S)

#: What precedes a note's text for the model (English, like every prompt).
NOTE_PREFIX = ("[A note the user added to this conversation — their own words, for context. "
               "It is not a question; do not answer it, take it into account.]")
#: The opening user line when a conversation starts with an answer.
OPENING = "[The conversation so far.]"


def make_note(text: str) -> dict:
    return {"role": "user", "content": f"{NOTE_OPEN}\n{text.strip()}\n{NOTE_CLOSE}"}


def note_text(content: str) -> str | None:
    """The note's text, or None when the message is not a note."""
    m = _NOTE_RE.match(content or "")
    return m.group(1).strip() if m else None


def is_note(msg: dict) -> bool:
    return msg.get("role") == "user" and note_text(msg.get("content", "")) is not None


def for_model(messages: list[dict]) -> list[dict]:
    """The stored conversation as the model reads it (see module doc)."""
    out: list[dict] = []
    for m in messages:
        role, content = m.get("role"), m.get("content") or ""
        text = note_text(content) if role == "user" else None
        if text is not None:
            content = f"{NOTE_PREFIX}\n{text}"
        if out and out[-1]["role"] == role:
            out[-1] = {"role": role, "content": out[-1]["content"] + "\n\n" + content}
        else:
            out.append({"role": role, "content": content})
    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": OPENING})
    return out


def _rekey(meta: dict[int, dict], index_map: dict[int, int]) -> dict[int, dict]:
    return {index_map[i]: m for i, m in meta.items() if i in index_map}


def remove(messages: list[dict], meta: dict[int, dict], idxs: set[int]):
    """Without the messages at `idxs` → (messages, meta, index_map)."""
    keep = [i for i in range(len(messages)) if i not in idxs]
    index_map = {old: new for new, old in enumerate(keep)}
    return [messages[i] for i in keep], _rekey(meta, index_map), index_map


def insert(messages: list[dict], meta: dict[int, dict], at: int, msg: dict):
    """`msg` placed at position `at` (0 … len) → (messages, meta, index_map)."""
    at = max(0, min(at, len(messages)))
    index_map = {i: (i if i < at else i + 1) for i in range(len(messages))}
    return messages[:at] + [msg] + messages[at:], _rekey(meta, index_map), index_map


def summary_message(summary: str, heading: str) -> dict:
    return {"role": "assistant", "content": f"{heading}\n\n{summary.strip()}"}


def compact_prompt(messages: list[dict], language: str) -> tuple[str, str]:
    """(system, user) for summarising `messages` — English instructions, the
    summary in the conversation's own language (named, since the request
    itself is English)."""
    lines = []
    for m in messages:
        text = note_text(m["content"]) if m["role"] == "user" else None
        who = "Note by the user" if text is not None else ("User" if m["role"] == "user" else "Assistant")
        lines.append(f"{who}: {text if text is not None else m['content']}")
    system = ("You summarise conversations precisely. Answer with the summary only, no preamble. "
              f"Write it in {language}.")
    user = ("Summarise the following conversation very compactly. Keep every important fact, "
            "document id (#123), date, amount and result, and what the user's notes say. "
            f"Write the summary in {language}.\n\n" + "\n".join(lines))
    return system, user
