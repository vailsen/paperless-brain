# app_ui/tag_style.py
"""Tag chip colour normalisation and rendering.

Paperless owns the *hue* of a tag — the user picked it and it is what makes tags
groupable across documents at a glance. Paperless does not own the saturation or
the lightness: a full-brightness yellow chip vibrates against the dark background
and outweighs the document title it sits under.

So we keep the hue and clamp the other two channels onto a fixed band. Every chip
then carries the same visual weight and no tag can shout louder than another.

The colour map is fetched from Paperless asynchronously and cached at module
level, because the card renderers are synchronous. Priming happens at startup
(``main.py``) and after a manual sync; a cold cache falls back to a deterministic
hue per tag name, never to a random one — the same tag must look the same on
every card and in every session.
"""

import asyncio
import math
import zlib

from nicegui import ui

# Fixed OKLCh band every chip is mapped onto. L is perceptual lightness, so equal
# L really does mean equal apparent brightness — that is the whole reason for
# working in OKLab rather than HSL.
_TEXT_L, _TEXT_C = 0.79, 0.11
_BG_L, _BG_C, _BG_ALPHA = 0.55, 0.10, 0.18


def _srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def hex_to_oklch_hue(hex_color: str) -> float:
    """Hue angle in degrees of a hex colour, in OKLab space.

    Only the hue survives normalisation, so this is the single value we take from
    whatever Paperless stored. Achromatic input has no meaningful hue; it returns
    0, which the caller renders as a grey chip via the near-zero chroma anyway.
    """
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (_srgb_to_linear(int(h[i:i + 2], 16) / 255) for i in (0, 2, 4))

    # sRGB(linear) → LMS → OKLab (Björn Ottosson's matrices).
    l_ = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m_ = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s_ = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    a = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    bb = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_
    return math.degrees(math.atan2(bb, a)) % 360


def normalize_tag_color(hex_color: str) -> tuple[str, str]:
    """Map an arbitrary Paperless tag hex onto a fixed perceptual lightness band.

    Returns (background_css, text_css) for the dark theme.
    Hue is preserved so tags stay visually distinguishable; everything else is normalised
    so no tag can shout louder than another.

    This works in OKLCh rather than HSL. HSL lightness is not perceptual — a yellow
    and a blue at the same HSL L differ by roughly 30 % in apparent brightness, which
    is why the first pass needed a hand-tuned per-hue correction table and why green
    still read hot after it. OKLab is perceptually uniform by construction, so a fixed
    L is a fixed apparent brightness and the table disappears.
    """
    hue = round(hex_to_oklch_hue(hex_color), 1)
    return (
        f"oklch({_BG_L} {_BG_C} {hue} / {_BG_ALPHA})",
        f"oklch({_TEXT_L} {_TEXT_C} {hue})",
    )


# ── Fallback hue ──────────────────────────────────────────────────────────────

# 12 evenly spaced hues. Even spacing in OKLCh is perceptually even, so these are
# maximally distinguishable without any further tuning.
_FALLBACK_HUES = list(range(0, 360, 30))


def fallback_hue(tag_name: str) -> float:
    """Deterministic hue for a tag with no Paperless colour (or a failed fetch).

    zlib.crc32, not hash(): the built-in str hash is salted per process, which
    would reshuffle every tag's colour on each app restart.
    """
    return float(
        _FALLBACK_HUES[zlib.crc32(tag_name.encode("utf-8")) % len(_FALLBACK_HUES)]
    )


# ── Colour map cache ──────────────────────────────────────────────────────────

_tag_colors: dict[str, str] = {}
_lock = asyncio.Lock()


async def refresh_tag_colors() -> None:
    """Pull the tag colour map from Paperless into the module cache.

    Safe to call concurrently and safe to call when Paperless is down — a failure
    leaves whatever was cached before in place, because a chip that falls back to
    its hash hue mid-session looks like a different tag.
    """
    global _tag_colors
    async with _lock:
        try:
            from services.clients import paperless

            colors = await paperless.get_tag_colors()
        except Exception as exc:
            print(
                f"[tag_style] tag colour refresh failed "
                f"({type(exc).__name__}: {exc}) — keeping previous map",
                flush=True,
            )
            return
        if colors:
            _tag_colors = colors


def invalidate_tag_colors() -> None:
    """Force the next refresh to hit the API. Called from the manual sync button."""
    try:
        from services.clients import paperless

        paperless.invalidate_tag_colors()
    except Exception:
        pass


def tag_colors(tag_name: str) -> tuple[str, str]:
    """(background_css, text_css) for a tag, from Paperless or the fallback hue."""
    stored = _tag_colors.get(tag_name)
    if stored:
        try:
            return normalize_tag_color(stored)
        except (ValueError, IndexError):
            # A malformed hex in Paperless must not take a whole card down.
            pass
    hue = fallback_hue(tag_name)
    return (
        f"oklch({_BG_L} {_BG_C} {hue} / {_BG_ALPHA})",
        f"oklch({_TEXT_L} {_TEXT_C} {hue})",
    )


# ── Rendering ─────────────────────────────────────────────────────────────────

_CHIP_BASE = (
    "font-size:10px;line-height:1.4;padding:1px 7px;border-radius:999px;"
    "white-space:nowrap;max-width:100%;overflow:hidden;text-overflow:ellipsis;"
)

MAX_VISIBLE_TAGS = 4


def tag_chip(tag_name: str):
    """A single normalised tag chip."""
    bg, fg = tag_colors(tag_name)
    return ui.label(tag_name).style(f"{_CHIP_BASE}background:{bg};color:{fg};")


def render_tag_chips(tags, max_visible: int = MAX_VISIBLE_TAGS) -> None:
    """Render a document's tag row: at most ``max_visible`` chips plus a muted +N.

    Sorted alphabetically so the four that show are the same four on every render —
    a card that reshuffles its tags between page loads reads as broken.

    +N expands on *click*, not hover: hover expansion inside a scrolling panel is
    hostile on touch, where there is no hover to leave.
    """
    ordered = sorted(tags)
    if not ordered:
        return
    with ui.row().classes("flex-wrap gap-1 mt-1 items-center"):
        for tag in ordered[:max_visible]:
            tag_chip(tag)
        rest = ordered[max_visible:]
        if not rest:
            return
        hidden = []
        for tag in rest:
            el = tag_chip(tag)
            el.set_visibility(False)
            hidden.append(el)
        more = ui.label(f"+{len(rest)}").style(
            "font-size:10px;line-height:1.4;padding:1px 4px;"
            "color:var(--c-text-muted);cursor:pointer;"
        )

        def _expand() -> None:
            for el in hidden:
                el.set_visibility(True)
            more.set_visibility(False)

        more.on("click", _expand)
