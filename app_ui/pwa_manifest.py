"""The web app manifest, with the app shortcuts.

One installed app, several entry points: Android shows `shortcuts` when the
app icon is long-pressed, and each can be dragged onto the home screen as an
icon of its own that opens the app at that page. That is what gives "an icon
for the chat, one for notes" — never a second manifest on the same origin,
which would share the scope and the service worker with this one (a push
notification could then open in the wrong "app").

`id` is pinned to "/" (static manifest.json): Chrome derives an installed
app's identity from `id`, else from `start_url`. The app was installed while
`start_url` was "/"; moving it to "/start" without an `id` made the manifest
a different app, so the installed one never took the update that carries the
shortcuts. Never change `id`.

Chrome fetches the manifest without the user's session, so nothing here may
depend on who is signed in. The names follow the browser's language
(`Accept-Language`), the list the server's configuration: no memo shortcut
without a transcription service.
"""

from __future__ import annotations

import base64
import json
from functools import lru_cache
from pathlib import Path

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


_STATIC = Path(__file__).parent / "static"


@lru_cache(maxsize=16)
def _data_uri(name: str) -> str | None:
    try:
        raw = (_STATIC / name).read_bytes()
    except OSError:
        return None
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def _icons(glyph: str) -> list[dict]:
    """The 192 px icons travel inside the manifest as data URIs; the 96 px ones
    stay addresses, as a fallback.

    The main icon showed on the installed app while every shortcut tile stayed
    blank (2026-11-27): Chrome hands the main icon's bytes to the WebAPK
    builder, while shortcut icons are fetched by address — which cannot work
    for a server only the phone's network reaches. A data URI needs no fetch.
    A browser that rejects one still has the address icons."""
    v = ICON_VERSION
    out = []
    for purpose, part in (("any", ""), ("maskable", "maskable-")):
        uri = _data_uri(f"shortcut-{glyph}-{v}-{part}192.png")
        if uri:
            out.append({"src": uri, "sizes": "192x192", "type": "image/png", "purpose": purpose})
    for purpose, part in (("any", ""), ("maskable", "maskable-")):
        out.append({"src": f"/static/shortcut-{glyph}-{v}-{part}96.png", "sizes": "96x96",
                    "type": "image/png", "purpose": purpose})
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
