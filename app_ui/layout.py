# app_ui/layout.py

from nicegui import app as ng_app
from nicegui import ui

from app_ui.theme import apply_theme
from config.version import __version__
from i18n import N_, get_translator
from services.session_auth import clear_session, get_session_token

_LOGO_HTML = (
    '<div style="display:flex;align-items:center;gap:9px;user-select:none;">'
    '<img src="/static/paperlessbrain_logo.png" style="height:32px;width:32px;object-fit:contain;">'
    '<span class="logo-text" style="font-size:1.1rem;font-weight:800;letter-spacing:-.025em;color:var(--c-text);">Paperless</span>'
    '<span class="logo-text" style="font-size:1.1rem;font-weight:800;letter-spacing:-.025em;'
    "background:linear-gradient(135deg,#c084fc,#7c3aed);"
    '-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;">Brain</span>'
    "</div>"
)

_MOBILE_HEADER_CSS = (
    "<style>"
    ".q-page{min-height:calc(100svh - var(--q-header-height,52px))!important;}"
    # Pre-hydration FOUC: before Vue mounts, <q-menu> is an unknown inline tag
    # whose children paint (Edge flashes the user dropdown on navigation).
    # After mount Quasar replaces the tag, so this rule has no effect then.
    "q-menu{display:none!important;}"
    ".mobile-nav-btn{display:none!important;}"
    # Nav icon colour lives in theme.py — icons inherit the label's colour and the
    # active item is the only differentiated one.
    ".nav-btn .q-btn__content{font-size:0.875rem!important;font-weight:500!important;gap:5px!important;}"
    "@media(max-width:767px){"
    ".desktop-nav-btn{display:none!important;}"
    ".mobile-nav-btn{display:inline-flex!important;}"
    ".mobile-hidden{display:none!important;}"
    ".app-header{padding-left:8px!important;padding-right:8px!important;gap:6px!important;}"
    ".doc-card{width:312px!important;}"
    ".doc-cards-row{justify-content:center!important;}"
    "}"
    "</style>"
)

# N_() marks the labels for pybabel extraction; translation happens at render via _(label).
NAV_ITEMS = [
    ("dashboard", N_("Dashboard"), "/"),
    ("chat", N_("Chat"), "/chat"),
    ("auto_awesome", N_("Deep research"), "/werkbank"),
    ("grid_view", N_("Browser"), "/browser"),
    ("psychology", N_("Memory"), "/brain"),
]


def user_icon(username: str) -> str:
    """Icon for a user. Shape, not colour, carries the distinction.

    Users used to get a deterministic hue from their name. That is identity, not
    state, and it put a third meaning on the accent — the same purple as the
    active nav item and the user's own message bubble. An accent that appears in
    three unrelated roles has stopped being an accent. The admin still reads as
    admin because the glyph differs, which also survives a greyscale screenshot.
    """
    return "admin_panel_settings" if username == "Superuser" else "person"


def _current_path() -> str:
    """Path of the page being built, for the active nav marker.

    ui.context is only populated inside a page handler; outside one (tests, a
    disconnected client) no item is marked active, which is the safe default.
    """
    try:
        return ui.context.client.request.url.path
    except Exception:
        return ""


def _is_active(nav_path: str, current: str) -> bool:
    if nav_path == "/":
        return current == "/"
    return current == nav_path or current.startswith(nav_path + "/")


def require_auth() -> bool:
    """Returns True if authenticated. Call at the top of protected pages."""
    if not get_session_token():
        ui.navigate.to("/login")
        return False
    return True


