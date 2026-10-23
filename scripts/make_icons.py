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

from app_ui.brand import BRAND, ICON_VERSION, mark_svg  # noqa: E402

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


async def main() -> None:
    from playwright.async_api import async_playwright

    v = ICON_VERSION
    (STATIC / f"favicon-{v}.svg").write_text(tile_svg(True, 0.7))
    jobs = [
        (tile_svg(True, 0.68), 192, f"icon-{v}-192.png"),
        (tile_svg(True, 0.68), 512, f"icon-{v}-512.png"),
        (tile_svg(False, 0.56), 512, f"icon-{v}-maskable-512.png"),
        (tile_svg(False, 0.62), 180, f"apple-touch-icon-{v}.png"),
    ]
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
