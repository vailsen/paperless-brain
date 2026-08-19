"""Werkbank v2 — persistence.

Same rules as the v1 repository: SQLite in WAL mode, and **every query is
scoped to a user**. Background tasks are where cross-user leaks happen, so
`user_id` sits on runs *and* denormalised on subtasks as defence in depth.

Two things v2 needs that v1 did not:

- **`tool_calls` keeps the retrieved raw text.** Check D2 compares a quote
  against what the tool actually returned; without this table the check cannot
  run at all. It is scratch data — deleted with the run, never long-lived.
- **Resumability.** A run survives a restart: a subtask that reached a terminal
  status is never executed twice, which matters because each one costs a model
  call and, for the doc researcher, real retrieval work.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

from config.settings import settings
from werkbank.v2.models import (
    Brief,
    CriticVerdict,
    RunState,
    Subtask,
    SubtaskResult,
    SubtaskStatus,
)

_DB_PATH = Path(settings.app_path) / "data" / "papersage.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS wb2_runs (
    run_id       TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    model        TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'draft',
    report_md    TEXT NOT NULL DEFAULT '',
    brief_json   TEXT NOT NULL DEFAULT '{}',
    state_json   TEXT NOT NULL DEFAULT '{}',
    started_at   TEXT,
    finished_at  TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS wb2_runs_user ON wb2_runs(user_id);

CREATE TABLE IF NOT EXISTS wb2_subtasks (
    run_id       TEXT NOT NULL,
    subtask_id   TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    agent        TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'todo',
    revision     INTEGER NOT NULL DEFAULT 0,
    plan_json    TEXT NOT NULL DEFAULT '{}',
    result_json  TEXT,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (run_id, subtask_id)
);
CREATE INDEX IF NOT EXISTS wb2_subtasks_user ON wb2_subtasks(user_id);

CREATE TABLE IF NOT EXISTS wb2_verdicts (
    run_id       TEXT NOT NULL,
    subtask_id   TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    revision     INTEGER NOT NULL DEFAULT 0,
    verdict_json TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (run_id, subtask_id, revision)
);

CREATE TABLE IF NOT EXISTS wb2_tool_calls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    subtask_id   TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    source_id    TEXT NOT NULL,
    tool         TEXT NOT NULL,
    args_json    TEXT NOT NULL DEFAULT '{}',
    raw_text     TEXT NOT NULL DEFAULT '',
    trust        TEXT NOT NULL DEFAULT 'model',
    ref          TEXT NOT NULL DEFAULT '',
    hits         INTEGER,
    retrieved_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS wb2_tool_calls_subtask
    ON wb2_tool_calls(run_id, subtask_id);
"""

# A subtask in one of these is never executed again on resume.
TERMINAL = {SubtaskStatus.OK, SubtaskStatus.PARTIAL, SubtaskStatus.UNRESOLVABLE}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect() -> Generator[sqlite3.Connection, None, None]:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── Runs ──────────────────────────────────────────────────────────────────────


def create_run(
    run_id: str, user_id: str, brief: Brief, model: str = "", status: str = "planned"
) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO wb2_runs "
            "(run_id, user_id, model, status, brief_json, state_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (run_id, user_id, model, status, brief.model_dump_json(), "{}", _now(), _now()),
        )


def save_brief(run_id: str, user_id: str, brief: Brief) -> None:
    """Store a brief on an existing run — the briefing step finishing, or the
    user editing it in the confirmation dialog."""
    with connect() as conn:
        if _run_gone(conn, run_id, user_id):
            return
        conn.execute(
            "UPDATE wb2_runs SET brief_json=?, updated_at=? WHERE run_id=? AND user_id=?",
            (brief.model_dump_json(), _now(), run_id, user_id),
        )


def save_state(run_id: str, user_id: str, state: RunState) -> None:
    """Persist the whole run state. Subtask rows stay the source of truth for
    status; this is the rest (coverage verdicts, caps, flags)."""
    with connect() as conn:
        conn.execute(
            "UPDATE wb2_runs SET state_json=?, model=?, started_at=?, finished_at=?, "
            "updated_at=? WHERE run_id=? AND user_id=?",
            (
                state.model_dump_json(),
                state.model,
                state.started_at or None,
                state.finished_at or None,
                _now(),
                run_id,
                user_id,
            ),
        )


