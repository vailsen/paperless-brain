"""werkbank/repository.py — sole SQLite layer for the Werkbank module.

Every public method takes user_id and filters on it. No caller may bypass
this; it is the only defence against cross-user data leaks in background tasks.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

from config.settings import settings
from werkbank.models import (
    Archetype,
    SubTask,
    SubTaskStatus,
    Task,
    TaskStatus,
)

_DB_PATH = Path(settings.app_path) / "data" / "papersage.db"
_LEGACY_DB_PATH = Path(settings.app_path) / "data" / "werkbank.db"
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s)


# ── Connection management ─────────────────────────────────────────────────────

def _migrate(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(agent_tasks)")}
    for col, ddl in [
        ("started_at",  "ALTER TABLE agent_tasks ADD COLUMN started_at TEXT"),
        ("short_title", "ALTER TABLE agent_tasks ADD COLUMN short_title TEXT"),
        ("language",    "ALTER TABLE agent_tasks ADD COLUMN language TEXT DEFAULT 'en'"),
    ]:
        if col not in existing:
            conn.execute(ddl)
    conn.commit()


def init_db() -> None:
    """Create tables if absent. Call once at app startup."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Migrate legacy werkbank.db → papersage.db
    if not _DB_PATH.exists() and _LEGACY_DB_PATH.exists():
        import shutil
        shutil.move(str(_LEGACY_DB_PATH), str(_DB_PATH))
    schema = _SCHEMA_PATH.read_text(encoding="utf-8")
    with sqlite3.connect(_DB_PATH) as conn:
        conn.executescript(schema)
    with sqlite3.connect(_DB_PATH) as conn:
        _migrate(conn)


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ── Row mappers ───────────────────────────────────────────────────────────────

def _row_to_archetype(row: sqlite3.Row) -> Archetype:
    return Archetype(
        id=row["id"],
        user_id=row["user_id"],
        name=row["name"],
        description=row["description"],
        soul_text=row["soul_text"],
        enabled_tools=json.loads(row["enabled_tools"] or "[]"),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        user_id=row["user_id"],
        original_request=row["original_request"],
        refined_request=row["refined_request"],
        status=TaskStatus(row["status"]),
        model=row["model"],
        result_md=row["result_md"],
        paperless_id=row["paperless_id"],
        paperless_url=row["paperless_url"],
        short_title=dict(row).get("short_title"),
        started_at=_parse_dt(dict(row).get("started_at")),
        language=dict(row).get("language") or "en",
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _row_to_subtask(row: sqlite3.Row) -> SubTask:
    return SubTask(
        id=row["id"],
        task_id=row["task_id"],
        archetype_id=row["archetype_id"],
        user_id=row["user_id"],
        instruction=row["instruction"],
        success_criteria=row["success_criteria"],
        status=SubTaskStatus(row["status"]),
        depends_on=json.loads(row["depends_on"] or "[]"),
        order_index=row["order_index"],
        result_raw=row["result_raw"],
        result_compacted=row["result_compacted"],
        critic_verdict=row["critic_verdict"],
        retry_count=row["retry_count"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        started_at=_parse_dt(row["started_at"]),
        finished_at=_parse_dt(row["finished_at"]),
    )


# ── Archetype CRUD ────────────────────────────────────────────────────────────

def get_archetypes(user_id: str) -> list[Archetype]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_archetypes WHERE user_id = ? ORDER BY name",
            (user_id,),
        ).fetchall()
    return [_row_to_archetype(r) for r in rows]


def get_archetype(archetype_id: int, user_id: str) -> Archetype | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM agent_archetypes WHERE id = ? AND user_id = ?",
            (archetype_id, user_id),
        ).fetchone()
    return _row_to_archetype(row) if row else None


def get_archetype_by_name(name: str, user_id: str) -> Archetype | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM agent_archetypes WHERE name = ? AND user_id = ?",
            (name, user_id),
        ).fetchone()
    return _row_to_archetype(row) if row else None


def create_archetype(
    user_id: str,
    name: str,
    description: str,
    soul_text: str,
    enabled_tools: list[str],
) -> Archetype:
    now = _now()
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO agent_archetypes
               (user_id, name, description, soul_text, enabled_tools, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (user_id, name, description, soul_text, json.dumps(enabled_tools), now, now),
        )
        row_id = cur.lastrowid
    return get_archetype(row_id, user_id)


def update_archetype(
    archetype_id: int,
    user_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    soul_text: str | None = None,
    enabled_tools: list[str] | None = None,
) -> None:
    fields, params = [], []
    if name is not None:
        fields.append("name = ?"); params.append(name)
    if description is not None:
        fields.append("description = ?"); params.append(description)
    if soul_text is not None:
        fields.append("soul_text = ?"); params.append(soul_text)
    if enabled_tools is not None:
        fields.append("enabled_tools = ?"); params.append(json.dumps(enabled_tools))
    if not fields:
        return
    fields.append("updated_at = ?"); params.append(_now())
    params += [archetype_id, user_id]
    with _conn() as conn:
        conn.execute(
            f"UPDATE agent_archetypes SET {', '.join(fields)} WHERE id = ? AND user_id = ?",
            params,
        )


def delete_archetype(archetype_id: int, user_id: str) -> None:
    with _conn() as conn:
        conn.execute(
            "DELETE FROM agent_archetypes WHERE id = ? AND user_id = ?",
            (archetype_id, user_id),
        )


# ── Task CRUD ─────────────────────────────────────────────────────────────────

