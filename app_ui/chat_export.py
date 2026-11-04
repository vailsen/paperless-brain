"""The whole conversation as one Markdown file ("Download conversation")."""

from __future__ import annotations

from datetime import datetime


def conversation_markdown(messages: list[dict], *, you: str, assistant: str, now: datetime | None = None) -> str:
    """User and assistant turns in order, each under its own heading.

    Document citations become readable references (`[#241 p3]`) — the pills
    mean nothing outside the chat; web sources are already links. Empty
    string when there is nothing to export."""
    from services.dossier_merge import plain_refs
    from services.web_citations import plain_web_refs

    turns = [m for m in messages or [] if m.get("role") in ("user", "assistant")
             and isinstance(m.get("content"), str) and m["content"].strip()]
    if not turns:
        return ""
    first = next((m["content"] for m in turns if m["role"] == "user"), turns[0]["content"])
    title = " ".join(first.split())[:80]
    out = [f"# {title}", "", f"*{(now or datetime.now()):%Y-%m-%d %H:%M}*", ""]
    for m in turns:
        who = you if m["role"] == "user" else assistant
        out += [f"## {who}", "", plain_refs(plain_web_refs(m["content"].strip())), ""]
    return "\n".join(out).rstrip() + "\n"
