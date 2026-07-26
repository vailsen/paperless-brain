# app_ui/theme.py
"""Dark/Light theming via CSS custom properties.

The app was authored dark-only with hardcoded colors. This module introduces a
single palette of semantic CSS variables (--c-*) with a dark default and a light
override keyed on Quasar's ``body--light`` class (added automatically when
``ui.dark_mode`` is disabled). Neutral colors throughout app_ui use ``var(--c-*)``
(hex sweep), and the common Tailwind gray utility classes are remapped here, so a
single toggle flips the whole UI.

Accent colors (purple brand, red/green/amber/blue status) are intentionally left
as-is — they read acceptably on both backgrounds.

Preference lives in ``app.storage.user["theme"]`` ("dark" | "light"), the same
encrypted per-user storage as the language setting. Default: dark.
"""

from nicegui import app as ng_app
from nicegui import ui

DEFAULT_THEME = "dark"

THEME_CSS = """
<style>
:root {
  --c-bg:            #111827;
  --c-bg-deep:       #0a0f1a;
  --c-surface:       #1f2937;
  --c-surface-2:     #172033;
  --c-surface-3:     #1e1b2e;
  --c-border:        #374151;
  --c-border-strong: #4b5563;
  --c-text:          #f1f5f9;
  --c-text-2:        #d1d5db;
  --c-text-muted:    #9ca3af;
}
body.body--light {
  --c-bg:            #f1f5f9;
  --c-bg-deep:       #e2e8f0;
  --c-surface:       #ffffff;
  --c-surface-2:     #f8fafc;
  --c-surface-3:     #f5f3ff;
  --c-border:        #e2e8f0;
  --c-border-strong: #cbd5e1;
  --c-text:          #0f172a;
  --c-text-2:        #334155;
  --c-text-muted:    #64748b;
}

html, body, .q-page, .nicegui-content { background: var(--c-bg) !important; }

/* Remap the Tailwind gray utility classes used across the app to theme vars,
   so class-based colors flip together with the var-based inline styles. */
.bg-gray-900 { background-color: var(--c-bg)        !important; }
.bg-gray-800 { background-color: var(--c-surface)    !important; }
.bg-gray-700 { background-color: var(--c-surface-2)  !important; }

.text-gray-100, .text-gray-200 { color: var(--c-text)       !important; }
.text-gray-300                 { color: var(--c-text-2)     !important; }
.text-gray-400, .text-gray-500, .text-gray-600 { color: var(--c-text-muted) !important; }

.border-gray-700, .border-gray-600 { border-color: var(--c-border) !important; }

/* Quasar surfaces in light mode: keep cards/menus/inputs on the surface color
   instead of Quasar's default dark. */
body.body--light .q-card,
body.body--light .q-field__control { background-color: var(--c-surface); }

/* ── Light-mode fixes for components authored with the Quasar `dark` prop ──
   The whole app hardcodes `dark` on inputs/selects/menus → white text/borders,
   invisible on a light background. Force readable colors in light mode. */
body.body--light .q-field--dark .q-field__native,
body.body--light .q-field--dark .q-field__input,
body.body--light .q-field--dark textarea,
body.body--light .q-field--dark input,
body.body--light .q-field--dark .q-field__prefix,
body.body--light .q-field--dark .q-field__suffix,
body.body--light .q-field--dark .q-select__dropdown-icon { color: var(--c-text) !important; }
body.body--light .q-field--dark .q-field__label,
body.body--light .q-field--dark .q-field__marginal,
body.body--light .q-field--dark .q-field__messages { color: var(--c-text-muted) !important; }
body.body--light .q-field--dark.q-field--outlined .q-field__control:before {
  border-color: var(--c-border-strong) !important;
}

/* Dropdown / context menus (rendered in a portal on <body>) */
body.body--light .q-menu {
  background: var(--c-surface) !important;
  color: var(--c-text) !important;
  border: 1px solid var(--c-border) !important;
}
body.body--light .q-menu .q-item,
body.body--light .q-menu .q-item__label,
body.body--light .q-menu .q-item__section { color: var(--c-text) !important; }
body.body--light .q-menu .q-item--active,
body.body--light .q-menu .q-item.q-manual-focusable--focused,
body.body--light .q-menu .q-item:hover { background: var(--c-surface-2) !important; }

body.body--light .q-checkbox__label,
body.body--light .q-radio__label,
body.body--light .q-toggle__label { color: var(--c-text-2) !important; }

/* Header: own surface + border so it reads as a distinct bar (both themes) */
.app-header { background: var(--c-surface) !important; border-bottom: 1px solid var(--c-border) !important; }
/* nav hover:text-white would vanish on a light header */
body.body--light .hover\\:text-white:hover { color: var(--c-text) !important; }

/* Bright accent text needs darkening for contrast on light backgrounds */
body.body--light .text-green-400, body.body--light .text-green-500 { color: #15803d !important; }
body.body--light .text-yellow-400 { color: #a16207 !important; }
body.body--light .text-blue-400   { color: #1d4ed8 !important; }
body.body--light .text-orange-400 { color: #c2410c !important; }

/* Dark accent badge backgrounds → light tints in light mode (model backend badges) */
body.body--light .bg-blue-950   { background-color: #dbeafe !important; }
body.body--light .bg-orange-950 { background-color: #ffedd5 !important; }
body.body--light .bg-blue-900   { background-color: #dbeafe !important; }
body.body--light .bg-purple-900, body.body--light .bg-purple-950 { background-color: #ede9fe !important; }

/* Expansion items authored with the `dark` prop */
body.body--light .q-expansion-item .q-item__label,
body.body--light .q-expansion-item__toggle-icon,
body.body--light .q-expansion-item .q-icon { color: var(--c-text) !important; }

/* Tables authored with the `dark` prop (q-table--dark) → light in light mode */
body.body--light .q-table--dark,
body.body--light .q-table--dark thead th,
body.body--light .q-table--dark tbody td,
body.body--light .q-table--dark tbody tr,
body.body--light .q-table--dark .q-table__bottom {
  background-color: transparent !important;
  color: var(--c-text-2) !important;
  border-color: var(--c-border) !important;
}
body.body--light .q-table--dark thead th { color: var(--c-text-muted) !important; }
body.body--light .q-table--dark tbody tr:hover td { background: rgba(124,58,237,0.06) !important; }

/* Header polish (light): clean white bar, crisp deep-purple icons, soft elevation */
body.body--light .app-header {
  background: #ffffff !important;
  border-bottom: 1px solid var(--c-border) !important;
  box-shadow: 0 1px 3px rgba(0,0,0,0.06);
}
body.body--light .app-header .nav-btn .q-icon,
body.body--light .app-header .q-icon { color: #7c3aed !important; }
/* Dark header keeps its subtle elevation too */
.app-header { box-shadow: 0 1px 3px rgba(0,0,0,0.12); }
</style>
"""


def apply_theme() -> None:
    """Inject theme CSS and set dark/light from the user's stored preference.

    MUST run inside a page context (reads app.storage.user). Call once per page
    from page_layout()/login.
    """
    theme = ng_app.storage.user.get("theme", DEFAULT_THEME)
    ui.dark_mode(theme != "light")  # True → dark (body--dark), False → light (body--light)
    ui.add_head_html(THEME_CSS)
