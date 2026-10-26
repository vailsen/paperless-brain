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


def wake(mac: str, server_url: str) -> str:
    """Send the packet and record the wake in the shared power state. Returns
    the broadcast address used. Raises on a bad MAC or a socket error."""
    from services.ollama_watchdog import on_wol

    broadcast = broadcast_for(server_url)
    send_magic_packet(mac, broadcast)
    on_wol(host_ip(server_url))
    return broadcast
