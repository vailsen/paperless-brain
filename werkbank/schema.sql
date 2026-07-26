-- werkbank/schema.sql
-- Applied once at startup via repository.init_db().
-- All tables carry user_id; every query filters on it.

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS agent_archetypes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    soul_text   TEXT    NOT NULL DEFAULT '',
    enabled_tools TEXT  NOT NULL DEFAULT '[]',  -- JSON array of tool names
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    UNIQUE (user_id, name)
);

CREATE TABLE IF NOT EXISTS agent_tasks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           TEXT    NOT NULL,
    original_request  TEXT    NOT NULL,
    refined_request   TEXT    NOT NULL DEFAULT '',
    status            TEXT    NOT NULL DEFAULT 'DRAFT',
    model             TEXT    NOT NULL DEFAULT '',
    result_md         TEXT,
    paperless_id      INTEGER,
    paperless_url     TEXT,
    short_title       TEXT,
    started_at        TEXT,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_tasks_user_status
    ON agent_tasks (user_id, status);

CREATE TABLE IF NOT EXISTS agent_subtasks (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id          INTEGER NOT NULL REFERENCES agent_tasks(id) ON DELETE CASCADE,
    archetype_id     INTEGER REFERENCES agent_archetypes(id) ON DELETE SET NULL,
    user_id          TEXT    NOT NULL,   -- denormalised: defence-in-depth
    instruction      TEXT    NOT NULL,
    success_criteria TEXT    NOT NULL DEFAULT '',
    status           TEXT    NOT NULL DEFAULT 'TODO',
    depends_on       TEXT    NOT NULL DEFAULT '[]',  -- JSON array of subtask IDs
    order_index      INTEGER NOT NULL DEFAULT 0,
    result_raw       TEXT,
    result_compacted TEXT,
    critic_verdict   TEXT,
    retry_count      INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL,
    started_at       TEXT,
    finished_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_subtasks_task
    ON agent_subtasks (task_id, order_index);

CREATE INDEX IF NOT EXISTS idx_agent_subtasks_user_status
    ON agent_subtasks (user_id, status);

-- Global admin-tunable settings (prompts, tag names, etc.)
-- Not user-scoped: these apply to all users.
CREATE TABLE IF NOT EXISTS werkbank_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Chat conversation history (per user)
CREATE TABLE IF NOT EXISTS chat_conversations (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    title      TEXT NOT NULL DEFAULT 'Neues Gespräch',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_conv_user
    ON chat_conversations (user_id, updated_at);

CREATE TABLE IF NOT EXISTS chat_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT    NOT NULL REFERENCES chat_conversations(id) ON DELETE CASCADE,
    user_id         TEXT    NOT NULL,
    role            TEXT    NOT NULL,
    content         TEXT    NOT NULL,
    created_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_conv
    ON chat_messages (conversation_id, id);
