# app_ui/theme.py
"""Dark/Light theming via CSS custom properties.

The app was authored dark-only with hardcoded colors. This module introduces a
single palette of semantic CSS variables (--c-*) with a dark default and a light
override keyed on Quasar's ``body--light`` class (added automatically when
``ui.dark_mode`` is disabled). Neutral colors throughout app_ui use ``var(--c-*)``
(hex sweep), and the common Tailwind gray utility classes are remapped here, so a
single toggle flips the whole UI.

Accent colors (steel brand, red/green/amber/blue status) are intentionally left
as-is — they read acceptably on both backgrounds.

The theme belongs to the device, not the user: dark on the desktop and light
on the phone is a normal combination. It lives in the ``pb_theme`` cookie, which
the server reads while rendering (``ui.dark_mode`` and the editor theme are set
server-side, so localStorage would only arrive after the first paint). A device
without the cookie falls back to the per-user ``get_pref("theme")`` — what the
setting was before it became per device — then to light.
"""

from nicegui import ui

from services.user_prefs import get_pref

DEFAULT_THEME = "light"
THEME_COOKIE = "pb_theme"
_THEMES = ("dark", "light")

# The one accent. Kept as a literal because ui.colors() writes --q-primary
# directly and cannot take a CSS var; it must stay in step with --c-accent below.
BRAND_PRIMARY = "#3563d9"