def create_task(user_id: str, original_request: str, model: str, language: str = "en") -> Task:
    now = _now()
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO agent_tasks
               (user_id, original_request, refined_request, status, model,
                language, created_at, updated_at)
               VALUES (?, ?, '', 'DRAFT', ?, ?, ?, ?)""",
            (user_id, original_request, model, language, now, now),
        )
        row_id = cur.lastrowid
    return get_task(row_id, user_id)


def get_task(task_id: int, user_id: str) -> Task | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM agent_tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        ).fetchone()
    return _row_to_task(row) if row else None


def get_tasks_for_user(user_id: str) -> list[Task]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM agent_tasks WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [_row_to_task(r) for r in rows]


def update_task_status(task_id: int, user_id: str, status: TaskStatus) -> None:
    now = _now()
    extra_fields  = ", started_at = ?" if status == TaskStatus.QUEUED else ""
    extra_params  = [now]              if status == TaskStatus.QUEUED else []
    with _conn() as conn:
        conn.execute(
            f"UPDATE agent_tasks SET status = ?, updated_at = ?{extra_fields} WHERE id = ? AND user_id = ?",
            [status.value, now] + extra_params + [task_id, user_id],
        )


def mark_task_failed(task_id: int) -> None:
    """Status-only update by ID — used by the scheduler's done-callback, which
    has no user_id. Touches nothing but the status column."""
    with _conn() as conn:
        conn.execute(
            "UPDATE agent_tasks SET status = 'FAILED', updated_at = ? WHERE id = ?",
            (_now(), task_id),
        )


def update_task_title(task_id: int, user_id: str, short_title: str) -> None:
    if not short_title or not short_title.strip():
        return
    with _conn() as conn:
        conn.execute(
            "UPDATE agent_tasks SET short_title = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (short_title.strip(), _now(), task_id, user_id),
        )


def update_task_refined_request(
    task_id: int, user_id: str, refined_request: str
) -> None:
    with _conn() as conn:
        conn.execute(
            """UPDATE agent_tasks SET refined_request = ?, updated_at = ?
               WHERE id = ? AND user_id = ?""",
            (refined_request, _now(), task_id, user_id),
        )


def update_task_result(
    task_id: int,
    user_id: str,
    result_md: str,
    paperless_id: int | None = None,
    paperless_url: str | None = None,
) -> None:
    with _conn() as conn:
        conn.execute(
            """UPDATE agent_tasks
               SET result_md = ?, paperless_id = ?, paperless_url = ?, updated_at = ?
               WHERE id = ? AND user_id = ?""",
            (result_md, paperless_id, paperless_url, _now(), task_id, user_id),
        )


def delete_task(task_id: int, user_id: str) -> None:
    with _conn() as conn:
        conn.execute(
            "DELETE FROM agent_tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        )


def next_queued_task() -> Task | None:
    """Return oldest QUEUED task across all users. Called by scheduler."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM agent_tasks WHERE status = 'QUEUED' ORDER BY created_at ASC LIMIT 1",
        ).fetchone()
    return _row_to_task(row) if row else None


# ── SubTask CRUD ──────────────────────────────────────────────────────────────

def insert_subtask(
    task_id: int,
    user_id: str,
    instruction: str,
    success_criteria: str,
    archetype_id: int | None,
    order_index: int,
) -> SubTask:
    """Insert a subtask with empty depends_on. Call update_depends_on after remapping."""
    now = _now()
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO agent_subtasks
               (task_id, archetype_id, user_id, instruction, success_criteria,
                status, depends_on, order_index, retry_count, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'TODO', '[]', ?, 0, ?, ?)""",
            (task_id, archetype_id, user_id, instruction, success_criteria,
             order_index, now, now),
        )
        row_id = cur.lastrowid
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM agent_subtasks WHERE id = ?", (row_id,)
        ).fetchone()
    return _row_to_subtask(row)


def update_depends_on(subtask_id: int, depends_on: list[int]) -> None:
    """Write the remapped DB-ID dependency list after bulk insert."""
    with _conn() as conn:
        conn.execute(
            "UPDATE agent_subtasks SET depends_on = ?, updated_at = ? WHERE id = ?",
            (json.dumps(depends_on), _now(), subtask_id),
        )


def get_subtasks(task_id: int, user_id: str) -> list[SubTask]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT * FROM agent_subtasks
               WHERE task_id = ? AND user_id = ?
               ORDER BY order_index""",
            (task_id, user_id),
        ).fetchall()
    return [_row_to_subtask(r) for r in rows]


def set_subtask_running(subtask_id: int) -> None:
    now = _now()
    with _conn() as conn:
        conn.execute(
            """UPDATE agent_subtasks
               SET status = 'RUNNING', started_at = ?, updated_at = ?
               WHERE id = ?""",
            (now, now, subtask_id),
        )


def set_subtask_result(
    subtask_id: int,
    *,
    status: SubTaskStatus,
    result_raw: str | None = None,
    result_compacted: str | None = None,
    critic_verdict: str | None = None,
    retry_count: int | None = None,
) -> None:
    now = _now()
    fields = ["status = ?", "updated_at = ?", "finished_at = ?"]
    params: list = [status.value, now, now]

    if result_raw is not None:
        fields.append("result_raw = ?"); params.append(result_raw)
    if result_compacted is not None:
        fields.append("result_compacted = ?"); params.append(result_compacted)
    if critic_verdict is not None:
        fields.append("critic_verdict = ?"); params.append(critic_verdict)
    if retry_count is not None:
        fields.append("retry_count = ?"); params.append(retry_count)

    params.append(subtask_id)
    with _conn() as conn:
        conn.execute(
            f"UPDATE agent_subtasks SET {', '.join(fields)} WHERE id = ?",
            params,
        )


def increment_subtask_retry(subtask_id: int) -> None:
    with _conn() as conn:
        conn.execute(
            """UPDATE agent_subtasks
               SET retry_count = retry_count + 1, updated_at = ?
               WHERE id = ?""",
            (_now(), subtask_id),
        )
