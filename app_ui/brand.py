"""The PaperlessBrain mark, word mark and browser/PWA head tags — one place.

The mark is a brain folded from paper: two hemispheres, the near one in the full
tone, the far one light, the folds drawn as creases in the ground colour. Inline
SVG, not an <img>, so the header logo can never show a broken-image icon and
follows the theme without a second file.

Icon files in app_ui/static are rendered from the same paths by
``scripts/make_icons.py``. Their names carry a version (``-v3``): installed
PWAs and browsers cache icons by URL, so a new look needs a new URL.
"""

# 64-unit grid. {c} near hemisphere, {c2} far one, {bg} the ground the creases
# are cut from. (A leaf was tried first and dropped: Paperless-ngx and
# Paperless-AI both already have one.)
_MARK = (
    '<path d="M31 9C21 8 13 14 13 22C7 25 6 34 10 39C8 46 13 54 22 54C25 57 29 57 31 55Z" fill="{c}"/>'
    '<path d="M33 9C43 8 51 14 51 22C57 25 58 34 54 39C56 46 51 54 42 54C39 57 35 57 33 55Z" fill="{c2}"/>'
    '<path d="M20 21L27 27M14 35L24 34M21 45L27 40" stroke="{bg}" stroke-width="3" stroke-linecap="round"/>'
    '<path d="M44 21L37 27M50 35L40 34M43 45L37 40" stroke="{bg}" stroke-width="3" '
    'stroke-linecap="round" opacity=".55"/>'
)

BRAND = "#3563d9"  # steel; = --c-accent in light mode
BRAND_DARK_BG = "#0f1012"
BRAND_LIGHT_BG = "#ebedf0"
ICON_VERSION = "v3"


def mark_svg(
    size: int | str,
    c: str = "var(--c-accent)",
    c2: str = "var(--c-logo-2)",
    bg: str = "var(--c-surface)",
) -> str:
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 64 64" aria-hidden="true">'
        + _MARK.format(c=c, c2=c2, bg=bg)
        + "</svg>"
    )


def wordmark_html(size_rem: float = 1.1, mark_px: int = 28, bg: str = "var(--c-surface)") -> str:
    """Mark plus "PaperlessBrain" as one word: weight and colour separate the
    halves, not a gap. Two spans side by side in a flex row read as
    "Paperless  Brain" — the row's gap fell between them."""
    return (
        '<a href="/" class="pb-brand" style="display:flex;align-items:center;gap:8px;'
        'user-select:none;text-decoration:none;">'
        + mark_svg(mark_px, bg=bg)
        + f'<span class="logo-text" style="font-size:{size_rem}rem;letter-spacing:-.02em;'
        'color:var(--c-text);white-space:nowrap;">'
        '<span style="font-weight:700;">Paperless</span>'
        '<span style="font-weight:500;color:var(--c-accent-text);">Brain</span></span></a>'
    )


def head_html() -> str:
    """Favicon, app icons, manifest and browser bar colour — for every page."""
    v = ICON_VERSION
    return (
        f'<meta name="theme-color" content="{BRAND_LIGHT_BG}" media="(prefers-color-scheme: light)">'
        f'<meta name="theme-color" content="{BRAND_DARK_BG}" media="(prefers-color-scheme: dark)">'
        f'<link rel="icon" type="image/svg+xml" href="/static/favicon-{v}.svg">'
        f'<link rel="icon" type="image/png" sizes="192x192" href="/static/icon-{v}-192.png">'
        f'<link rel="icon" type="image/png" sizes="512x512" href="/static/icon-{v}-512.png">'
        f'<link rel="apple-touch-icon" href="/static/apple-touch-icon-{v}.png">'
        '<link rel="manifest" href="/manifest.json">'
    )
