"""werkbank/settings_store.py — global admin-tunable settings for the Werkbank.

Stored in the werkbank SQLite DB (werkbank_settings table).
Not user-scoped: applies to all users (Planner/Splitter/Critic/Synthesizer prompts
and tag names for Paperless export).

Falls back to:
  - config/settings.py for tag names (PAPERLESS_* env vars)
  - Each role's DEFAULT_SYSTEM_PROMPT constant for prompts
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from werkbank.repository import _DB_PATH

# ── Known keys ────────────────────────────────────────────────────────────────

PROMPT_PLANNER           = "prompt_planner"
PROMPT_SPLITTER          = "prompt_splitter"
PROMPT_SPLITTER_CRITIQUE = "prompt_splitter_critique"
PROMPT_CRITIC            = "prompt_critic"
PROMPT_SYNTHESIZER       = "prompt_synthesizer"
PROMPT_COMPACTION        = "prompt_compaction"
TAG_INBOX          = "tag_inbox"
TAG_AI_GENERATED   = "tag_ai_generated"
TAG_NO_INGEST      = "tag_no_ingest"
INGEST_SERVER        = "ingest_server"
INGEST_MODEL         = "ingest_model"
INGEST_MODEL_NAME    = "ingest_model_name"
INGEST_CORRESPONDENT = "ingest_correspondent"
INGEST_DOC_TYPE      = "ingest_doc_type"
EXTRACTION_PROFILE   = "extraction_profile"
ARCHIVE_LANGUAGE     = "archive_language"
SEARCH_MAX_RESULTS   = "search_max_results"
BRAIN_HINT_THRESHOLD = "brain_hint_threshold"
BRAIN_HINT_WINDOW    = "brain_hint_window"

TOKENS_PLANNER           = "tokens_planner"
TOKENS_SPLITTER          = "tokens_splitter"
TOKENS_SPLITTER_CRITIQUE = "tokens_splitter_critique"
TOKENS_CRITIC            = "tokens_critic"
TOKENS_SYNTHESIZER = "tokens_synthesizer"
TOKENS_COMPACTION  = "tokens_compaction"
TOKENS_WORKER      = "tokens_worker"

_DEFAULT_TOKENS = 16_000

_ALL_KEYS = (
    PROMPT_PLANNER, PROMPT_SPLITTER, PROMPT_SPLITTER_CRITIQUE, PROMPT_CRITIC,
    PROMPT_SYNTHESIZER, PROMPT_COMPACTION,
    TAG_INBOX, TAG_AI_GENERATED, TAG_NO_INGEST,
    INGEST_SERVER, INGEST_MODEL, INGEST_MODEL_NAME,
    INGEST_CORRESPONDENT, INGEST_DOC_TYPE,
    EXTRACTION_PROFILE, ARCHIVE_LANGUAGE,
    SEARCH_MAX_RESULTS, BRAIN_HINT_THRESHOLD, BRAIN_HINT_WINDOW,
    TOKENS_PLANNER, TOKENS_SPLITTER, TOKENS_SPLITTER_CRITIQUE, TOKENS_CRITIC,
    TOKENS_SYNTHESIZER, TOKENS_COMPACTION, TOKENS_WORKER,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Core get / set ────────────────────────────────────────────────────────────

def get(key: str) -> str:
    """Return stored value, or empty string if not set."""
    try:
        with sqlite3.connect(_DB_PATH) as conn:
            row = conn.execute(
                "SELECT value FROM werkbank_settings WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else ""
    except Exception:
        return ""


def set_value(key: str, value: str) -> None:
    """Upsert a setting value."""
    now = _now()
    with sqlite3.connect(_DB_PATH) as conn:
        conn.execute(
            "INSERT INTO werkbank_settings (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, now),
        )
        conn.commit()


def get_all() -> dict[str, str]:
    """Return all stored settings as {key: value}."""
    try:
        with sqlite3.connect(_DB_PATH) as conn:
            rows = conn.execute("SELECT key, value FROM werkbank_settings").fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


# ── Typed accessors ───────────────────────────────────────────────────────────

def get_prompt(key: str, default: str) -> str:
    """Return stored prompt or fall back to the role's DEFAULT_SYSTEM_PROMPT."""
    stored = get(key)
    return stored if stored.strip() else default


