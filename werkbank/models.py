"""werkbank/models.py — pure data containers, no logic."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class TaskStatus(str, Enum):
    DRAFT = "DRAFT"
    TRIAGE = "TRIAGE"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    AWAITING_REVIEW = "AWAITING_REVIEW"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class SubTaskStatus(str, Enum):
    TODO = "TODO"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


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


@dataclass
class Task:
    id: int
    user_id: str
    original_request: str
    refined_request: str
    status: TaskStatus
    model: str  # maps to backend/lane
    result_md: str | None
    paperless_id: int | None
    paperless_url: str | None
    short_title: str | None
    started_at: datetime | None
    created_at: datetime
    updated_at: datetime
    language: str = "en"


@dataclass
class SubTask:
    id: int
    task_id: int
    archetype_id: int | None  # None = system role (Synthesizer etc.)
    user_id: str              # denormalised: defence-in-depth
    instruction: str
    success_criteria: str
    status: SubTaskStatus
    depends_on: list[int]     # DB IDs of dependency SubTasks
    order_index: int
    result_raw: str | None
    result_compacted: str | None
    critic_verdict: str | None
    retry_count: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass
class SubTaskSpec:
    """Transient: output of Splitter before DB insert. Uses temp refs, not IDs."""
    ref: str
    instruction: str
    archetype: str            # archetype name
    success_criteria: str
    depends_on: list[str]     # temp refs
    order_index: int = 0
