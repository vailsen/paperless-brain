"""The PaperlessBrain mark, word mark and browser/PWA head tags — one place.

The mark is a leaf folded from a sheet: two tones split along the midrib. Inline
SVG, not an <img>, so the header logo can never show a broken-image icon and
follows the theme without a second file.

Icon files in app_ui/static are rendered from the same paths by
``scripts/make_icons.py``. Their names carry a version (``-v2``): installed
PWAs and browsers cache icons by URL, so a new look needs a new URL.
"""

# 64-unit grid. {c} is the near half, {c2} the far, lighter half.
_LEAF = (
    '<path d="M10 54C8 30 26 10 54 10C54 30 34 50 10 54Z" fill="{c2}"/>'
    '<path d="M10 54C8 30 26 10 54 10L10 54Z" fill="{c}"/>'
)

BRAND = "#3563d9"  # steel; = --c-accent in light mode
BRAND_DARK_BG = "#0f1012"
BRAND_LIGHT_BG = "#ebedf0"
ICON_VERSION = "v2"


def leaf_svg(size: int | str, c: str = "var(--c-accent)", c2: str = "var(--c-logo-2)") -> str:
    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 64 64" aria-hidden="true">'
        + _LEAF.format(c=c, c2=c2)
        + "</svg>"
    )


def wordmark_html(size_rem: float = 1.1, mark_px: int = 28) -> str:
    """Mark plus "PaperlessBrain" as one word: weight and colour separate the
    halves, not a gap. Two spans side by side in a flex row read as
    "Paperless  Brain" — the row's gap fell between them."""
    return (
        '<a href="/" class="pb-brand" style="display:flex;align-items:center;gap:8px;'
        'user-select:none;text-decoration:none;">'
        + leaf_svg(mark_px)
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
