# services/ollama_watchdog.py
import asyncio
import re
from datetime import datetime, timedelta

_wol_active = False
_last_activity: datetime | None = None


def touch() -> None:
    global _last_activity
    _last_activity = datetime.now()


def on_wol() -> None:
    global _wol_active, _last_activity
    _wol_active = True
    _last_activity = datetime.now()


def on_manual_shutdown() -> None:
    global _wol_active
    _wol_active = False


async def _ssh_shutdown() -> None:
    from config.settings import settings

    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", settings.ollama_server)
    host = m.group(1) if m else ""
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


async def idle_watchdog() -> None:
    """Background task: SSH-shutdown after IDLE_MINUTES of no Ollama use post-WoL."""
    while True:
        await asyncio.sleep(60)
        if not _wol_active or _last_activity is None:
            continue
        from config.settings import settings
        if datetime.now() - _last_activity > timedelta(minutes=settings.ollama_idle_shutdown_minutes):
            await _ssh_shutdown()
            on_manual_shutdown()
