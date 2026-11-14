"""Who is admin: the Paperless usernames in `ADMIN_USERS` (.env), nothing else.

Never settable in the UI — a setting an admin page could change would let a
user make themselves admin. Matching ignores case: Paperless usernames are
case-insensitive at login.
"""

from __future__ import annotations


def admin_names() -> set[str]:
    from config.settings import settings

    return {n.strip().casefold() for n in (settings.admin_users or "").split(",") if n.strip()}


def is_admin(username: str | None) -> bool:
    return bool(username) and username.casefold() in admin_names()


def current_is_admin() -> bool:
    """Is the signed-in user of this page an admin? False outside a page."""
    try:
        from nicegui import app

        return is_admin(app.storage.user.get("paperless_user", ""))
    except Exception:  # noqa: BLE001
        return False