def get_tokens(key: str, default: int = _DEFAULT_TOKENS) -> int:
    """Return stored token limit or `default` (falls back to global 16 000)."""
    stored = get(key)
    try:
        v = int(stored)
        return v if v > 0 else default
    except (ValueError, TypeError):
        return default


def get_tag_inbox() -> str:
    stored = get(TAG_INBOX)
    if stored.strip():
        return stored
    from config.settings import settings
    return settings.ignore_inbox_tag_at_sync


def get_no_ingest_tag() -> str:
    """Tag that marks documents to skip during Paperless sync (no embedding)."""
    stored = get(TAG_NO_INGEST)
    if stored.strip():
        return stored
    from config.settings import settings
    return settings.ignore_inbox_tag_at_sync


def get_tag_ai_generated() -> str:
    stored = get(TAG_AI_GENERATED)
    if stored.strip():
        return stored
    from config.settings import settings
    return settings.ai_generated_tag_name


def get_ingest_server() -> str:
    """Ollama server URL for document ingestion. Falls back to .env OLLAMA_SERVER."""
    stored = get(INGEST_SERVER)
    if stored.strip():
        return stored
    from config.settings import settings
    return settings.ollama_server


def get_ingest_model() -> str:
    """Vision model for document ingestion. Falls back to .env OLLAMA_INGEST_MODEL."""
    stored = get(INGEST_MODEL)
    if stored.strip():
        return stored
    from config.settings import settings
    return settings.ollama_ingest_model


def get_extraction_profile() -> str:
    """Requested extraction-rule profile. Falls back to .env EXTRACTION_PROFILE.

    "Requested", not "active": an unknown name still falls back to the default
    profile at load time. Use config.extraction_rules.get_active_profile() to
    show what is really in use.
    """
    stored = get(EXTRACTION_PROFILE)
    if stored.strip():
        return stored.strip().lower()
    from config.settings import settings
    return (settings.extraction_profile or "").strip().lower()


def get_archive_language() -> str:
    """Language the vision model writes summaries in. Falls back to .env.

    Only generated text is affected — page_text is extracted verbatim and keeps
    the document's own language, so a foreign-language document stays readable
    while its summary stays consistent with the rest of the archive.
    """
    stored = get(ARCHIVE_LANGUAGE)
    if stored.strip():
        return stored.strip().lower()
    from config.settings import settings
    return (settings.archive_language or "en").strip().lower()


def get_ingest_model_name() -> str:
    """Registry entry name of the ingestion model, or "" for the .env Ollama path.

    The name rather than the model id, because the transport, base URL and API key
    all have to be looked up in the user's registry at call time — the key is
    encrypted per user and cannot be copied into this global store.
    """
    return get(INGEST_MODEL_NAME).strip()


def get_ingest_correspondent() -> str:
    stored = get(INGEST_CORRESPONDENT)
    if stored.strip():
        return stored
    from config.settings import settings
    return settings.ai_generated_correspondent


def get_ingest_doc_type() -> str:
    stored = get(INGEST_DOC_TYPE)
    if stored.strip():
        return stored
    from config.settings import settings
    return settings.ai_generated_doc_type


def get_search_max_results() -> int:
    stored = get(SEARCH_MAX_RESULTS)
    try:
        v = int(stored)
        if v > 0:
            return v
    except (ValueError, TypeError):
        pass
    from config.settings import settings
    return settings.chroma_max_results


def get_brain_hint_threshold() -> float:
    stored = get(BRAIN_HINT_THRESHOLD)
    try:
        v = float(stored)
        return max(0.0, min(1.0, v))
    except (ValueError, TypeError):
        pass
    from config.settings import settings
    return settings.brain_hint_similarity_threshold


def get_brain_hint_window() -> float:
    stored = get(BRAIN_HINT_WINDOW)
    try:
        v = float(stored)
        if v >= 1.0:
            return v
    except (ValueError, TypeError):
        pass
    from config.settings import settings
    return settings.brain_hint_window_factor
