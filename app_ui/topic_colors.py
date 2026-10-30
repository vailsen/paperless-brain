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


#: The same hues as the `--topic-*` tokens, for drawing on a canvas (ECharts),
#: which cannot read CSS variables. Keep in step with `app_ui/theme.py`.
HEX = {
    "light": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
    "dark": ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
}


def tint(slot: int, theme: str, surface: str, share: float) -> str:
    """The slot's hue mixed into `surface` — a fill that text stays readable on."""
    hue = HEX.get(theme, HEX["light"])[int(slot) - 1]
    a = [int(hue[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(surface[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * share + y * (1 - share)):02x}" for x, y in zip(a, b))