def page_layout() -> None:
    """Call at the top of every page to inject the shared header + drawer."""
    apply_theme()
    ui.add_head_html(
        '<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=yes">'
        '<meta name="theme-color" content="#6d28d9">'
        # SVG first for browsers that support it; the PNGs are the fallback
        # Chrome (especially on Android) actually uses.
        '<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">'
        '<link rel="icon" type="image/png" sizes="192x192" href="/static/icon-192.png">'
        '<link rel="icon" type="image/png" sizes="512x512" href="/static/icon-512.png">'
        '<link rel="apple-touch-icon" href="/static/icon-192.png">'
        '<link rel="manifest" href="/manifest.json">'
        # Register the service worker so Chrome/Android offers the WebAPK
        # "Install app" flow instead of a plain home-screen shortcut.
        "<script>if('serviceWorker' in navigator){window.addEventListener('load',"
        "function(){navigator.serviceWorker.register('/sw.js').catch(function(){});});}</script>"
        "<style>html,body{background:var(--c-bg);}</style>"
        "<script>(function(){var m=document.querySelector('meta[name=\"viewport\"]');"
        "var c='width=device-width,initial-scale=1.0,user-scalable=yes';"
        "if(m){m.content=c;}else{m=document.createElement('meta');m.name='viewport';"
        "m.content=c;document.head.appendChild(m);}})();</script>"
    )
    ui.add_head_html(_MOBILE_HEADER_CSS)

    _ = get_translator()

    username: str = ng_app.storage.user.get("paperless_user", "")
    icon = user_icon(username) if username else "person"
    color = "text-gray-300"

    with ui.header().classes(
        "bg-gray-900 border-b border-gray-700 px-4 py-2 items-center gap-4 app-header"
    ):
        ui.html(_LOGO_HTML, sanitize=False)

        ui.space()

        _here = _current_path()

        # Desktop nav — hidden on mobile via CSS.
        # color=None matters: ui.button defaults to color='primary', which Quasar
        # applies to a flat button's *text and icon*. That made every resting nav
        # glyph carry the accent, so purple meant "nav icon" as well as "active
        # item". Without the prop the glyphs inherit the neutral ramp and only
        # .nav-active carries the accent.
        for nav_icon, label, path in NAV_ITEMS:
            ui.button(
                _(label),
                icon=nav_icon,
                color=None,
                on_click=lambda p=path: ui.navigate.to(p),
            ).props("flat dark dense").classes(
                "nav-btn desktop-nav-btn"
                + (" nav-active" if _is_active(path, _here) else "")
            )

        # Mobile hamburger — hidden on desktop, shown on mobile
        with ui.element("div").classes("mobile-nav-btn"):
            _menu_btn = ui.button(icon="menu", color=None).props("flat dark dense").classes("text-gray-300")
            with ui.menu().props("dark").style(
                "background:var(--c-surface);border:1px solid var(--c-border);border-radius:10px;min-width:180px;"
            ) as _nav_menu:
                for _ni, _nl, _np in NAV_ITEMS:
                    with ui.element("q-item").props("clickable v-ripple dense").classes(
                        "text-gray-200 rounded-lg nav-menu-item"
                        + (" nav-active" if _is_active(_np, _here) else "")
                    ).style("padding:8px 12px;").on("click", lambda p=_np: ui.navigate.to(p)):
                        with ui.element("q-item-section").props("avatar").style("min-width:28px;padding-right:8px;"):
                            ui.icon(_ni, size="xs")
                        with ui.element("q-item-section"):
                            ui.label(_(_nl)).style(
                                "font-size:0.875rem;font-weight:500;letter-spacing:normal;"
                            )
            _menu_btn.on_click(_nav_menu.open)

        # ── Logged-in user dropdown (Settings + Logout) ──────────────────────
        if username:
            ui.separator().props("vertical dark").classes("mx-2 h-6 self-center")
            _user_btn = (
                ui.button(icon=icon, color=None)
                .props("flat dark dense")
                .classes(f"{color} nav-btn")
                .style("gap:4px;padding:4px 8px;")
            )
            with _user_btn:
                ui.label(username).classes(f"text-sm {color} mobile-hidden").style(
                    "font-weight:500;"
                )
                ui.icon("expand_more", size="xs").classes("text-gray-400")
                # Menu must be nested in the button: QMenu anchors to its parent
                # element, and the previous sibling placement anchored it to the
                # full-width header (flush with the viewport edge).
                with ui.menu().props("dark anchor='bottom right' self='top right'").style(
                    "background:var(--c-surface);border:1px solid var(--c-border);"
                    "border-radius:10px;min-width:180px;"
                ):
                    # Username header (read-only, always visible — useful on mobile where button shows only icon)
                    with ui.element("q-item").props("dense").style("padding:8px 12px 4px;"):
                        with ui.element("q-item-section").props("avatar").style(
                            "min-width:28px;padding-right:8px;"
                        ):
                            ui.icon(icon, size="xs").classes(color)
                        with ui.element("q-item-section"):
                            ui.label(username).classes(f"text-sm font-semibold {color}")
                    ui.separator().props("dark").classes("my-1")
                    with ui.element("q-item").props("clickable v-ripple dense").classes(
                        "text-gray-200 rounded-lg"
                    ).style("padding:8px 12px;").on("click", lambda: ui.navigate.to("/settings")):
                        with ui.element("q-item-section").props("avatar").style(
                            "min-width:28px;padding-right:8px;"
                        ):
                            ui.icon("settings", size="xs").classes("text-gray-400")
                        with ui.element("q-item-section"):
                            ui.label(_("Settings")).style("font-size:0.875rem;font-weight:500;")
                    with ui.element("q-item").props("clickable v-ripple dense").classes(
                        "text-gray-200 rounded-lg"
                    ).style("padding:8px 12px;").on("click", _logout):
                        with ui.element("q-item-section").props("avatar").style(
                            "min-width:28px;padding-right:8px;"
                        ):
                            ui.icon("logout", size="xs").classes("text-gray-400")
                        with ui.element("q-item-section"):
                            ui.label(_("Sign out")).style("font-size:0.875rem;font-weight:500;")
                    ui.separator().props("dark").classes("my-1")
                    with ui.element("q-item").props("dense").style("padding:2px 12px 6px;"):
                        with ui.element("q-item-section"):
                            ui.label(f"v{__version__}").classes("text-xs text-gray-500")


def _logout() -> None:
    clear_session()
    ui.navigate.to("/login")
