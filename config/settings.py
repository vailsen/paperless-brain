# config/settings.py
from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_path: Path

    # Paperless
    paperless_url: str
    paperless_superuser_token: str
    # Names a real tag in Paperless — existing installs must pin their own value.
    ignore_inbox_tag_at_sync: str = "Inbox"

    # ChromaDB
    embedding_model: str
    chroma_path: str
    chroma_collection: str
    extraction_sidecar_path: str
    thumb_path: str
    chroma_max_results: int = 20
    brain_hint_similarity_threshold: float = 0.70
    brain_hint_window_factor: float = 1.5
    # VisionLLM (configured via Einstellungen > Verarbeitung, stored in werkbank_settings)
    ollama_server: str = ""
    ollama_ingest_model: str = ""
    # Extraction-rule profile (config/extraction_rules/<code>.py). "en" ships
    # common international document types; "de" the German legal domain.
    extraction_profile: str = "en"
    # Language of AI-generated extraction output (summaries, image descriptions).
    # Archive-level, not per-user — sidecars are shared between users.
    archive_language: str = "en"
    # IANA timezone for timestamps on generated documents (reads the standard
    # TZ env var). Empty = system local time.
    tz: str = ""

    # Wake-on-LAN / remote shutdown of the Ollama host (empty = feature disabled)
    ollama_host_lan_mac_address_wol: str = ""
    ollama_ssh_user: str = ""
    ollama_idle_shutdown_minutes: int = 30

    # AI-generated documents
    ai_generated_tag_name: str = "AI-generated"
    ai_generated_correspondent: str = "PaperlessBrain AI"
    ai_generated_doc_type: str = "Information"

    # SearXNG
    searxng_host: str = "http://localhost:8888"

    # Debug: surface raw retrieval scores on result cards and in the detail
    # dialog. Off by default — a bare number next to a document tells a user
    # nothing actionable and competes with the title for attention.
    show_relevance_scores: bool = False

    # Vault / Obsidian-backed memory
    vault_root: Path = Path("/mnt/vaults")
    # Names a real folder inside every user's vault. Existing installs must pin
    # their old value in .env — changing this orphans already-written memory files.
    brain_subfolder: str = "PaperlessBrain Memory"
    # Voice memos. Same warning as above: names a real folder, changing it
    # orphans already-written memos.
    memo_subfolder: str = "PaperlessBrain Memos"
    vault_sync_cooldown_s: int = 3

    # Voice memos — transcription service (OpenAI-compatible /v1/audio/transcriptions).
    # Empty whisper_url = feature hidden everywhere. Not shipped with the app; see
    # docs/voice-memos-tasks.md for setup options.
    whisper_url: str = ""
    whisper_api_key: str = ""
    whisper_model: str = "whisper-1"
    whisper_language: str = ""
    # Hard caps on what the upload route accepts. A stuck recording must not
    # post a huge blob, and transcription is CPU work worth bounding.
    memo_max_upload_mb: int = 25
    memo_max_seconds: int = 300
    # Conversation mode records a meeting, not a thought — the memo caps would
    # cut it off mid-sentence. The transcription service has its own upload
    # limit; keep this at or below it (hwdsl2/whisper-server: WHISPER_MAX_UPLOAD_MB).
    conversation_max_upload_mb: int = 200
    conversation_max_seconds: int = 3600

    # NiceGui
    storage_secret: str
    shutdown_password: str = ""
    host: str = "0.0.0.0"
    port: int = 8080

    class Config:
        env_file = ".env"


settings = Settings()


def local_tz():
    """Timezone for user-facing timestamps: TZ setting, else system local."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if settings.tz:
        try:
            return ZoneInfo(settings.tz)
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo
