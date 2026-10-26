"""Reactions from every channel — Telegram buttons, email replies, the app.

One entry point so the rules are the same whichever channel a reaction comes
from. The finding ledger arrives in Phase 4; until then no finding has an
owner, so every reaction is ignored.
"""

from __future__ import annotations


def owner_of(finding_id: str) -> str | None:
    return None


async def react(user: str, finding_id: str, action: str, *, channel: str) -> str:
    return ""
