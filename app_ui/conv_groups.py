"""How the chat's conversation drawer sorts its list (`app_ui/pages/chat.py`).

Two ways, the user's choice (pref `chat_hist_group`): by expert — each expert
a section, the one used last on top — or by date (today, yesterday, the last
seven days, the last 30 days, older). Within a section the newest comes first,
which is how `list_conversations` returns them.

A search narrows the list first: the title, or any message of the
conversation (`chat_history_service.search_conversations`).

Pure functions; labels are keys, translated where they are shown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

MODES = ("expert", "date")
DATE_BUCKETS = ("today", "yesterday", "week", "month", "older")
UNIVERSAL = "universal"


@dataclass
class Group:
    key: str  # expert id, or one of DATE_BUCKETS
    convs: list[dict] = field(default_factory=list)


def _when(conv: dict, tz) -> datetime | None:
    try:
        return datetime.fromisoformat(str(conv.get("updated_at") or "")).astimezone(tz)
    except ValueError:
        return None


def date_bucket(conv: dict, now: datetime) -> str:
    dt = _when(conv, now.tzinfo)
    if dt is None:
        return "older"
    days = (now.date() - dt.date()).days
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return "week"
    if days < 30:
        return "month"
    return "older"


def matches(conv: dict, text: str, content_hits: set[str]) -> bool:
    t = text.strip().lower()
    return not t or t in str(conv.get("title") or "").lower() or conv.get("id") in content_hits


def group(convs: list[dict], mode: str, now: datetime) -> list[Group]:
    """Sections in display order; empty ones left out."""
    if mode == "expert":
        out: dict[str, Group] = {}
        for c in convs:  # newest first, so a section's place is its newest chat
            key = c.get("expert_id") or UNIVERSAL
            out.setdefault(key, Group(key)).convs.append(c)
        return list(out.values())
    buckets = {k: Group(k) for k in DATE_BUCKETS}
    for c in convs:
        buckets[date_bucket(c, now)].convs.append(c)
    return [g for g in buckets.values() if g.convs]


def context_kind(conv: dict) -> str:
    """What a conversation is about, for its badge: "finding", "dossier",
    "overview" (the findings dock) — or "" for a plain chat."""
    ctx = str(conv.get("context") or "")
    if ctx.startswith("finding:"):
        return "finding"
    if ctx.startswith("dossier:"):
        return "dossier"
    if ctx == "findings:overview":
        return "overview"
    return ""
