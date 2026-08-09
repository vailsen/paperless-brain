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

# The one accent. Kept as a literal because ui.colors() writes --q-primary
# directly and cannot take a CSS var; it must stay in step with --c-accent below.
BRAND_PRIMARY = "#7c3aed"

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
  /* Warning = "you must react to this". Reserved for genuine state (the card
     action flag); never spent on identity or category. */
  --c-warn:          #fbbf24;
  --c-warn-bg:       rgba(251,191,36,0.12);
  --c-accent:        #a855f7;
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
  --c-warn:          #b45309;
  --c-warn-bg:       rgba(180,83,9,0.10);
  --c-accent:        #7c3aed;
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
body.body--light .app-header .q-icon { color: var(--c-text-2) !important; }
/* Dark header keeps its subtle elevation too */
.app-header { box-shadow: 0 1px 3px rgba(0,0,0,0.12); }

/* Quasar ships `text-transform: uppercase` on .q-btn and .q-tab, so editing a
   Python label string has no visible effect — the caps come from CSS. Undo it
   once, globally, rather than remembering `no-caps` on every future button.
   Section eyebrows and table headers set their own inline uppercase and are
   unaffected: an eyebrow labels a region, a button names an action. */
.q-btn__content, .q-tab__label { text-transform: none !important; }

/* ── Visual discipline: colour encodes state, not identity ─────────────────
   Icons inherit text colour. Tag chips are the one exception (their hue comes
   from Paperless and enables cross-document grouping) and are styled inline by
   app_ui/tag_style.py. Everything below is deliberately hueless. */

/* Top navigation — one signal for "active", not three. The purple accent plus
   the underline carry it; the underline alone survives greyscale and
   colour-blindness, which is why it is not colour-only. */
.app-header .desktop-nav-btn { color: var(--c-text-2) !important; }
.app-header .desktop-nav-btn .q-icon { color: inherit !important; }
.app-header .desktop-nav-btn:hover { color: var(--c-text) !important; }
.app-header .desktop-nav-btn.nav-active,
.app-header .desktop-nav-btn.nav-active .q-icon { color: var(--c-accent) !important; }
.app-header .desktop-nav-btn.nav-active { box-shadow: inset 0 -2px 0 0 var(--c-accent); border-radius: 3px; }
/* Mobile nav menu entries follow the same rule */
.q-menu .nav-menu-item .q-icon { color: var(--c-text-muted) !important; }
.q-menu .nav-menu-item.nav-active,
.q-menu .nav-menu-item.nav-active .q-icon { color: var(--c-accent) !important; }

/* Document card action icons: muted at rest, lifting on card hover, brightest
   on the icon itself. Three steps so the targets stay discoverable without all
   24 of them competing for attention at once. */
.doc-card-body { cursor: pointer; }
.card-action-btn .q-icon { color: var(--c-text-muted) !important; transition: color .12s; }
.doc-card:hover .card-action-btn .q-icon { color: var(--c-text-2) !important; }
.card-action-btn:hover .q-icon { color: var(--c-text) !important; }
/* Pinned is an active selection — the one thing purple is still allowed to mean. */
.card-action-btn.is-pinned .q-icon,
.doc-card:hover .card-action-btn.is-pinned .q-icon { color: var(--c-accent) !important; }

/* The action flag is system state, not a tag. With the tag row neutralised it
   is the only saturated element on a card that has one. */
.card-action-flag {
  font-size: 0.65rem; font-weight: 600; letter-spacing: .01em;
  color: var(--c-warn); background: var(--c-warn-bg);
  border-radius: 4px; padding: 1px 6px; line-height: 1.5;
}
.card-action-flag-icon { color: var(--c-warn) !important; }

/* Voice memo. The record button is neutral at rest like every other icon; while
   recording it carries --c-warn, because "your microphone is live" is exactly
   the kind of state the user must react to. The pulse is the only motion. */
