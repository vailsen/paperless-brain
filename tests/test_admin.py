"""ADMIN_USERS is the only way to be admin."""

from services import admin


def test_admins_come_from_env_only(monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "admin_users", " Alice, bob ,")
    assert admin.is_admin("alice") and admin.is_admin("Bob")
    assert not admin.is_admin("carol")
    assert not admin.is_admin("")
    monkeypatch.setattr(settings, "admin_users", "")
    assert not admin.is_admin("alice")
    assert not admin.current_is_admin()  # outside a page
