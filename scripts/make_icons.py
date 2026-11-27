"""Render the app icons from the mark in app_ui/brand.py (dev only, needs Playwright).

    python scripts/make_icons.py

Writes app_ui/static/{favicon,icon-*,apple-touch-icon}-<ICON_VERSION>.*  Bump
ICON_VERSION in brand.py first when the mark changes — installed PWAs keep an
icon until its URL changes.
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app_ui.brand import BRAND, ICON_VERSION, SHORTCUT_ICON_VERSION, mark_svg  # noqa: E402

STATIC = ROOT / "app_ui" / "static"
WHITE, WHITE_2 = "#ffffff", "rgba(255,255,255,.62)"


def tile_svg(rounded: bool, mark: float) -> str:
    """64-unit tile: steel ground, white mark at ``mark`` of the width.
    ``rounded=False`` is the maskable variant — the launcher cuts its own shape,
    so the ground must reach every edge and the mark stay in the safe circle."""
    r = 14 if rounded else 0
    inner = mark_svg(64, WHITE, WHITE_2, BRAND).replace('width="64" height="64" ', "")
    off = 32 * (1 - mark)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        f'<rect width="64" height="64" rx="{r}" fill="{BRAND}"/>'
        f'<g transform="translate({off} {off}) scale({mark})">{inner}</g></svg>'
    )


def badge_svg() -> str:
    """Android's status-bar badge: only the alpha channel counts, the system
    paints it white. The opaque tile came out as a white square, so this is the
    bare mark — hemispheres opaque, creases cut out through a mask."""
    inner = mark_svg(64, "#fff", "#fff", "#000").replace('width="64" height="64" ', "")
    inner = inner.replace("<svg ", "<svg x=\"4\" y=\"4\" width=\"56\" height=\"56\" ", 1)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        f'<defs><mask id="m" maskUnits="userSpaceOnUse" x="0" y="0" width="64" height="64">{inner}</mask></defs>'
        '<rect width="64" height="64" fill="#fff" mask="url(#m)"/></svg>'
    )


# Shortcut glyphs: Material Icons paths (Apache 2.0, the set the UI already
# uses through Quasar), on a 24-unit grid.
GLYPHS = {
    "chat": "M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2z",
    "note": "M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zm2 16H8v-2h8v2z"
            "m0-4H8v-2h8v2zm-3-5V3.5L18.5 9H13z",
    "memo": "M12 14c1.66 0 2.99-1.34 2.99-3L15 5c0-1.66-1.34-3-3-3S9 3.34 9 5v6c0 1.66 1.34 3 3 3z"
            "m5.3-3c0 3-2.54 5.1-5.3 5.1S6.7 14 6.7 11H5c0 3.41 2.72 6.23 6 6.72V21h2v-3.28"
            "c3.28-.48 6-3.3 6-6.72h-1.7z",
}


def shortcut_svg(glyph: str, maskable: bool) -> str:
    """The module's glyph, large and white on the app's tile.

    No brain mark and nothing in the bottom-right corner: Android paints the
    app's own badge there on every pinned shortcut (a web app cannot turn it
    off), and it covered the corner badge of the first design. The glyph sits a
    little up and to the left, clear of it. The maskable variant keeps it inside
    the 40 % safe circle."""
    r = 0 if maskable else 14
    size, c = (30.0, 30.5) if maskable else (36.0, 29.5)
    g = size / 24
    t = c - size / 2
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        f'<rect width="64" height="64" rx="{r}" fill="{BRAND}"/>'
        f'<path d="{GLYPHS[glyph]}" fill="{WHITE}" transform="translate({t} {t}) scale({g})"/>'
        "</svg>"
    )


async def main() -> None:
    from playwright.async_api import async_playwright

    v = ICON_VERSION
    (STATIC / f"favicon-{v}.svg").write_text(tile_svg(True, 0.7))
    jobs = [
        (tile_svg(True, 0.68), 192, f"icon-{v}-192.png"),
        (tile_svg(True, 0.68), 512, f"icon-{v}-512.png"),
        (tile_svg(False, 0.56), 512, f"icon-{v}-maskable-512.png"),
        (tile_svg(False, 0.62), 180, f"apple-touch-icon-{v}.png"),
        (badge_svg(), 96, f"badge-{v}-96.png"),
    ]
    for glyph in GLYPHS:
        for size in (96, 192):
            sv = SHORTCUT_ICON_VERSION
            jobs.append((shortcut_svg(glyph, False), size, f"shortcut-{glyph}-{sv}-{size}.png"))
            jobs.append((shortcut_svg(glyph, True), size, f"shortcut-{glyph}-{sv}-maskable-{size}.png"))
    async with async_playwright() as p:
        b = await p.chromium.launch()
        for svg, size, name in jobs:
            pg = await b.new_page(viewport={"width": size, "height": size})
            await pg.set_content(
                f'<html><body style="margin:0;background:transparent">'
                f'{svg.replace("<svg ", f"<svg width=\"{size}\" height=\"{size}\" ", 1)}</body></html>'
            )
            await pg.screenshot(path=str(STATIC / name), omit_background=True)
            await pg.close()
        await b.close()
    print("wrote", ", ".join(n for *_, n in jobs), f"favicon-{v}.svg")


if __name__ == "__main__":
    asyncio.run(main())
