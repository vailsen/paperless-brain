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


_migrated = False


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive, idempotent: the expert a conversation runs with, what it is
    about (`dossier:<id>`, `finding:<id>`), and per-answer provenance — the
    tool log of a turn, which "Save to dossier" needs after a reload too."""
    global _migrated
    if _migrated:
        return
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_conversations)")}
    except sqlite3.Error:
        return
    if not cols:
        return  # schema not created yet (werkbank/schema.sql does that)
    if "expert_id" not in cols:
        conn.execute("ALTER TABLE chat_conversations ADD COLUMN expert_id TEXT")
    if "context" not in cols:
        conn.execute("ALTER TABLE chat_conversations ADD COLUMN context TEXT")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS chat_turn_meta ("
        " conversation_id TEXT NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,"
        " user_id TEXT NOT NULL, msg_index INTEGER NOT NULL, meta TEXT NOT NULL,"
        " PRIMARY KEY (conversation_id, msg_index))"
    )
    conn.commit()
    _migrated = True


@contextmanager
def _conn() -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate(conn)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def create_conversation(
    user_id: str, title: str = "Neues Gespräch", *, expert_id: str | None = None,
    context: str | None = None,
) -> str:
    conv_id = str(uuid.uuid4())
    now = _now()
    with _conn() as c:
        c.execute(
            "INSERT INTO chat_conversations (id, user_id, title, created_at, updated_at, expert_id, context)"
            " VALUES (?,?,?,?,?,?,?)",
            (conv_id, user_id, title[:80], now, now, expert_id, context),
        )
    return conv_id


def list_conversations(user_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, title, updated_at, expert_id, context FROM chat_conversations"
            " WHERE user_id=? ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_conversation(conv_id: str, user_id: str) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT id, title, updated_at, expert_id, context FROM chat_conversations"
            " WHERE id=? AND user_id=?",
            (conv_id, user_id),
        ).fetchone()
    return dict(row) if row else None


def set_turn_meta(conv_id: str, user_id: str, msg_index: int, meta: dict) -> None:
    import json

    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO chat_turn_meta (conversation_id, user_id, msg_index, meta)"
            " VALUES (?,?,?,?)",
            (conv_id, user_id, int(msg_index), json.dumps(meta, ensure_ascii=False)),
        )


def load_turn_meta(conv_id: str, user_id: str) -> dict[int, dict]:
    import json

    with _conn() as c:
        rows = c.execute(
            "SELECT msg_index, meta FROM chat_turn_meta WHERE conversation_id=? AND user_id=?",
            (conv_id, user_id),
        ).fetchall()
    out = {}
    for r in rows:
        try:
            out[int(r["msg_index"])] = json.loads(r["meta"])
        except (ValueError, TypeError):
            continue
    return out


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
        # Message indices change: provenance of the old turns no longer fits.
        c.execute(
            "DELETE FROM chat_turn_meta WHERE conversation_id=? AND user_id=?",
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