def load_state(run_id: str, user_id: str) -> RunState | None:
    """Rebuild a run: state row plus every persisted subtask and result."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM wb2_runs WHERE run_id=? AND user_id=?", (run_id, user_id)
        ).fetchone()
        if not row:
            return None
        state = RunState.model_validate_json(row["state_json"] or "{}")
        state.run_id, state.user_id = run_id, user_id
        if row["brief_json"] and row["brief_json"] != "{}":
            state.brief = Brief.model_validate_json(row["brief_json"])

        state.subtasks, state.results, state.statuses = [], {}, {}
        for sub in conn.execute(
            "SELECT * FROM wb2_subtasks WHERE run_id=? AND user_id=? ORDER BY subtask_id",
            (run_id, user_id),
        ):
            if sub["plan_json"] and sub["plan_json"] != "{}":
                state.subtasks.append(Subtask.model_validate_json(sub["plan_json"]))
            if sub["result_json"]:
                result = SubtaskResult.model_validate_json(sub["result_json"])
                state.results[result.subtask_id] = result
            else:
                # No result yet: the row's own status is what the board shows,
                # and it is the difference between "waiting" and "working".
                try:
                    state.statuses[sub["subtask_id"]] = SubtaskStatus(sub["status"])
                except ValueError:
                    state.statuses[sub["subtask_id"]] = SubtaskStatus.TODO

        state.verdicts = {}
        for ver in conn.execute(
            "SELECT subtask_id, verdict_json, revision FROM wb2_verdicts "
            "WHERE run_id=? AND user_id=? ORDER BY revision",
            (run_id, user_id),
        ):
            state.verdicts[ver["subtask_id"]] = CriticVerdict.model_validate_json(
                ver["verdict_json"]
            )
        return state


def save_report(run_id: str, user_id: str, markdown: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE wb2_runs SET report_md=?, status='done', updated_at=? "
            "WHERE run_id=? AND user_id=?",
            (markdown, _now(), run_id, user_id),
        )


def load_report(run_id: str, user_id: str) -> str:
    with connect() as conn:
        row = conn.execute(
            "SELECT report_md FROM wb2_runs WHERE run_id=? AND user_id=?",
            (run_id, user_id),
        ).fetchone()
    return row["report_md"] if row else ""


def set_run_status(run_id: str, user_id: str, status: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE wb2_runs SET status=?, updated_at=? WHERE run_id=? AND user_id=?",
            (status, _now(), run_id, user_id),
        )


def reset_stale_runs() -> int:
    """Startup repair: a process that dies mid-run leaves rows saying 'running'
    that nothing is running. There is no background resume loop by design — a
    run costs real model calls, so restarting one is the user's decision. Mark
    them resumable instead, which is what the play button acts on.

    Not user-scoped: this is a process-lifecycle fact, not a query about anyone's
    data, and it runs once at startup before any session exists.
    """
    with connect() as conn:
        cur = conn.execute(
            "UPDATE wb2_runs SET status='planned', updated_at=? WHERE status='running'",
            (_now(),),
        )
        return cur.rowcount


def list_runs(user_id: str, limit: int = 50) -> list[dict]:
    with connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT run_id, status, model, brief_json, created_at, updated_at FROM wb2_runs "
                "WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            )
        ]


def run_exists(run_id: str, user_id: str) -> bool:
    with connect() as conn:
        return conn.execute(
            "SELECT 1 FROM wb2_runs WHERE run_id=? AND user_id=?", (run_id, user_id)
        ).fetchone() is not None


def delete_run(run_id: str, user_id: str) -> None:
    """Drop the run and its scratch data — the raw tool text goes with it."""
    with connect() as conn:
        for table in ("wb2_tool_calls", "wb2_verdicts", "wb2_subtasks", "wb2_runs"):
            conn.execute(f"DELETE FROM {table} WHERE run_id=? AND user_id=?", (run_id, user_id))


# ── Subtasks ──────────────────────────────────────────────────────────────────


def save_plan(run_id: str, user_id: str, subtasks: list[Subtask]) -> None:
    with connect() as conn:
        if _run_gone(conn, run_id, user_id):
            return
        for sub in subtasks:
            conn.execute(
                "INSERT OR REPLACE INTO wb2_subtasks "
                "(run_id, subtask_id, user_id, agent, status, revision, plan_json, "
                " result_json, updated_at) "
                "VALUES (?,?,?,?,"
                "  COALESCE((SELECT status FROM wb2_subtasks WHERE run_id=? AND subtask_id=?), 'todo'),"
                "  COALESCE((SELECT revision FROM wb2_subtasks WHERE run_id=? AND subtask_id=?), 0),"
                "  ?,"
                "  (SELECT result_json FROM wb2_subtasks WHERE run_id=? AND subtask_id=?),"
                "  ?)",
                (
                    run_id, sub.subtask_id, user_id, sub.agent,
                    run_id, sub.subtask_id,
                    run_id, sub.subtask_id,
                    sub.model_dump_json(),
                    run_id, sub.subtask_id,
                    _now(),
                ),
            )


def _run_gone(conn, run_id: str, user_id: str) -> bool:
    """A deleted run must stay deleted. Its background task can still be in
    flight, and `INSERT OR REPLACE` on a subtask would put the run back in the
    list — deleting something that reappears reads as the button not working."""
    return conn.execute(
        "SELECT 1 FROM wb2_runs WHERE run_id=? AND user_id=?", (run_id, user_id)
    ).fetchone() is None


def save_result(run_id: str, user_id: str, result: SubtaskResult) -> None:
    with connect() as conn:
        if _run_gone(conn, run_id, user_id):
            return
        conn.execute(
            "UPDATE wb2_subtasks SET status=?, revision=?, result_json=?, updated_at=? "
            "WHERE run_id=? AND subtask_id=? AND user_id=?",
            (
                result.status.value,
                result.revision,
                result.model_dump_json(),
                _now(),
                run_id,
                result.subtask_id,
                user_id,
            ),
        )


def set_status(run_id: str, user_id: str, subtask_id: str, status: SubtaskStatus) -> None:
    with connect() as conn:
        if _run_gone(conn, run_id, user_id):
            return
        conn.execute(
            "UPDATE wb2_subtasks SET status=?, updated_at=? "
            "WHERE run_id=? AND subtask_id=? AND user_id=?",
            (status.value, _now(), run_id, subtask_id, user_id),
        )


def pending_subtask_ids(run_id: str, user_id: str) -> list[str]:
    """Subtasks still to run. The resume contract: terminal ones never again."""
    with connect() as conn:
        return [
            r["subtask_id"]
            for r in conn.execute(
                "SELECT subtask_id, status FROM wb2_subtasks "
                "WHERE run_id=? AND user_id=? ORDER BY subtask_id",
                (run_id, user_id),
            )
            if r["status"] not in {s.value for s in TERMINAL}
        ]


def save_verdict(
    run_id: str, user_id: str, subtask_id: str, revision: int, verdict: CriticVerdict
) -> None:
    """Every revision is kept, never overwritten — the audit trail is the point."""
    with connect() as conn:
        if _run_gone(conn, run_id, user_id):
            return
        conn.execute(
            "INSERT OR REPLACE INTO wb2_verdicts "
            "(run_id, subtask_id, user_id, revision, verdict_json, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (run_id, subtask_id, user_id, revision, verdict.model_dump_json(), _now()),
        )


# ── Tool calls (raw text for D2) ──────────────────────────────────────────────


def log_tool_call(
    run_id: str,
    user_id: str,
    subtask_id: str,
    *,
    source_id: str,
    tool: str,
    args: dict,
    raw_text: str,
    trust: str,
    ref: str = "",
    hits: int | None = None,
) -> None:
    with connect() as conn:
        if _run_gone(conn, run_id, user_id):
            return
        conn.execute(
            "INSERT INTO wb2_tool_calls "
            "(run_id, subtask_id, user_id, source_id, tool, args_json, raw_text, "
            " trust, ref, hits, retrieved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id, subtask_id, user_id, source_id, tool,
                json.dumps(args, ensure_ascii=False), raw_text, trust, ref, hits, _now(),
            ),
        )


def raw_texts_for(run_id: str, user_id: str, subtask_id: str) -> dict[str, str]:
    """source id → retrieved text. This is what D2 matches quotes against."""
    with connect() as conn:
        return {
            r["source_id"]: r["raw_text"]
            for r in conn.execute(
                "SELECT source_id, raw_text FROM wb2_tool_calls "
                "WHERE run_id=? AND subtask_id=? AND user_id=?",
                (run_id, subtask_id, user_id),
            )
        }


def tool_queries(run_id: str, user_id: str, subtask_id: str) -> set[str]:
    """Every call this subtask already made, as `tool:{args}` keys.

    Feeds the dedupe across revisions. Deliberately not a *count*: a revision
    redoes the research from scratch, so charging it for the earlier attempt's
    calls starves the attempt whose result actually counts.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT tool, args_json FROM wb2_tool_calls "
            "WHERE run_id=? AND subtask_id=? AND user_id=?",
            (run_id, subtask_id, user_id),
        ).fetchall()
    keys = set()
    for row in rows:
        try:
            args = json.loads(row["args_json"] or "{}")
        except ValueError:
            continue
        keys.add(f"{row['tool']}:{json.dumps(args, sort_keys=True, ensure_ascii=False)}")
    return keys


def tool_call_count(run_id: str, user_id: str, subtask_id: str) -> int:
    """Feeds `SelfCheck.tool_calls`, which is why D5 cannot be talked out of."""
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM wb2_tool_calls WHERE run_id=? AND subtask_id=? AND user_id=?",
            (run_id, subtask_id, user_id),
        ).fetchone()[0]
