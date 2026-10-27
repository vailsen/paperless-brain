# services/ollama_watchdog.py
"""Idle shutdown of the Ollama host — and the power state it decides on.

The state (was the host woken by us, when was the last inference request, which
host) lives in one small shared file, `data/power_state.json`, not in module
globals: two processes send inference requests — the app and the proactive
worker — and only the app runs the watchdog. With globals, a worker that woke
the host left the app unaware (no shutdown ever), and a worker sweep looked idle
to the app (shutdown mid-sweep). An app restart also forgot that it had woken
the host. Diagnostics D6.

Rules unchanged: only a host woken through WoL (`on_wol`) is ever shut down,
after `OLLAMA_IDLE_SHUTDOWN_MINUTES` without `touch()`. The worker calls
`touch()` and `on_wol()` like the app does and adds no shutdown logic.

One exception to the idle time: a host the *worker* woke and nobody else used
goes down after `PROACTIVE_IDLE_SHUTDOWN_MINUTES` (default 5) — no one is
waiting at night for a next question, so the long window only burns power.
The state records who woke it (`wol_by`); any request from the app (a user)
hands it back to the normal idle time.
"""

import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

#: `touch()` writes at most this often per process. The idle window is minutes;
#: a write per generated page or chat token would be pointless churn.
TOUCH_WRITE_INTERVAL_S = 10.0

_last_touch_write = 0.0

#: "app" or "worker" — set once by the worker at start (`set_role`).
_role = "app"


def set_role(role: str) -> None:
    global _role
    _role = role


def _state_path() -> Path:
    from config.settings import settings

    return Path(settings.app_path) / "data" / "power_state.json"


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


def state() -> dict:
    """`{"wol_active": bool, "last_activity": datetime|None, "wol_host": str}`."""
    d = _read()
    last = d.get("last_activity")
    try:
        last_dt = datetime.fromisoformat(last) if last else None
    except ValueError:
        last_dt = None
    return {
        "wol_active": bool(d.get("wol_active")),
        "last_activity": last_dt,
        "wol_host": d.get("wol_host", ""),
        "wol_by": d.get("wol_by", "app"),
    }


def touch() -> None:
    """An inference request just happened (any process)."""
    global _last_touch_write
    now = time.monotonic()
    if now - _last_touch_write < TOUCH_WRITE_INTERVAL_S:
        return
    _last_touch_write = now
    changes = {"last_activity": datetime.now().isoformat()}
    if _role == "app":
        changes["wol_by"] = "app"  # a user is using it: normal idle time again
    try:
        _update(**changes)
    except OSError:
        pass


def on_wol(host: str = "") -> None:
    global _last_touch_write
    changes = {"wol_active": True, "last_activity": datetime.now().isoformat(), "wol_by": _role}
    if host:
        changes["wol_host"] = host
    _update(**changes)
    _last_touch_write = time.monotonic()


def on_manual_shutdown() -> None:
    _update(wol_active=False)


async def _ssh_shutdown() -> None:
    from config.settings import settings

    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", settings.ollama_server)
    host = m.group(1) if m else state()["wol_host"]
    user = settings.ollama_ssh_user
    if not host or not user:
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=5",
            f"{user}@{host}",
            "sudo shutdown -h now",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=8.0)
        except asyncio.TimeoutError:
            pass
    except Exception:
        pass


def shutdown_due(now: datetime | None = None) -> bool:
    from config.settings import settings

    s = state()
    if not s["wol_active"] or s["last_activity"] is None:
        return False
    now = now or datetime.now()
    minutes = settings.ollama_idle_shutdown_minutes
    if s["wol_by"] == "worker":
        from config.proactive import proactive_settings

        minutes = proactive_settings().proactive_idle_shutdown_minutes
    return now - s["last_activity"] > timedelta(minutes=minutes)


async def idle_watchdog() -> None:
    """Background task (app only): SSH-shutdown after IDLE_MINUTES of no Ollama use post-WoL."""
    while True:
        await asyncio.sleep(60)
        if shutdown_due():
            await _ssh_shutdown()
            on_manual_shutdown()
