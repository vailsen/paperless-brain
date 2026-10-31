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

The app's requests are also stamped separately (`app_activity`): the worker
leaves the GPU to a user who is chatting (`app_busy`) and only picks its own
LLM jobs up again once the app has been quiet for `APP_YIELD_S`.
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

#: The worker starts no LLM job while the app made an inference request this
#: recently. A running job finishes first (one document, seconds).
APP_YIELD_S = 180.0

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
        # A user is using it: a host the worker woke gets the normal idle time.
        # One woken for a chat keeps its own, shorter one.
        if _read().get("wol_by") != "chat":
            changes["wol_by"] = "app"
        changes["app_activity"] = changes["last_activity"]
    try:
        _update(**changes)
    except OSError:
        pass


#: Monotonic time of the last chat request in this process (`chat_touch`).
_chat_last: float | None = None


def chat_touch() -> None:
    """A user's chat just sent an inference request. Process-local: what waits
    on it (a proactive Werkbank run, `chat_busy`) runs in the app too."""
    global _chat_last
    _chat_last = time.monotonic()


def chat_busy() -> bool:
    """Did a chat use the model within `APP_YIELD_S`?"""
    return _chat_last is not None and time.monotonic() - _chat_last < APP_YIELD_S


def app_busy(now: datetime | None = None) -> bool:
    """Did the app (a user's chat, ingest, Werkbank) use the model within
    `APP_YIELD_S`? Read by the worker before it starts an LLM job."""
    raw = _read().get("app_activity")
    try:
        last = datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return False
    return last is not None and ((now or datetime.now()) - last).total_seconds() < APP_YIELD_S


def on_wol(host: str = "", by: str = "") -> None:
    """Record a wake. `by`: "chat" for a wake a chat asked for; default the
    process role ("app" — the dashboard button — or "worker")."""
    global _last_touch_write
    changes = {
        "wol_active": True, "last_activity": datetime.now().isoformat(), "wol_by": by or _role,
        # A fresh wake starts clean: no leftover retry or verification.
        "shutdown_pending_since": None, "shutdown_retry_at": None, "shutdown_error": "",
    }
    if host:
        changes["wol_host"] = host
    _update(**changes)
    _last_touch_write = time.monotonic()


def on_manual_shutdown() -> None:
    _update(wol_active=False, shutdown_pending_since=None, shutdown_retry_at=None)


#: After a shutdown command, check this long later that the host is really gone.
VERIFY_AFTER_S = 180
#: A failed or ineffective shutdown is tried again after this long.
RETRY_AFTER_S = 300


def _log(msg: str) -> None:
    print(f"[watchdog] {msg}", flush=True)


def _target_host() -> str:
    from config.settings import settings

    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", settings.ollama_server)
    return m.group(1) if m else state()["wol_host"]


#: Exit status of `SHUTDOWN_CMD` when someone is using the machine at its seat.
IN_USE = 75
#: The idle shutdown, run on the host: refuse while a local desktop session is
#: active and not idle (logind: class user, on a seat, foreground, IdleHint no
#: — desktops set the hint once the screen idles). The watchdog cannot see a
#: person at the keyboard; at 01:14 one night it shut the PC down under its
#: owner, who had switched it back on after the worker's wake. A host without
#: logind has no sessions to list and shuts down as before.
SHUTDOWN_CMD = (
    "for s in $(loginctl list-sessions --no-legend 2>/dev/null | awk '{print $1}'); do "
    'c=$(loginctl show-session "$s" -p Class --value); '
    'a=$(loginctl show-session "$s" -p Active --value); '
    'i=$(loginctl show-session "$s" -p IdleHint --value); '
    'st=$(loginctl show-session "$s" -p Seat --value); '
    f'[ "$c" = user ] && [ "$a" = yes ] && [ -n "$st" ] && [ "$i" = no ] && exit {IN_USE}; '
    "done; sudo shutdown -h now"
)


