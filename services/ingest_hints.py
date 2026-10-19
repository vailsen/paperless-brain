"""services/ingest_hints.py — per-document reading hints for the vision model.

A user who spots a misread value ("the blood pressure is 210/80, not 210/180")
leaves a hint when re-ingesting the document; every page prompt of that
document carries it from then on.

Kept in ``data/ingest_hints.json``, not in the sidecar: every re-ingest deletes
the sidecar before it writes a new one, and a correction that vanished with the
next prompt-version re-ingest would bring the misreading back unnoticed.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

FILENAME = "ingest_hints.json"
MAX_CHARS = 1000

_LOCK = threading.Lock()


def default_path() -> Path:
    from config.settings import settings

    return Path(settings.app_path) / "data" / FILENAME


def _load(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def get_hint(doc_id: int, path: Path | None = None) -> str:
    entry = _load(path or default_path()).get(str(doc_id)) or {}
    return str(entry.get("text") or "")


def set_hint(doc_id: int, text: str, user: str = "", path: Path | None = None) -> str:
    """Store the hint for ``doc_id``; an empty text removes it. Returns what was stored."""
    path = path or default_path()
    text = (text or "").strip()[:MAX_CHARS]
    with _LOCK:
        data = _load(path)
        if text:
            data[str(doc_id)] = {
                "text": text,
                "by": user,
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        elif data.pop(str(doc_id), None) is None:
            return ""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    return text
