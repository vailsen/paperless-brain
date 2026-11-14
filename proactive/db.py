"""`proactive.db` — the proactive system's operational store (architecture §4.1).

Disposable by design except `channel_link` and `push_subscription`: job queue, schedules, cursors and
the analysis cache can all be rebuilt; notification dedupe lives in the finding
files, so losing this file causes a re-sweep, never a notification storm.

Both processes open it (the app enqueues jobs and writes prefs and link codes;
the worker runs everything), so it runs in WAL mode with a busy timeout, and
every write is one short transaction.

Nothing here runs while `PROACTIVE_ENABLED=false`: `connect()` refuses, so no
code path can create the file by accident.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

#: The schema every proactive.db starts from: the state the first release
#: shipped with (the pre-release steps 1–8 squashed into one). Creates
#: `PRAGMA user_version = BASE_VERSION`.
BASE_VERSION = 8

SCHEMA = """
CREATE TABLE proactive_prefs (
    user                TEXT PRIMARY KEY,
    proactive_optin     INTEGER NOT NULL DEFAULT 0,
    channels            TEXT    NOT NULL DEFAULT '',
    model               TEXT    NOT NULL DEFAULT '',
    deadline_offsets    TEXT    NOT NULL DEFAULT '',
    language            TEXT    NOT NULL DEFAULT '',
    chat_model          TEXT    NOT NULL DEFAULT '',
    notify_sync         INTEGER NOT NULL DEFAULT 1,
    notify_werkbank     INTEGER NOT NULL DEFAULT 1,
    -- '' = the app language: German documents, English app is the normal case
    findings_language   TEXT    NOT NULL DEFAULT '',
    quiet_start         TEXT    NOT NULL DEFAULT '22:00',
    quiet_end           TEXT    NOT NULL DEFAULT '07:00',
    updated_at          TEXT    NOT NULL
);
CREATE TABLE window_log (
    id             INTEGER PRIMARY KEY,
    window_start   TEXT    NOT NULL,
    model          TEXT    NOT NULL DEFAULT '',
    docs_processed INTEGER NOT NULL DEFAULT 0,
    woke_host      INTEGER NOT NULL DEFAULT 0,
    stop_reason    TEXT    NOT NULL DEFAULT ''
);
-- `model_key` / `lane`: model grouping (one swap per model per window);
-- `lane` is '' for jobs without LLM.
CREATE TABLE jobs (
    id           INTEGER PRIMARY KEY,
    kind         TEXT    NOT NULL,
    user         TEXT    NOT NULL DEFAULT '',
    payload_json TEXT    NOT NULL DEFAULT '{}',
    needs_llm    INTEGER NOT NULL DEFAULT 0,
    status       TEXT    NOT NULL DEFAULT 'queued',
    attempts     INTEGER NOT NULL DEFAULT 0,
    run_after    TEXT    NOT NULL,
    created_at   TEXT    NOT NULL,
    finished_at  TEXT,
    last_error   TEXT,
    dedupe_key   TEXT,
    model_key    TEXT    NOT NULL DEFAULT '',
    lane         TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX jobs_due ON jobs(status, run_after);
CREATE UNIQUE INDEX jobs_dedupe ON jobs(dedupe_key)
    WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'running');
