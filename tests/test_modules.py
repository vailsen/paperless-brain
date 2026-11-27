"""The module registry (`app_ui/modules.py`): hiding, the start page, the
old "Use experts" switch."""

import pytest

from app_ui import modules


@pytest.fixture
def prefs(monkeypatch):
    store: dict = {}
    import services.user_prefs as up

    monkeypatch.setattr(up, "get_pref", lambda k, d=None: store.get(k, d))
    monkeypatch.setattr(up, "set_pref", lambda k, v: store.__setitem__(k, v))
    return store


def test_module_for_path():
    assert modules.module_for_path("/").key == "dashboard"
    assert modules.module_for_path("/findings/abc").key == "findings"
    assert modules.module_for_path("/brain").key == "vault"
    assert modules.module_for_path("/werkbank").key == "research"
    assert modules.module_for_path("/settings") is None
    assert modules.module_for_path("/chatty") is None


def test_chat_and_dashboard_cannot_be_hidden(prefs):
    modules.set_hidden({"chat", "dashboard", "browser", "nonsense"})
    assert modules.hidden() == {"browser"}
    assert prefs["hidden_modules"] == ["browser"]


def test_old_experts_switch_means_experts_hidden(prefs):
    prefs["experts_enabled"] = False
    assert modules.hidden() == {"experts"}
    from services import experts

    assert experts.enabled() is False
    modules.set_hidden(set())
    assert experts.enabled() is True and prefs["experts_enabled"] is True


def test_visible_modules_drop_hidden_ones(prefs, monkeypatch):
    import config.proactive

    monkeypatch.setattr(config.proactive, "is_enabled", lambda: True)
    modules.set_hidden({"vault", "experts"})
    keys = [m.key for m in modules.visible_modules()]
    assert keys == ["dashboard", "chat", "findings", "research", "browser"]
    monkeypatch.setattr(config.proactive, "is_enabled", lambda: False)
    assert "findings" not in [m.key for m in modules.visible_modules()]


def test_start_target(prefs):
    assert modules.start_target("chat", "") == "/chat"
    assert modules.start_target(modules.LAST, "vault") == "/brain"
    assert modules.start_target(modules.LAST, "") == "/"
    modules.set_hidden({"vault"})
    assert modules.start_target("vault", "") == "/"  # hidden → the dashboard
    assert modules.start_target("bogus", "") == "/"
