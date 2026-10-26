"""Settings > Proactive — only rendered when `PROACTIVE_ENABLED=true`.

Background access comes first because everything else depends on it: the
worker acts as the user with their own Paperless token, and it has one only if
the user granted it here (`services/background_access.py`). Opting in to the
proactive system therefore requires an active grant; revoking the grant also
opts the user out.
"""

from __future__ import annotations

from nicegui import ui

from i18n import get_translator
from services import background_access
from services.user_prefs import get_pref


def _snapshot_from_session(p) -> None:
    """Copy what the worker cannot read itself (diagnostics §8.2)."""
    from i18n import DEFAULT_LANG

    p.language = get_pref("language", DEFAULT_LANG) or DEFAULT_LANG
    p.chat_model = get_pref("chat_model_name", "") or ""


def proactive_setting(username: str, token: str) -> None:
    _ = get_translator()
    from proactive import prefs as pprefs

    @ui.refreshable
    def body() -> None:
        status = background_access.status(username)
        p = pprefs.get(username)

        # ── Background access ────────────────────────────────────────────────
        ui.label(_("Background access")).classes("text-sm font-semibold text-gray-200")
        ui.label(
            _(
                "Reminders and insights are prepared while you are not using the app. "
                "To do that, PaperlessBrain keeps your Paperless access token "
                "encrypted on the server and acts as you — it sees exactly the "
                "documents you can see, nothing more. You can withdraw this at any time."
            )
        ).classes("text-xs text-gray-400 leading-relaxed mb-2")

        status_text = {
            "none": _("Not allowed"),
            "active": _("Allowed"),
            "needs_reauth": _("Your token was rejected — sign in again to renew it"),
        }[status]
        with ui.row().classes("items-center gap-2 mb-2"):
            ui.icon(
                "check_circle" if status == "active" else ("error" if status == "needs_reauth" else "block"),
                size="xs",
            ).classes("text-gray-400" if status != "needs_reauth" else "pb-warn-text")
            ui.label(status_text).classes("text-xs text-gray-300")

        def _grant() -> None:
            if not token:
                ui.notify(_("Not signed in."), type="negative")
                return
            background_access.grant(username, token)
            ui.notify(_("Background access allowed."), type="positive")
            body.refresh()

        def _revoke() -> None:
            background_access.revoke(username)
            cur = pprefs.get(username)
            if cur.optin:
                cur.optin = False
                pprefs.save(cur)
            ui.notify(_("Background access withdrawn. Proactive features are off."), type="info")
            body.refresh()

        with ui.row().classes("gap-2 mb-4"):
            if status == "active":
                ui.button(_("Withdraw background access"), icon="block", on_click=_revoke).props(
                    "flat dense"
                )
            else:
                ui.button(_("Allow background access"), icon="key", on_click=_grant).props(
                    "unelevated dense color=primary"
                )
                if status == "needs_reauth":
                    ui.button(_("Withdraw"), on_click=_revoke).props("flat dense")

        # ── Opt-in ───────────────────────────────────────────────────────────
        def _on_optin(e) -> None:
            cur = pprefs.get(username)
            want = bool(e.value)
            if want and background_access.status(username) != "active":
                ui.notify(_("Allow background access first."), type="warning")
                body.refresh()
                return
            cur.optin = want
            _snapshot_from_session(cur)
            pprefs.save(cur)
            body.refresh()

        sw = ui.switch(
            _("Proactive reminders and insights"),
            value=p.optin and status == "active",
            on_change=_on_optin,
        ).classes("text-sm text-gray-300")
        if status != "active":
            sw.disable()
            ui.label(_("Needs background access.")).classes("text-xs text-gray-500")

    body()
