"""Per-user preferences — the same on every device the user signs in from.

These used to live in ``app.storage.user``, which NiceGUI keys by a browser
cookie, not by the Paperless login. So every browser had its own copy: a switch
set on the desktop was off on the phone, a browser that stayed away for the
cookie's 14 days started over empty, and the one setting that changes data —
writing AI text back into Paperless — depended on which device started the sync.

They now live in ``app.storage.general`` (NiceGUI's own server-wide store, the
same ``.nicegui`` volume — no new persistence layer) under the Paperless
username. ``app.storage.user`` keeps what genuinely belongs to one browser: the
login and the conversation open in that tab.

Read and written inside a page or request context — the username comes from
the session. Signed out (the login page) there is no user to key by, so the
browser storage is used as before.
"""

from __future__ import annotations

from typing import Any

from nicegui import app

_ROOT = "user_prefs"

# Every key that moved here. A value still sitting in a browser's storage under
# one of these names is adopted the first time that user reads it, so nobody
# loses a setting in the switch; after that the browser copy is dropped, so
# there is only ever one owner.
KEYS = frozenset({
    "language",
    "theme",
    "push_text_to_paperless",
    "voice_memos_enabled",
    "chat_mic_engine",
    "dictation_language",
    "chat_model_name",
    "pinned_doc_ids",
    "pinned_docs_cache",
    "hidden_actions",
})


def _username() -> str:
    try:
        return str(app.storage.user.get("paperless_user") or "")
    except Exception:   # no request context — nothing to key by
        return ""


def _prefs(username: str) -> dict:
    """The user's preference dict, created on first write."""
    root = app.storage.general.setdefault(_ROOT, {})
    return root.setdefault(username, {})


def _stored(username: str) -> dict:
    """The user's preferences for reading — never creates anything, so a
    page view does not write the server store."""
    return (app.storage.general.get(_ROOT) or {}).get(username) or {}


def get_pref(key: str, default: Any = None) -> Any:
    """The signed-in user's value for `key`, else `default`."""
    username = _username()
    if not username:
        return app.storage.user.get(key, default)
    prefs = _stored(username)
    browser = app.storage.user
    if key in prefs:
        if key in browser:
            browser.pop(key, None)       # a stale per-browser copy — drop it
        return prefs[key]
    if key in browser:
        # First read since preferences became per user: keep what this browser
        # had. Another browser's older value then no longer wins anything.
        value = browser.pop(key)
        _prefs(username)[key] = value
        return value
    return default


def set_pref(key: str, value: Any) -> None:
    """Store `key` for the signed-in user (for this browser when signed out)."""
    username = _username()
    if not username:
        app.storage.user[key] = value
        return
    _prefs(username)[key] = value
    app.storage.user.pop(key, None)
