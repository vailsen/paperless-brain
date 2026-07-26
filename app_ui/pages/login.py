# app_ui/pages/login.py
import asyncio

from nicegui import app as ng_app
from nicegui import ui

from app_ui.theme import apply_theme
from config.settings import settings
from i18n import get_translator
from services.paperless import get_token
from services.session_auth import get_session_token, set_session_token


_LOGIN_LOGO_HTML = (
    '<div style="display:flex;align-items:center;gap:10px;justify-content:center;user-select:none;">'
    '<img src="/static/paperlessbrain_logo.png" style="height:40px;width:40px;object-fit:contain;">'
    '<span style="font-size:1.4rem;font-weight:800;letter-spacing:-.03em;color:var(--c-text);">Paperless</span>'
    '<span style="font-size:1.4rem;font-weight:800;letter-spacing:-.03em;'
    'background:linear-gradient(135deg,#c084fc,#7c3aed);'
    '-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;">Brain</span>'
    '</div>'
)


@ui.page("/login")
async def login_page() -> None:
    _ = get_translator()
    apply_theme()
    ui.add_head_html(
        '<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=yes">'
        '<meta name="theme-color" content="#6d28d9">'
        '<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">'
        '<link rel="icon" type="image/png" sizes="192x192" href="/static/icon-192.png">'
        '<link rel="icon" type="image/png" sizes="512x512" href="/static/icon-512.png">'
        '<link rel="apple-touch-icon" href="/static/icon-192.png">'
        # Logged-out users land here first — Chrome evaluates installability on
        # the current page, so the manifest + SW must be present on login too.
        '<link rel="manifest" href="/manifest.json">'
        "<script>if('serviceWorker' in navigator){window.addEventListener('load',"
        "function(){navigator.serviceWorker.register('/sw.js').catch(function(){});});}</script>"
        '<style>html,body{background:var(--c-bg-deep);}</style>'
        '<script>(function(){var m=document.querySelector(\'meta[name="viewport"]\');'
        'var c=\'width=device-width,initial-scale=1.0,user-scalable=yes\';'
        'if(m){m.content=c;}else{m=document.createElement(\'meta\');m.name=\'viewport\';'
        'm.content=c;document.head.appendChild(m);}})();</script>'
    )

    # Already logged in → forward to browser
    if get_session_token():
        ui.navigate.to("/")
        return

    with ui.column().classes(
        "absolute inset-0 items-center justify-center bg-gray-950"
    ):
        with (
            ui.card()
            .classes("bg-gray-900 border border-gray-700 rounded-2xl p-8 w-80 gap-4")
            .style("box-shadow:0 8px 40px rgba(0,0,0,.6)")
        ):
            ui.html(_LOGIN_LOGO_HTML, sanitize=False).classes("w-full mb-1")

            ui.label(_("Paperless login")).classes(
                "text-gray-400 text-sm text-center w-full"
            )
            ui.separator().classes("my-1")

            username_input = (
                ui.input(_("Username")).props("dark outlined dense").classes("w-full")
            )
            password_input = (
                ui.input(_("Password"), password=True, password_toggle_button=True)
                .props("dark outlined dense")
                .classes("w-full")
            )
            error_label = ui.label("").classes("text-red-400 text-xs hidden")
            login_btn = (
                ui.button(_("Sign in"), icon="login")
                .props("dark")
                .classes("w-full bg-purple-700 hover:bg-purple-600 text-white mt-1")
            )

            async def do_login() -> None:
                username = username_input.value.strip()
                password = password_input.value
                if not username or not password:
                    error_label.set_text(_("Please enter username and password."))
                    error_label.classes(remove="hidden")
                    return

                login_btn.props(add="loading")
                error_label.classes(add="hidden")

                token = await asyncio.to_thread(
                    get_token, settings.paperless_url, username, password
                )

                login_btn.props(remove="loading")

                if token:
                    set_session_token(token)
                    ng_app.storage.user["paperless_user"] = username
                    ui.navigate.to("/")
                else:
                    error_label.set_text(_("Login failed. Please check your details."))
                    error_label.classes(remove="hidden")
                    password_input.set_value("")

            login_btn.on_click(do_login)
            password_input.on("keydown.enter", do_login)
            username_input.on(
                "keydown.enter", lambda: password_input.run_method("focus")
            )
