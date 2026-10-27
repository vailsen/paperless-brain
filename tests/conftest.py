"""Shared test setup.

`config.settings` instantiates a pydantic Settings at import time, so the
required environment must exist before any application module is imported.
pytest imports conftest first, which makes this the only reliable place for it.

APP_PATH points at a throwaway directory: several modules create directories
under it at import (e.g. credential_store), and tests must never touch the
developer's real `data/`.
"""

import os
import tempfile
from pathlib import Path

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="paperlessbrain-tests-"))

os.environ.update(
    {
        "APP_PATH": f"{_TEST_ROOT}/",
        "PAPERLESS_URL": "http://paperless.invalid",
        "PAPERLESS_SUPERUSER_TOKEN": "test-token",
        "EMBEDDING_MODEL": "intfloat/multilingual-e5-large-instruct",
        "CHROMA_PATH": "data/chroma_db/",
        "CHROMA_COLLECTION": "test_documents",
        "EXTRACTION_SIDECAR_PATH": "data/extractions/",
        "THUMB_PATH": "data/thumbnails/",
        "STORAGE_SECRET": "test-secret",
        "VAULT_ROOT": str(_TEST_ROOT / "vaults"),
    }
)

import pytest  # noqa: E402


@pytest.fixture
def tmp_sidecar_dir(tmp_path: Path) -> Path:
    """Empty directory standing in for EXTRACTION_SIDECAR_PATH."""
    d = tmp_path / "extractions"
    d.mkdir()
    return d


@pytest.fixture
def proactive_on(tmp_path, monkeypatch):
    """PROACTIVE_ENABLED=true with a throwaway proactive.db and a resettable clock."""
    from config import proactive as cfg
    from proactive import clock

    monkeypatch.setenv("PROACTIVE_ENABLED", "true")
    monkeypatch.setenv("PROACTIVE_DB_PATH", str(tmp_path / "proactive.db"))
    # Quiet hours depend on the wall clock; a test run at 23:00 must not hold
    # every message back. Their own tests call `quiet_wait` directly.
    from datetime import timedelta

    from proactive import notify

    monkeypatch.setattr(notify, "quiet_delay", lambda user: timedelta(0))
    # Nor may the developer's own app (its data/power_state.json) pause the worker.
    from services import ollama_watchdog

    monkeypatch.setattr(ollama_watchdog, "app_busy", lambda now=None: False)
    cfg.proactive_settings.cache_clear()
    clock.reset()
    yield tmp_path / "proactive.db"
    cfg.proactive_settings.cache_clear()
    clock.reset()


@pytest.fixture
def proactive_off(tmp_path, monkeypatch):
    from config import proactive as cfg

    monkeypatch.setenv("PROACTIVE_ENABLED", "false")
    monkeypatch.setenv("PROACTIVE_DB_PATH", str(tmp_path / "proactive.db"))
    cfg.proactive_settings.cache_clear()
    yield tmp_path / "proactive.db"
    cfg.proactive_settings.cache_clear()
