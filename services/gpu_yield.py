# services/gpu_yield.py
"""GPU yield: the proactive worker steps aside while a user is using the model.

This is not power management (that lives in whatever sits in front of a
sleeping GPU host, see docs/operations.md "Sleeping GPU host"). It only
decides who goes first on a shared local model:

- The app stamps every inference request it makes (`touch`) into a small
  shared file, `data/activity_state.json`. The worker reads it (`app_busy`)
  and starts no LLM job while the app made a request within `APP_YIELD_S`.
  A file, not module globals: the app and the worker are two processes.
- `chat_touch` / `chat_busy` are process-local: what waits on a user's chat
  (a proactive Werkbank run, an embedding sync) runs in the app too.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

#: `touch()` writes at most this often per process. A write per generated page
#: or chat token would be pointless churn.
TOUCH_WRITE_INTERVAL_S = 10.0

#: The worker starts no LLM job while the app made an inference request this
#: recently. A running job finishes first (one document, seconds).
APP_YIELD_S = 180.0

_last_touch_write = 0.0

#: "app" or "worker" — set once by the worker at start (`set_role`).
_role = "app"


def set_role(role: str) -> None:
    global _role
    _role = role


def role() -> str:
    return _role


def _state_path() -> Path:
    from config.settings import settings

    return Path(settings.app_path) / "data" / "activity_state.json"


def _read() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}


def _update(**changes) -> None:
    """Read-modify-write under an exclusive file lock, atomic replace."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o660)
    try:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        data = _read()
        data.update(changes)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, path)
    finally:
        os.close(lock)  # closing releases the flock


def touch() -> None:
    """An inference request just happened. Only the app's requests count: the
    worker's own requests must not make it wait for itself."""
    global _last_touch_write
    if _role != "app":
        return
    now = time.monotonic()
    if now - _last_touch_write < TOUCH_WRITE_INTERVAL_S:
        return
    _last_touch_write = now
    try:
        _update(app_activity=datetime.now().isoformat())
    except OSError:
        pass


def app_busy(now: datetime | None = None) -> bool:
    """Did the app (a user's chat, ingest, Werkbank) use the model within
    `APP_YIELD_S`? Read by the worker before it starts an LLM job."""
    raw = _read().get("app_activity")
    try:
        last = datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return False
    return last is not None and ((now or datetime.now()) - last).total_seconds() < APP_YIELD_S


#: Monotonic time of the last chat request in this process (`chat_touch`).
_chat_last: float | None = None


def chat_touch() -> None:
    """A user's chat just sent an inference request."""
    global _chat_last
    _chat_last = time.monotonic()


def chat_busy() -> bool:
    """Did a chat use the model within `APP_YIELD_S`?"""
    return _chat_last is not None and time.monotonic() - _chat_last < APP_YIELD_S
