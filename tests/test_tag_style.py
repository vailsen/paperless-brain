"""Tag chip colour normalisation.

The point of normalize_tag_color() is that no tag can outshout another, so the
tests assert equal visual weight across hues rather than exact values.

Round 1 worked in HSL plus a hand-tuned per-hue correction table, and green still
read hot against blue and pink. These now assert the OKLCh invariant instead:
identical perceptual lightness for every hue, by construction.
"""

import re

import pytest

from app_ui.tag_style import (
    _BG_L,
    _TEXT_C,
    _TEXT_L,
    fallback_hue,
    hex_to_oklch_hue,
    normalize_tag_color,
)

_TEXT_RE = re.compile(r"oklch\(([\d.]+) ([\d.]+) ([\d.]+)\)")
_BG_RE = re.compile(r"oklch\(([\d.]+) ([\d.]+) ([\d.]+) / ([\d.]+)\)")

# One saturated sample per region of the wheel, including the two that HSL
# systematically over-brightens.
HUES = {
    "yellow": "#ffff00",
    "green": "#00ff00",
    "cyan": "#00ffff",
    "blue": "#3778dd",
    "violet": "#8b5cf6",
    "pink": "#ec4899",
    "red": "#ef4444",
    "orange": "#f97316",
}


def _text_parts(css: str) -> tuple[float, float, float]:
    m = _TEXT_RE.fullmatch(css)
    assert m, f"unexpected text colour format: {css}"
    return float(m.group(1)), float(m.group(2)), float(m.group(3))


@pytest.mark.parametrize("name,hex_color", HUES.items())
def test_every_hue_gets_identical_perceptual_lightness(name, hex_color):
    """The whole point: equal L in OKLab means equal apparent brightness."""
    _, text = normalize_tag_color(hex_color)
    lightness, chroma, _hue = _text_parts(text)
    assert lightness == _TEXT_L
    assert chroma == _TEXT_C


def test_yellow_and_blue_are_indistinguishable_in_weight():
    """Round 1's headline failure — yellow burning a hole in the card."""
    _, yellow = normalize_tag_color("#ffff00")
    _, blue = normalize_tag_color("#3778dd")
    assert _text_parts(yellow)[0] == _text_parts(blue)[0]


def test_green_no_longer_reads_hotter_than_blue_or_pink():
    """The round-2 regression report: the green chip sat visibly brighter."""
    lightnesses = {
        n: _text_parts(normalize_tag_color(h)[0 + 1])[0]
        for n, h in (("green", "#00ff00"), ("blue", "#3778dd"), ("pink", "#ec4899"))
    }
    assert len(set(lightnesses.values())) == 1, lightnesses


def test_hues_stay_distinct():
    """Normalisation must not collapse the wheel — tags stay groupable by colour."""
    hues = [_text_parts(normalize_tag_color(h)[1])[2] for h in HUES.values()]
    assert len(set(hues)) == len(hues)


def test_hue_is_preserved_through_normalisation():
    source = hex_to_oklch_hue("#3778dd")
    _, text = normalize_tag_color("#3778dd")
    assert abs(_text_parts(text)[2] - source) < 0.1


def test_background_is_the_same_hue_at_lower_lightness_and_alpha():
    bg, text = normalize_tag_color("#3778dd")
    m = _BG_RE.fullmatch(bg)
    assert m, f"unexpected background format: {bg}"
    bg_l, _bg_c, bg_hue, alpha = (float(g) for g in m.groups())
    assert bg_l == _BG_L
    assert bg_l < _text_parts(text)[0]
    assert alpha < 1.0
    assert abs(bg_hue - _text_parts(text)[2]) < 0.1


def test_shorthand_hex_is_expanded():
    assert normalize_tag_color("#0f0") == normalize_tag_color("#00ff00")


def test_hash_missing_is_accepted():
    assert normalize_tag_color("ffff00") == normalize_tag_color("#ffff00")


def test_achromatic_input_does_not_crash():
    for grey in ("#000000", "#ffffff", "#808080"):
        bg, text = normalize_tag_color(grey)
        assert bg.startswith("oklch(") and text.startswith("oklch(")


def test_fallback_hue_is_stable_across_processes():
    """crc32, not the salted built-in hash: the same tag must survive a restart."""
    assert fallback_hue("Rechnung") == fallback_hue("Rechnung")
    assert fallback_hue("Rechnung") != fallback_hue("Vertrag")
    assert 0 <= fallback_hue("Rechnung") < 360
