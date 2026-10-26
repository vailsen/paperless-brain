"""App-side registration of the proactive system.

`register()` runs once at app start. With `PROACTIVE_ENABLED=false` it does
nothing: no page, no route, no tool, no DB file. Every proactive page and
endpoint of the app is imported from here and nowhere else, so the switch has
one place to hold.
"""

from __future__ import annotations

import logging

log = logging.getLogger("proactive.app")


def register() -> bool:
    from config.proactive import proactive_settings

    cfg = proactive_settings()  # invalid config → the app refuses to start, loudly
    if not cfg.proactive_enabled:
        return False
    print("[startup] proactive system enabled", flush=True)
    return True
