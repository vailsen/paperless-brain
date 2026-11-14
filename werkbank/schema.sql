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
