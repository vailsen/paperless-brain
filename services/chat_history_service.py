"""Chat conversation persistence — stored in papersage.db (same DB as werkbank)."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

from config.settings import settings

_DB_PATH = Path(settings.app_path) / "data" / "papersage.db"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def create_conversation(user_id: str, title: str = "Neues Gespräch") -> str:
    conv_id = str(uuid.uuid4())
    now = _now()
    with _conn() as c:
        c.execute(
            "INSERT INTO chat_conversations (id, user_id, title, created_at, updated_at)"
            " VALUES (?,?,?,?,?)",
            (conv_id, user_id, title[:80], now, now),
        )
    return conv_id


def list_conversations(user_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, title, updated_at FROM chat_conversations"
            " WHERE user_id=? ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def load_messages(conv_id: str, user_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT role, content FROM chat_messages"
            " WHERE conversation_id=? AND user_id=? ORDER BY id",
            (conv_id, user_id),
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def append_messages(conv_id: str, user_id: str, messages: list[dict]) -> None:
    if not messages:
        return
    now = _now()
    with _conn() as c:
        c.executemany(
            "INSERT INTO chat_messages (conversation_id, user_id, role, content, created_at)"
            " VALUES (?,?,?,?,?)",
            [(conv_id, user_id, m["role"], m["content"], now) for m in messages],
        )
        c.execute(
            "UPDATE chat_conversations SET updated_at=? WHERE id=? AND user_id=?",
            (now, conv_id, user_id),
        )


def rename_conversation(conv_id: str, user_id: str, title: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE chat_conversations SET title=?, updated_at=? WHERE id=? AND user_id=?",
            (title[:80], _now(), conv_id, user_id),
        )


def delete_conversation(conv_id: str, user_id: str) -> None:
    with _conn() as c:
        c.execute(
            "DELETE FROM chat_conversations WHERE id=? AND user_id=?",
            (conv_id, user_id),
        )


def replace_messages(conv_id: str, user_id: str, messages: list[dict]) -> None:
    """Delete all messages for a conversation and insert new ones (used for compaction)."""
    now = _now()
    with _conn() as c:
        c.execute(
            "DELETE FROM chat_messages WHERE conversation_id=? AND user_id=?",
            (conv_id, user_id),
        )
        if messages:
            c.executemany(
                "INSERT INTO chat_messages (conversation_id, user_id, role, content, created_at)"
                " VALUES (?,?,?,?,?)",
                [(conv_id, user_id, m["role"], m["content"], now) for m in messages],
            )
        c.execute(
            "UPDATE chat_conversations SET updated_at=? WHERE id=? AND user_id=?",
            (now, conv_id, user_id),
        )