THEME_CSS = """
<style>
:root {
  /* Dark: graphite, no blue tint. */
  --c-bg:            #0f1012;
  --c-bg-deep:       #0b0c0d;
  --c-surface:       #16171a;
  --c-surface-2:     #1c1d21;
  --c-surface-3:     #222328;
  --c-border:        #27292e;
  --c-border-strong: #363840;
  --c-text:          #ececee;
  --c-text-2:        #c3c5cb;
  --c-text-muted:    #8a8d95;
  /* Warning = "you must react to this". Reserved for genuine state (the card
     action flag); never spent on identity or category. */
  --c-warn:          #fbbf24;
  --c-warn-bg:       rgba(251,191,36,0.12);
  /* The one accent (steel). Solid for actions and the active state, -text for
     links and accent words, -soft/-border for the user's own messages and the
     reference pills. --c-logo-2 is the leaf's lighter half. */
  --c-accent:        #4f7cf3;
  --c-accent-text:   #8fb0ff;
  --c-accent-soft:   rgba(79,124,243,0.16);
  --c-accent-border: rgba(79,124,243,0.45);
  --c-logo-2:        #3a5bb0;
  /* Marked passages on a PDF page: a highlighter, readable on white paper. */
  --c-hl:            rgba(250,204,21,0.32);
  --c-hl-line:       rgba(234,179,8,0.8);
  --c-hl-soft:       rgba(250,204,21,0.14);
  --q-primary:       #4f7cf3;
  /* Tag chips: hue from Paperless (app_ui/tag_style.py), lightness and chroma
     per theme. Light text on a dark tint here, dark text on a light tint below. */
  --tag-fg-l: 0.79; --tag-fg-c: 0.11;
  --tag-bg-l: 0.55; --tag-bg-c: 0.10; --tag-bg-a: 0.18;
  /* Topic colours the user picks for findings (card outline) — eight hues in a
     fixed, colour-blind-checked order, stepped per theme. */
  --topic-1: #3987e5; --topic-2: #d95926; --topic-3: #199e70; --topic-4: #c98500;
  --topic-5: #d55181; --topic-6: #008300; --topic-7: #9085e9; --topic-8: #e66767;
  /* Findings overview: what waits vs. what calls for action. */
  --viz-calm: #3987e5; --viz-act: #e66767;
}
body.body--light {
  --topic-1: #2a78d6; --topic-2: #eb6834; --topic-3: #1baf7a; --topic-4: #eda100;
  --topic-5: #e87ba4; --topic-6: #008300; --topic-7: #6250d6; --topic-8: #e34948;
  --viz-calm: #2a78d6; --viz-act: #e34948;
  --tag-fg-l: 0.45; --tag-fg-c: 0.12;
  --tag-bg-l: 0.72; --tag-bg-c: 0.08; --tag-bg-a: 0.30;
  /* Light: soft paper grey; no surface is pure white, which is what glared. */
  --c-bg:            #ebedf0;
  --c-bg-deep:       #e2e5e9;
  --c-surface:       #f6f7f9;
  --c-surface-2:     #eff1f4;
  --c-surface-3:     #e6e9ee;
  --c-border:        #d9dde3;
  --c-border-strong: #c3c9d1;
  --c-text:          #1a1d23;
  --c-text-2:        #3a404a;
  --c-text-muted:    #687080;
  --c-warn:          #b45309;
  --c-warn-bg:       rgba(180,83,9,0.10);
  --c-accent:        #3563d9;
  --c-accent-text:   #2a55c4;
  --c-accent-soft:   rgba(53,99,217,0.10);
  --c-accent-border: rgba(53,99,217,0.35);
  --c-logo-2:        #9db4ee;
  --q-primary:       #3563d9;
}

/* Accent as classes. Never Tailwind/Quasar purple-* or color="purple": Quasar's
   colour classes are !important inside a CSS layer, which no override outside
   it can beat, so they would stay violet forever. */
.pb-accent-text   { color: var(--c-accent-text) !important; }
.pb-warn-text     { color: var(--c-warn) !important; }
.pb-accent-border { border-color: var(--c-accent-border) !important; }

/* Inter, shipped with the app (no font CDN). */
@font-face {
  font-family: 'Inter'; font-style: normal; font-weight: 100 900; font-display: swap;
  src: url('/static/fonts/inter-latin-wght-normal.woff2') format('woff2');
}
body, .q-btn, .q-field, .q-tab, .q-item, .q-table, .nicegui-content {
  font-family: 'Inter', system-ui, -apple-system, 'Segoe UI', sans-serif;
}
body { font-feature-settings: "cv11", "ss01"; letter-spacing: -0.006em; }

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
body.body--light .q-table--dark tbody tr:hover td { background: var(--c-accent-soft) !important; }

/* Header (light): own surface, neutral icons, a border instead of a shadow */
body.body--light .app-header .q-icon { color: var(--c-text-2) !important; }
.app-header { box-shadow: none !important; }
/* Cards are separated by their border, not a shadow. */
.q-card { box-shadow: none !important; }

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

/* Top navigation — one signal for "active", not three. The accent plus
   the underline carry it; the underline alone survives greyscale and
   colour-blindness, which is why it is not colour-only. */
.app-header .desktop-nav-btn { color: var(--c-text-2) !important; }
.app-header .desktop-nav-btn .q-icon { color: inherit !important; }
.app-header .desktop-nav-btn:hover { color: var(--c-text) !important; }
.app-header .desktop-nav-btn.nav-active,
.app-header .desktop-nav-btn.nav-active .q-icon { color: var(--c-text) !important; }
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
/* Pinned is an active selection — one of the things the accent means. */
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
   the kind of state the user must react to. The pulse is the only motion.

   The note editor's voice button is the same control and gets the same rules,
   under its own class rather than by reusing `.memo-record-btn`: both dialogs
   exist on the note page at once, and the memo recorder finds its button with
   `document.querySelector('.memo-record-btn:not(.memo-quick-btn)')` — sharing
   the class would let it bind to the wrong one. */
.memo-record-btn,
.nvedit-record-btn { background: var(--c-surface-2) !important; }
.memo-record-btn .q-icon,
.nvedit-record-btn .q-icon { color: var(--c-text-2) !important; transition: color .12s; }
.memo-record-btn:hover .q-icon,
.nvedit-record-btn:hover .q-icon { color: var(--c-text) !important; }
.memo-record-btn.memo-recording,
.nvedit-record-btn.nvedit-recording { background: var(--c-warn-bg) !important; }
.memo-record-btn.memo-recording .q-icon,
.nvedit-record-btn.nvedit-recording .q-icon { color: var(--c-warn) !important; }
.memo-record-btn.memo-recording,
.nvedit-record-btn.nvedit-recording { animation: memo-pulse 1.4s ease-in-out infinite; }
@keyframes memo-pulse { 0%,100% { opacity: 1; } 50% { opacity: .62; } }
@media (prefers-reduced-motion: reduce) {
  .memo-record-btn.memo-recording,
  .nvedit-record-btn.nvedit-recording { animation: none; }
}
/* Locked (swiped up): still live, so still --c-warn, but the pulse stops and a
   ring takes over — the button is now a stop button waiting for a tap, not a
   thumb that must stay put. */
.memo-record-btn.memo-locked,
.nvedit-record-btn.nvedit-locked { animation: none; box-shadow: 0 0 0 2px var(--c-warn); }
/* Nothing on this button should start a text selection or a scroll: the whole
   vertical drag belongs to the swipe-to-lock gesture. */
.memo-record-btn,
.nvedit-record-btn { touch-action: none; user-select: none; -webkit-user-select: none; }

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

/* Settings groups: the collapsed header is the whole page at a glance, so it
   reads as a bar, not as a list row. Icons stay neutral like everywhere else —
   they name the group, they do not signal state. */
.settings-group-header {
  background: var(--c-surface) !important;
  border: 1px solid var(--c-border) !important;
  border-radius: 8px;
  color: var(--c-text) !important;
  min-height: 60px;
}
.settings-group-header .q-icon { color: var(--c-text-2) !important; }
.settings-group-header .q-item__label { font-weight: 600; }
.settings-group-header .q-item__label--caption { color: var(--c-text-muted) !important; font-weight: 400; }
/* Open: square off the bottom so the header and its contents read as one block. */
.settings-group.q-expansion-item--expanded .settings-group-header,
.settings-group .q-expansion-item--expanded .settings-group-header {
  border-bottom-left-radius: 0; border-bottom-right-radius: 0;
}
.settings-group .q-expansion-item__content { padding-bottom: 0.25rem; }
/* Nothing inside a settings card may widen it past the viewport. Monospace
   strings are the usual offender — paths, URLs and model ids have no spaces,
   so a browser treats each as one unbreakable word. */
/* File uploaders follow the theme — Quasar's `dark` uploader is black, and the
   default header is a solid primary bar. A neutral drop area instead. */
.q-uploader { background: var(--c-surface-2) !important; color: var(--c-text) !important;
    border: 1px dashed var(--c-border-strong); border-radius: 12px; box-shadow: none !important; }
.q-uploader__header { background: transparent !important; color: var(--c-text) !important; }
.q-uploader__subtitle { color: var(--c-text-muted); }
.q-uploader__list { background: transparent; min-height: 0; padding: 0 8px; }
.settings-group .q-card { max-width: 100%; min-width: 0; }
.settings-group code,
.settings-group .font-mono { overflow-wrap: anywhere; }
</style>
"""