.memo-record-btn { background: var(--c-surface-2) !important; }
.memo-record-btn .q-icon { color: var(--c-text-2) !important; transition: color .12s; }
.memo-record-btn:hover .q-icon { color: var(--c-text) !important; }
.memo-record-btn.memo-recording { background: var(--c-warn-bg) !important; }
.memo-record-btn.memo-recording .q-icon { color: var(--c-warn) !important; }
.memo-record-btn.memo-recording { animation: memo-pulse 1.4s ease-in-out infinite; }
@keyframes memo-pulse { 0%,100% { opacity: 1; } 50% { opacity: .62; } }
@media (prefers-reduced-motion: reduce) { .memo-record-btn.memo-recording { animation: none; } }
/* Locked (swiped up): still live, so still --c-warn, but the pulse stops and a
   ring takes over — the button is now a stop button waiting for a tap, not a
   thumb that must stay put. */
.memo-record-btn.memo-locked { animation: none; box-shadow: 0 0 0 2px var(--c-warn); }
/* Nothing on this button should start a text selection or a scroll: the whole
   vertical drag belongs to the swipe-to-lock gesture. */
.memo-record-btn { touch-action: none; user-select: none; -webkit-user-select: none; }

/* A transcribed conversation is far longer than a memo, so the card needs a
   ceiling: without one the autogrow textarea grows the dialog past the viewport
   and the Save/Discard row ends up off-screen. The card is already a flex
   column (.nicegui-card), so capping its height and letting exactly one child
   scroll is enough — everything else keeps its natural size and stays visible. */
/* dvh second so it wins where supported: on mobile `vh` ignores the browser's
   collapsing toolbar, which is exactly where the buttons went missing. */
.memo-card { max-height: 88vh; max-height: 88dvh; overflow: hidden; }
.memo-card > * { flex-shrink: 0; }
/* `flex: 0 1 auto` not `1 1 0`: basis auto keeps a short memo at its natural
   height (no empty gap above the buttons), while still shrinking and scrolling
   once the card hits its ceiling. min-height:0 is what permits that shrink. */
.memo-card > .memo-text-scroll { flex: 0 1 auto; min-height: 0; overflow-y: auto; }

/* Dashboard card footer icons: monochrome. The status dots stay coloured —
   those are genuine state. */
.dash-card .footer-icon .q-icon { color: var(--c-text-muted) !important; }
.dash-card .footer-icon:hover .q-icon { color: var(--c-text) !important; }

/* Suggestion chips: fade the right edge so the clipped chip reads as
   "more content" rather than "broken layout". A scroll listener mirrors the
   mask on the left once the row has left its origin, and drops it at the end
   so the last chip is not permanently half-faded. */
.suggestion-row {
  -webkit-mask-image: linear-gradient(to right, black calc(100% - 40px), transparent 100%);
          mask-image: linear-gradient(to right, black calc(100% - 40px), transparent 100%);
}
.suggestion-row.scrolled {
  -webkit-mask-image: linear-gradient(to right, transparent 0, black 40px, black calc(100% - 40px), transparent 100%);
          mask-image: linear-gradient(to right, transparent 0, black 40px, black calc(100% - 40px), transparent 100%);
}
.suggestion-row.at-end {
  -webkit-mask-image: linear-gradient(to right, transparent 0, black 40px, black 100%);
          mask-image: linear-gradient(to right, transparent 0, black 40px, black 100%);
}
/* Everything fits — no clipped chip, so nothing to hint at. */
.suggestion-row.no-mask { -webkit-mask-image: none; mask-image: none; }
.suggestion-chip .q-icon { color: var(--c-text-muted) !important; font-size: 15px !important; }
.suggestion-chip .q-btn__content { gap: 5px; }
</style>
"""


def apply_theme() -> None:
    """Inject theme CSS and set dark/light from the user's stored preference.

    MUST run inside a page context (reads app.storage.user). Call once per page
    from page_layout()/login.
    """
    theme = ng_app.storage.user.get("theme", DEFAULT_THEME)
    ui.dark_mode(theme != "light")  # True → dark (body--dark), False → light (body--light)
    # Quasar's `primary` is the app's brand purple, not its stock blue. Without
    # this, every ui.button() (which defaults to color='primary') picks up
    # `.bg-primary{background:var(--q-primary)!important}` — and that !important
    # beats the Tailwind `bg-purple-700` classes the pages set, so twenty
    # buttons that read purple in the source rendered blue on screen. Checked
    # checkboxes and other Quasar accents follow the same token.
    ui.colors(primary=BRAND_PRIMARY)
    ui.add_head_html(THEME_CSS)
