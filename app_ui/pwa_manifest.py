"""The web app manifest, with the app shortcuts.

One installed app, several entry points: Android shows `shortcuts` when the
app icon is long-pressed, and each can be dragged onto the home screen as an
icon of its own that opens the app at that page. That is what gives "an icon
for the chat, one for notes" — never a second manifest on the same origin,
which would share the scope and the service worker with this one (a push
notification could then open in the wrong "app").

Chrome fetches the manifest without the user's session, so nothing here may
depend on who is signed in. The names follow the browser's language
(`Accept-Language`), the list the server's configuration: no memo shortcut
without a transcription service.
"""

from __future__ import annotations

import json
from functools import lru_cache

from app_ui.brand import ICON_VERSION
from i18n import N_, SUPPORTED_LANGUAGES, translator_for

# (key, name, short name, url, glyph). Dashboard is the app icon itself.
SHORTCUTS = (
    ("chat", N_("Chat"), N_("Chat"), "/chat", "chat"),
    ("vault", N_("Note vault"), N_("Notes"), "/brain", "note"),
    ("memo", N_("New voice memo"), N_("Memo"), "/?memo=1", "memo"),
)


def pick_language(accept_language: str | None) -> str:
    """First supported language in an Accept-Language header, else English."""
    for part in (accept_language or "").split(","):
        code = part.split(";")[0].strip().lower().split("-")[0]
        if code in SUPPORTED_LANGUAGES:
            return code
    return "en"


def _icons(glyph: str) -> list[dict]:
    v = ICON_VERSION
    out = []
    for size in (96, 192):
        out.append({"src": f"/static/shortcut-{glyph}-{v}-{size}.png", "sizes": f"{size}x{size}",
                    "type": "image/png", "purpose": "any"})
        out.append({"src": f"/static/shortcut-{glyph}-{v}-maskable-{size}.png", "sizes": f"{size}x{size}",
                    "type": "image/png", "purpose": "maskable"})
    return out


def shortcuts(lang: str, memo: bool) -> list[dict]:
    _ = translator_for(lang)
    return [
        {"name": _(name), "short_name": _(short), "url": url, "icons": _icons(glyph)}
        for key, name, short, url, glyph in SHORTCUTS
        if key != "memo" or memo
    ]


@lru_cache(maxsize=4)
def _base(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def build_manifest(path: str, accept_language: str | None = None) -> dict:
    from app_ui.memo_dialog import memo_configured

    manifest = json.loads(_base(path))
    lang = pick_language(accept_language)
    manifest["lang"] = lang
    manifest["shortcuts"] = shortcuts(lang, memo_configured())
    return manifest
