"""User preferences follow the Paperless login, not the browser.

They used to sit in NiceGUI's per-browser storage, so a switch set on the
desktop was off on the phone and a browser idle for the cookie's lifetime lost
everything — including the switch that decides whether a sync writes AI text
back into Paperless.
"""

from types import SimpleNamespace

import pytest

from services import user_prefs


class _Browser:
    """One browser: its own `app.storage.user`, sharing the server's general store."""

    def __init__(self, general: dict, user: str = "", **stored):
        self.storage = SimpleNamespace(
            user={"paperless_user": user, **stored} if user else dict(stored),
            general=general,
        )


@pytest.fixture
def server():
    return {}   # app.storage.general — one per server, shared by all browsers


def _use(monkeypatch, browser):
    monkeypatch.setattr(user_prefs, "app", browser)


def test_a_setting_made_on_one_device_holds_on_the_other(monkeypatch, server):
    desktop, phone = _Browser(server, "alice"), _Browser(server, "alice")
    _use(monkeypatch, desktop)
    user_prefs.set_pref("push_text_to_paperless", True)
    _use(monkeypatch, phone)
    assert user_prefs.get_pref("push_text_to_paperless", False) is True


def test_users_do_not_share_preferences(monkeypatch, server):
    _use(monkeypatch, _Browser(server, "alice"))
    user_prefs.set_pref("language", "de")
    _use(monkeypatch, _Browser(server, "bob"))
    assert user_prefs.get_pref("language", "en") == "en"


def test_the_old_browser_value_is_adopted_once_and_then_dropped(monkeypatch, server):
    desktop = _Browser(server, "alice", theme="light")
    _use(monkeypatch, desktop)
    assert user_prefs.get_pref("theme", "dark") == "light"
    assert "theme" not in desktop.storage.user          # one owner from now on
    # A second browser with an older, different value no longer wins.
    phone = _Browser(server, "alice", theme="dark")
    _use(monkeypatch, phone)
    assert user_prefs.get_pref("theme", "dark") == "light"
    assert "theme" not in phone.storage.user


def test_an_empty_value_is_a_value(monkeypatch, server):
    """Unhiding every deadline stores [] — that must not read as "never set"."""
    _use(monkeypatch, _Browser(server, "alice"))
    user_prefs.set_pref("hidden_actions", [])
    assert user_prefs.get_pref("hidden_actions") == []


def test_signed_out_falls_back_to_the_browser(monkeypatch, server):
    login_page = _Browser(server)
    _use(monkeypatch, login_page)
    user_prefs.set_pref("language", "de")
    assert login_page.storage.user["language"] == "de"
    assert user_prefs.get_pref("language", "en") == "de"
    assert server == {}