def current_theme() -> str:
    """This device's theme: cookie, else the user's old preference, else light.

    MUST run inside a page context."""
    try:
        value = ui.context.client.request.cookies.get(THEME_COOKIE)
    except (AttributeError, RuntimeError):
        value = None
    if value in _THEMES:
        return value
    value = get_pref("theme", DEFAULT_THEME)
    return value if value in _THEMES else DEFAULT_THEME


def set_device_theme_js(theme: str) -> str:
    """JS that stores ``theme`` for this browser for a year."""
    return (
        f"document.cookie='{THEME_COOKIE}={theme}; path=/; max-age=31536000; SameSite=Lax';"
    )


def apply_theme() -> None:
    """Inject theme CSS and set dark/light from the user's stored preference.

    MUST run inside a page context (reads the user preferences). Call once per page
    from page_layout()/login.
    """
    theme = current_theme()
    ui.dark_mode(theme != "light")  # True → dark (body--dark), False → light (body--light)
    # Quasar's `primary` is the app's brand steel, not its stock blue. Without
    # this, every ui.button() (which defaults to color='primary') picks up
    # `.bg-primary{background:var(--q-primary)!important}` — and that !important
    # beats any Tailwind colour class a page sets. --q-primary is redefined
    # per theme in THEME_CSS (dark uses a lighter steel). Checked
    # checkboxes and other Quasar accents follow the same token.
    ui.colors(primary=BRAND_PRIMARY)
    ui.add_head_html(THEME_CSS)
