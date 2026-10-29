"""Wake-on-LAN — the one code path that wakes the Ollama host.

Used by the dashboard's wake button and by the proactive worker's LLM gate, so
both wake the same machine the same way and both record it in the shared power
state (`ollama_watchdog.on_wol`), which is what lets the app's idle watchdog
shut the host down again afterwards.
"""

from __future__ import annotations

import re
import socket


def broadcast_for(server_url: str) -> str:
    """`http://192.168.1.20:11434` → `192.168.1.255`; unknown → limited broadcast."""
    m = re.search(r"(\d+\.\d+\.\d+)\.\d+", server_url or "")
    return f"{m.group(1)}.255" if m else "255.255.255.255"


def host_ip(server_url: str) -> str:
    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", server_url or "")
    return m.group(1) if m else ""


def magic_packet(mac: str) -> bytes:
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(raw) != 6:
        raise ValueError(f"not a MAC address: {mac!r}")
    return b"\xff" * 6 + raw * 16


def send_magic_packet(mac: str, broadcast: str, port: int = 9) -> None:
    packet = magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast, port))


def wake(mac: str, server_url: str, by: str = "") -> str:
    """Send the packet and record the wake in the shared power state. Returns
    the broadcast address used. Raises on a bad MAC or a socket error."""
    from services.ollama_watchdog import on_wol

    broadcast = broadcast_for(server_url)
    send_magic_packet(mac, broadcast)
    on_wol(host_ip(server_url), by=by)
    return broadcast


def _host_port(base_url: str) -> tuple[str, int]:
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(base_url if "://" in base_url else f"http://{base_url}")
        return parts.hostname or "", parts.port or 11434
    except ValueError:
        return "", 11434


def wakes(base_url: str) -> bool:
    """Is `base_url` on the machine Wake-on-LAN controls? Only that one can be
    woken — a MAC is configured for one host (`OLLAMA_SERVER`, else the host
    woken before, else whichever local model the user picked)."""
    from config.settings import settings
    from services.ollama_watchdog import _target_host

    if not settings.ollama_host_lan_mac_address_wol:
        return False
    target, (host, _port) = _target_host(), _host_port(base_url)
    return bool(host) and (not target or target == host)


async def ensure_awake(base_url: str, *, on_wait=None, should_stop=None, why: str = "a chat") -> str:
    """Make sure the local model host at `base_url` answers, waking it if it
    does not. Returns:

    - "up": it answered already;
    - "woken": it was off, the packet went out, and it answers now;
    - "not_ours": it does not answer and is not the host WoL controls;
    - "timeout": woken, but it did not answer within `OLLAMA_WAKE_TIMEOUT_S`;
    - "stopped": `should_stop()` said so while waiting.

    `on_wait(seconds)` is called about every two seconds while it boots. The
    wake is recorded as a chat's ("chat"), so the host sleeps again after
    `OLLAMA_CHAT_IDLE_SHUTDOWN_MINUTES` without use — a research run's requests
    keep it awake while it works. `why` is for the log.
    """
    import asyncio
    import time

    from config.settings import settings
    from services.ollama_watchdog import _host_reachable, _log

    host, port = _host_port(base_url)
    if not host:
        return "not_ours"
    if await _host_reachable(host, port):
        return "up"
    if not wakes(base_url):
        return "not_ours"
    wake(settings.ollama_host_lan_mac_address_wol, base_url, by="chat")
    _log(f"waking {host} for {why}")
    started = time.monotonic()
    while (waited := time.monotonic() - started) < settings.ollama_wake_timeout_s:
        if should_stop and should_stop():
            return "stopped"
        if on_wait:
            on_wait(int(waited))
        await asyncio.sleep(2.0)
        if await _host_reachable(host, port):
            _log(f"{host} answers after {int(time.monotonic() - started)} s")
            return "woken"
    _log(f"{host} did not answer within {settings.ollama_wake_timeout_s} s of the wake")
    return "timeout"
