# config/deprecations.py
"""Settings that no longer do anything — warn once, never crash.

The built-in Wake-on-LAN, idle shutdown and dashboard power buttons were
removed: a sleeping GPU host gets a wake-on-request proxy in front of it
instead (docs/operations.md, "Sleeping GPU host").
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

REMOVED_POWER_SETTINGS = (
    "OLLAMA_HOST_LAN_MAC_ADDRESS_WOL",
    "OLLAMA_SSH_USER",
    "OLLAMA_IDLE_SHUTDOWN_MINUTES",
    "OLLAMA_CHAT_IDLE_SHUTDOWN_MINUTES",
    "OLLAMA_WAKE_TIMEOUT_S",
    "SHUTDOWN_PASSWORD",
    "PROACTIVE_WOL_AUTO",
    "PROACTIVE_IDLE_SHUTDOWN_MINUTES",
)

_warned = False


def _env_file_values(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip().removeprefix("export ").strip()] = v.strip().strip("\"'")
    except OSError:
        pass
    return out


def set_removed_power_settings(env_file: str = ".env") -> list[str]:
    """Names of removed power settings that still carry a value."""
    file_vals = _env_file_values(Path(env_file))
    return [n for n in REMOVED_POWER_SETTINGS
            if (os.environ.get(n) or file_vals.get(n) or "").strip()]


def warn_removed_settings_once() -> None:
    global _warned
    if _warned:
        return
    _warned = True
    names = set_removed_power_settings()
    if names:
        log.warning(
            "%s no longer do anything: paperless-brain has no built-in Wake-on-LAN or idle "
            "shutdown any more. Put a wake-on-request proxy in front of a sleeping GPU host "
            "and point OLLAMA_SERVER at it (docs/operations.md, \"Sleeping GPU host\"). "
            "Remove these settings to silence this warning.", ", ".join(names))
