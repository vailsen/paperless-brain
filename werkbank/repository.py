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
from werkbank.models import Archetype

_DB_PATH = Path(settings.app_path) / "data" / "papersage.db"
_LEGACY_DB_PATH = Path(settings.app_path) / "data" / "werkbank.db"
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Connection management ─────────────────────────────────────────────────────

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
