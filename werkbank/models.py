"""werkbank/models.py — pure data containers, no logic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Archetype:
    id: int
    user_id: str
    name: str
    description: str
    soul_text: str
    enabled_tools: list[str]  # subset of TOOL_DEFINITIONS names
    created_at: datetime
    updated_at: datetime