CREATE TABLE schedules (
    name          TEXT PRIMARY KEY,
    next_run_at   TEXT NOT NULL,
    interval_spec TEXT NOT NULL
);
CREATE TABLE sweep_cursor (
    user             TEXT    NOT NULL,
    analysis_version INTEGER NOT NULL,
    last_doc_id      INTEGER NOT NULL DEFAULT 0,
    finished_at      TEXT,
    PRIMARY KEY (user, analysis_version)
);
CREATE TABLE newdoc_cursor (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    last_doc_id INTEGER NOT NULL
);
CREATE TABLE doc_analysis (
    doc_id           INTEGER NOT NULL,
    analysis_version INTEGER NOT NULL,
    summary          TEXT    NOT NULL DEFAULT '',
    deadlines_json   TEXT    NOT NULL DEFAULT '[]',
    topic_hint       TEXT    NOT NULL DEFAULT '',
    content_hash     TEXT    NOT NULL DEFAULT '',
    model            TEXT    NOT NULL DEFAULT '',
    created_at       TEXT    NOT NULL,
    PRIMARY KEY (doc_id, analysis_version)
);
-- Not disposable: the user's confirmed addresses.
CREATE TABLE channel_link (
    user            TEXT    NOT NULL,
    channel         TEXT    NOT NULL,
    address         TEXT    NOT NULL,
    full_text_optin INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL,
    PRIMARY KEY (user, channel)
);
CREATE INDEX channel_link_address ON channel_link(channel, address);
CREATE TABLE link_code (
    code_hash  TEXT PRIMARY KEY,
    user       TEXT NOT NULL,
    channel    TEXT NOT NULL,
    address    TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL,
    used_at    TEXT
);
-- The per-user daily cloud cap.
CREATE TABLE cloud_usage (
    user TEXT    NOT NULL,
    day  TEXT    NOT NULL,
    docs INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user, day)
);
-- Small worker state (key/value).
CREATE TABLE kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- When a user was last told something (`proactive.pushes`).
CREATE TABLE push_log (
    id         INTEGER PRIMARY KEY,
    user       TEXT NOT NULL,
    at         TEXT NOT NULL,
    kind       TEXT NOT NULL,
    finding_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX push_log_user_at ON push_log(user, at);
-- Web Push: one row per device. Like `channel_link`, not disposable.
-- `last_status` / `last_sent`: what the push service said last time (shown in
-- the settings, so "no push arrived" can be told apart from "not accepted").
CREATE TABLE push_subscription (
    endpoint    TEXT PRIMARY KEY,
    user        TEXT NOT NULL,
    p256dh      TEXT NOT NULL,
    auth        TEXT NOT NULL,
    device      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    last_status INTEGER,
    last_sent   TEXT
);
CREATE INDEX push_subscription_user ON push_subscription(user);
-- Messages held back during quiet hours, sent as one at their end.
CREATE TABLE quiet_hold (
    id           INTEGER PRIMARY KEY,
    user         TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE INDEX quiet_hold_user ON quiet_hold(user);
"""

# Append-only. Entry i moves `PRAGMA user_version` from BASE_VERSION + i to
# BASE_VERSION + i + 1; never edit a shipped step, add a new one.
MIGRATIONS: list[str] = []

SCHEMA_VERSION = BASE_VERSION + len(MIGRATIONS)


def kv_get(key: str, default: str = "") -> str:
    with connect() as conn:
        r = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else default


def kv_set(key: str, value: str) -> None:
    from proactive import clock

    with connect() as conn:
        conn.execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, clock.now_iso()),
        )


class ProactiveDisabled(RuntimeError):
    """A proactive code path ran while `PROACTIVE_ENABLED=false`."""


def db_path() -> Path:
    from config.proactive import proactive_settings
    from config.settings import settings

    return proactive_settings().db_path(settings.app_path)


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        conn.executescript(f"BEGIN;\n{SCHEMA}\nPRAGMA user_version = {BASE_VERSION};\nCOMMIT;")
        version = BASE_VERSION
    elif version < BASE_VERSION:
        raise RuntimeError(
            f"proactive.db is at pre-release schema {version}; move it aside and restart "
            "(channel links and push devices must then be set up again)")
    for i, script in enumerate(MIGRATIONS[version - BASE_VERSION:], start=version + 1):
        # executescript commits first; the version bump is part of the script so
        # a crash between the two cannot re-run a half-applied step.
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {i};\nCOMMIT;")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """A connection with migrations applied. Commits on success, rolls back on
    an exception. Short-lived: open, do one thing, close."""
    from config.proactive import is_enabled

    if not is_enabled():
        raise ProactiveDisabled("PROACTIVE_ENABLED=false — proactive.db is not used")
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 15000")
        # Checked on every open (one pragma read): a deleted file is recreated
        # with the full schema instead of failing on a missing table.
        if conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
            conn.execute("PRAGMA journal_mode = WAL")
        _migrate(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
    finally:
        conn.close()


def schema_version() -> int:
    with connect() as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]