async def _ssh_shutdown() -> tuple[bool, str]:
    """Run `SHUTDOWN_CMD` on the host. `(True, "")` when the command was
    accepted (or the connection dropped because the host went down),
    `(False, "in use")` when someone is at the machine, `(False, reason)`
    otherwise.

    `BatchMode=yes`: without it ssh may sit at a password or passphrase prompt
    until the timeout — which used to count as success, so the host was marked
    off while it kept running all morning."""
    from config.settings import settings

    host, user = _target_host(), settings.ollama_ssh_user
    if not host or not user:
        return False, "no host or OLLAMA_SSH_USER"
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=5",
            "-o", "BatchMode=yes",
            f"{user}@{host}",
            SHUTDOWN_CMD,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _out, err = await asyncio.wait_for(proc.communicate(), timeout=15.0)
        except asyncio.TimeoutError:
            proc.kill()
            return False, "ssh did not finish within 15 s"
    except Exception as exc:
        return False, f"ssh could not run: {exc}"
    detail = (err or b"").decode(errors="replace").strip().splitlines()
    last = detail[-1] if detail else ""
    if proc.returncode == 0:
        return True, ""
    if proc.returncode == IN_USE:
        return False, "in use"
    # The host closing the connection while it shuts down is success.
    if proc.returncode == 255 and re.search(r"closed by remote host|Connection reset", last, re.I):
        return True, ""
    return False, last or f"ssh exited with {proc.returncode}"


async def _host_reachable(host: str, port: int = 11434) -> bool:
    try:
        _r, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=3.0)
        w.close()
        return True
    except Exception:
        return False


def shutdown_due(now: datetime | None = None) -> bool:
    from config.settings import settings

    s = state()
    if not s["wol_active"] or s["last_activity"] is None:
        return False
    retry = _read().get("shutdown_retry_at")
    if retry and (now or datetime.now()) < datetime.fromisoformat(retry):
        return False
    now = now or datetime.now()
    minutes = settings.ollama_idle_shutdown_minutes
    if s["wol_by"] == "worker":
        from config.proactive import proactive_settings

        minutes = proactive_settings().proactive_idle_shutdown_minutes
    elif s["wol_by"] == "chat":
        minutes = settings.ollama_chat_idle_shutdown_minutes
    return now - s["last_activity"] > timedelta(minutes=minutes)


async def watchdog_step(now: datetime | None = None) -> str:
    """One check. Returns what it did (for tests and the log):
    `idle` | `shutdown` | `failed` | `verified` | `still_up` | `in_use`."""
    now = now or datetime.now()
    d = _read()
    pending = d.get("shutdown_pending_since")
    if pending:
        if (now - datetime.fromisoformat(pending)).total_seconds() < VERIFY_AFTER_S:
            return "idle"
        host = _target_host()
        if host and await _host_reachable(host):
            # Up again after an accepted shutdown: almost always a person who
            # switched it back on — it is theirs now, never shut down again.
            # (This used to retry, and shut the PC down under its owner.) A
            # hung shutdown is reported instead of forced.
            _log(f"{host} still answers {VERIFY_AFTER_S}s after the shutdown command — "
                 "someone is using it, leaving it on")
            _update(wol_active=False, shutdown_pending_since=None, shutdown_retry_at=None,
                    shutdown_error="host was on again after the shutdown — left on")
            return "still_up"
        _update(shutdown_pending_since=None, shutdown_error="")
        _log(f"{host or 'host'} is off")
        return "verified"
    if not shutdown_due(now):
        return "idle"
    s = state()
    idle_min = int((now - s["last_activity"]).total_seconds() // 60)
    _log(f"shutting down {_target_host() or 'the host'}: idle {idle_min} min, woken by {s['wol_by']}")
    ok, why = await _ssh_shutdown()
    if not ok and why == "in use":
        _log(f"{_target_host() or 'host'} is in use at its seat — not shutting down, leaving it on")
        _update(wol_active=False, shutdown_pending_since=None, shutdown_retry_at=None, shutdown_error="")
        return "in_use"
    if ok:
        _update(wol_active=False, shutdown_pending_since=now.isoformat(), shutdown_error="",
                shutdown_retry_at=None)
        return "shutdown"
    # Keep `wol_active`: a failed shutdown must be retried, not forgotten. It
    # used to be marked off regardless, so one failure left the host running.
    _log(f"shutdown failed: {why} — retrying in {RETRY_AFTER_S // 60} min")
    _update(shutdown_error=why, shutdown_retry_at=(now + timedelta(seconds=RETRY_AFTER_S)).isoformat())
    return "failed"


async def idle_watchdog() -> None:
    """Background task (app only): shut the host down once it has been idle
    after a WoL wake — and make sure it really went down."""
    while True:
        await asyncio.sleep(60)
        try:
            await watchdog_step()
        except Exception as exc:  # the watchdog must outlive one bad check
            _log(f"check failed: {type(exc).__name__}: {exc}")
