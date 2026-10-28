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


def fake_push(monkeypatch, devices: dict[str, str], sent: list) -> None:
    """Push configured, one device per user (`{user: device_id}`), delivery
    recorded into `sent` as `("sendMessage", {"chat_id": device_id, "text":
    title + body})` — the shape the Telegram fake had, so assertions on what
    reached a user stay readable. Call after `proactive_on`."""
    from config import proactive as cfgmod
    from proactive.notify import webpush

    pub, priv = webpush.generate_keys()
    monkeypatch.setenv("PUSH_VAPID_PUBLIC_KEY", pub)
    monkeypatch.setenv("PUSH_VAPID_PRIVATE_KEY", priv)
    cfgmod.proactive_settings.cache_clear()
    for user, dev in devices.items():
        webpush.subscribe(user, {"endpoint": f"https://fcm.googleapis.com/fcm/send/{dev}",
                                 "keys": {"p256dh": "k", "auth": "a"}})

    def send_one(sub, data, urgency="normal"):
        text = "\n".join(x for x in (data.get("title"), data.get("body")) if x)
        sent.append(("sendMessage", {"chat_id": sub["endpoint"].rsplit("/", 1)[-1], "text": text,
                                     "url": data.get("url", ""), "urgency": urgency}))
        return 201

    monkeypatch.setattr(webpush, "_send_one", send_one)
