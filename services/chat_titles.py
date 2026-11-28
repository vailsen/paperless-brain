"""Conversation titles written by the chat model.

A new conversation is named after the user's first message — often a poor
name ("and what about the other one?"). After the first answer, and again
after the third, the conversation's own model gives it a short title in one
call without tools. A title the user set ("Rename") is never replaced:
`chat_history_service.set_auto_title` checks `title_source` in the same
statement that writes.
"""

from __future__ import annotations

import re

#: After which answers the title is (re)written: the first answer says what
#: the conversation is about, the third what it turned into.
RETITLE_AFTER = (1, 3)

#: What the model reads: the first question and the newest exchange, capped.
EXCERPT_CHARS = 2400
MAX_TITLE_CHARS = 60

SYSTEM = (
    "You name conversations for a list of chats. Answer with the title only: "
    "3 to 7 words, no quotes, no full stop, no emoji, no Markdown. Say what the "
    "conversation is about (the topic, a document, a person or a matter), not "
    "that it is a question or a chat. Write it in the language the user writes in."
)


def due(answers: int) -> bool:
    """Whether a conversation with this many answers gets a (new) title."""
    return answers in RETITLE_AFTER


def answer_count(messages: list[dict]) -> int:
    return sum(1 for m in messages
               if m.get("role") == "assistant" and isinstance(m.get("content"), str) and m["content"].strip())


def _plain(text: str) -> str:
    """One line, reference markers out (`[^241-p3]` says nothing about the topic)."""
    from services.citations import CITE_RE

    text = CITE_RE.sub("", text or "")
    return re.sub(r"\s+", " ", text).strip()


def build_request(messages: list[dict]) -> str:
    """The user message of the title call: the first question, then the
    newest messages that still fit — oldest first."""
    from services.chat_attachments import strip_for_title

    lines: list[str] = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        role = m.get("role")
        text = _plain(strip_for_title(content) if role == "user" else content)
        if text:
            lines.append(f"{'User' if role == 'user' else 'Assistant'}: {text}")
    if not lines:
        return ""
    first, rest = lines[0][:600], lines[1:]
    budget = EXCERPT_CHARS - len(first)
    tail: list[str] = []
    for line in reversed(rest):
        if budget <= 0:
            break
        piece = line if len(line) <= budget else line[:budget] + " …"
        tail.append(piece)
        budget -= len(piece)
    body = "\n\n".join([first, *reversed(tail)])
    return f"Conversation:\n\n{body}\n\nThe title:"


_PREFIX_RE = re.compile(r"^(?:title|titel|titre|tytuł)\s*[:：-]\s*", re.I)


def clean(raw: str) -> str | None:
    """The model's answer as a title, or None when it is no usable title."""
    text = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S | re.I)
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if not lines:
        return None
    t = lines[0]
    t = re.sub(r"^#+\s*", "", t)
    t = _PREFIX_RE.sub("", t)
    t = t.replace("**", "").replace("__", "").replace("`", "")
    while True:  # quotes and a full stop, in whichever order the model put them
        stripped = t.strip(" \t\"'“”„‚‘’«»*_-").rstrip(".!?:;,").strip()
        if stripped == t:
            break
        t = stripped
    t = re.sub(r"\s+", " ", t)
    if not t or len(t.split()) > 12:
        return None
    if len(t) > MAX_TITLE_CHARS:
        t = t[:MAX_TITLE_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:-") or t[:MAX_TITLE_CHARS]
    return t
