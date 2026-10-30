"""Colours the user gives to finding topics — the outline of a finding's card.

Per user (`services/user_prefs`, key `topic_colors`: topic key → slot 1–8).
A slot names a theme token (`--topic-1` … `--topic-8`, `app_ui/theme.py`), not
a hex: eight hues in a fixed order checked for colour-blind separation, stepped
for the light and the dark theme. No colour is the default; the user decides
what is worth telling apart, as with Paperless tag colours.
"""

from __future__ import annotations

SLOTS = range(1, 9)


def colors() -> dict[str, int]:
    from services.user_prefs import get_pref

    raw = get_pref("topic_colors") or {}
    out = {}
    for topic, slot in raw.items() if isinstance(raw, dict) else []:
        try:
            if int(slot) in SLOTS:
                out[str(topic)] = int(slot)
        except (TypeError, ValueError):
            continue
    return out


def set_color(topic: str, slot: int | None) -> None:
    from services.user_prefs import set_pref

    current = colors()
    if slot in SLOTS:
        current[topic] = int(slot)
    else:
        current.pop(topic, None)
    set_pref("topic_colors", current)


def css_var(slot: int) -> str:
    return f"var(--topic-{int(slot)})"
