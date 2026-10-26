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
            return

        ui.separator().classes("my-4")
        _channels_section(username, body.refresh)
        ui.separator().classes("my-4")
        _reminders_section(username)
        ui.separator().classes("my-4")
        _model_section(username, token, p)

    body()


def _model_section(username: str, token: str, p) -> None:
    """Proactive model: the operator may fix it, restrict it, or forbid cloud."""
    _ = get_translator()
    from config.proactive import proactive_settings
    from proactive import prefs as pprefs
    from proactive.models import allowed_registry_models, resolve_model

    cfg = proactive_settings()
    ui.label(_("Model")).classes("text-sm font-semibold text-gray-200")
    if cfg.proactive_model_fixed:
        ui.input(_("Model"), value=cfg.proactive_model_fixed).props(
            "outlined dark dense readonly"
        ).classes("w-full")
        ui.label(_("Set by the operator for all users.")).classes("text-xs text-gray-500")
        return

    allowed = allowed_registry_models(username, token)
    fallback = cfg.proactive_model_default or p.chat_model or _("none")
    from app_ui.model_picker import model_select

    def _show_cloud_warning() -> None:
        cur = pprefs.get(username)
        resolved = resolve_model(username, token, cur)
        if resolved is not None and resolved.lane == "cloud":
            warn.set_text(
                _(
                    "This is a cloud model: while your existing documents are analysed, "
                    "their text is sent to that provider (at most {n} documents a day)."
                ).format(n=cfg.proactive_cloud_max_docs_per_user_day)
            )
            warn.set_visibility(True)
        else:
            warn.set_visibility(False)

    def _on_change(e) -> None:
        name = e.value or ""
        if name and name not in {m["name"] for m in allowed_registry_models(username, token)}:
            ui.notify(_("This model is not allowed for proactive jobs."), type="negative")
            return
        cur = pprefs.get(username)
        cur.model = name
        _snapshot_from_session(cur)
        pprefs.save(cur)
        _show_cloud_warning()
        ui.notify(_("Saved"), type="positive")

    model_select(
        allowed,
        p.model if p.model in {m["name"] for m in allowed} else "",
        label=_("Model for reminders and insights"),
        on_change=_on_change,
        allow_empty=True,
        empty_label=_("Default ({model})").format(model=fallback),
    ).props("outlined dark dense").classes("w-full")
    if p.model and p.model not in {m["name"] for m in allowed}:
        ui.label(
            _("Your earlier choice {model} is not allowed any more; the default is used.").format(
                model=p.model
            )
        ).classes("text-xs text-gray-500")
    warn = ui.label("").classes("text-xs pb-warn-text leading-relaxed")
    _show_cloud_warning()


def _channels_section(username: str, refresh) -> None:
    """Where notifications go: link Telegram / email, choose, opt in to full text."""
    _ = get_translator()
    from config.proactive import proactive_settings
    from proactive import jobs, linking
    from proactive import prefs as pprefs
    from proactive.db import kv_get

    cfg = proactive_settings()
    links = linking.links(username)
    ui.label(_("Notification channels")).classes("text-sm font-semibold text-gray-200")
    ui.label(
        _(
            "By default a message contains only a title, the urgency and a link into "
            "the app. Full text only where you switch it on — Telegram chats with a bot "
            "are not end-to-end encrypted, and email leaves the server too."
        )
    ).classes("text-xs text-gray-400 leading-relaxed mb-2")

    def _full_text_switch(channel: str) -> None:
        def _on(e, ch=channel) -> None:
            linking.set_full_text(username, ch, bool(e.value))

        ui.switch(
            _("Include the full text"), value=links[channel]["full_text"], on_change=_on
        ).classes("text-xs text-gray-300")

    # ── Telegram ─────────────────────────────────────────────────────────────
    with ui.column().classes("w-full gap-1 mb-3"):
        ui.label("Telegram").classes("text-xs font-semibold text-gray-300")
        if not cfg.telegram_available:
            ui.label(_("Not set up on this server.")).classes("text-xs text-gray-500")
        elif "telegram" in links:
            with ui.row().classes("items-center gap-2"):
                ui.label(_("Linked")).classes("text-xs text-gray-300")

                def _unlink_tg() -> None:
                    linking.unlink(username, "telegram")
                    refresh()

                ui.button(_("Unlink"), on_click=_unlink_tg).props("flat dense size=sm")
            _full_text_switch("telegram")
        else:
            code_box = ui.column().classes("gap-0")

            def _new_tg_code() -> None:
                code = linking.create_code(username, "telegram")
                bot = kv_get("telegram_bot_username")
                code_box.clear()
                with code_box:
                    ui.label(
                        _("Send this to {bot} in Telegram:").format(
                            bot=f"@{bot}" if bot else _("your PaperlessBrain bot")
                        )
                    ).classes("text-xs text-gray-300")
                    ui.label(f"/start {code}").classes("text-sm font-mono text-gray-100 break-all")
                    ui.label(_("Valid for 10 minutes, once.")).classes("text-xs text-gray-500")

            ui.button(_("Link Telegram"), icon="link", on_click=_new_tg_code).props(
                "flat dense size=sm"
            )

    # ── Email ────────────────────────────────────────────────────────────────
    with ui.column().classes("w-full gap-1 mb-3"):
        ui.label(_("Email")).classes("text-xs font-semibold text-gray-300")
        if not cfg.email_available:
            ui.label(_("Not set up on this server.")).classes("text-xs text-gray-500")
        elif "email" in links:
            with ui.row().classes("items-center gap-2 min-w-0 w-full"):
                ui.label(links["email"]["address"]).classes("text-xs text-gray-300 break-all")

                def _unlink_mail() -> None:
                    linking.unlink(username, "email")
                    refresh()

                ui.button(_("Unlink"), on_click=_unlink_mail).props("flat dense size=sm")
            _full_text_switch("email")
        else:
            addr = ui.input(_("Your email address"), value=linking.pending_address(username)).props(
                "outlined dark dense"
            ).classes("w-full")

            def _send_code() -> None:
                a = (addr.value or "").strip()
                if "@" not in a or " " in a or len(a) > 254:
                    ui.notify(_("Please enter a valid email address."), type="warning")
                    return
                code = linking.create_code(username, "email", a)
                jobs.enqueue("email_verify", username, {"address": a, "code": code})
                ui.notify(_("A code is on its way to {address}.").format(address=a), type="positive")

            ui.button(_("Send code"), on_click=_send_code).props("flat dense size=sm")
            code_in = ui.input(_("Code from the email")).props("outlined dark dense").classes("w-full")

            def _confirm() -> None:
                hit = linking.redeem(code_in.value or "", "email", user=username)
                if hit is None:
                    ui.notify(_("This code is invalid or has expired."), type="negative")
                    return
                linking.link(username, "email", hit[1])
                ui.notify(_("Email address confirmed."), type="positive")
                refresh()

            ui.button(_("Confirm"), on_click=_confirm).props("flat dense size=sm")

    # ── Choice ───────────────────────────────────────────────────────────────
    usable = [c for c in ("telegram", "email") if c in links and (
        cfg.telegram_available if c == "telegram" else cfg.email_available)]
    options = {"": _("No messages")}
    if "telegram" in usable:
        options["telegram"] = "Telegram"
    if "email" in usable:
        options["email"] = _("Email")
    if len(usable) == 2:
        options["both"] = _("Telegram and email")
    p = pprefs.get(username)

    def _on_choice(e) -> None:
        cur = pprefs.get(username)
        cur.channels = e.value or ""
        pprefs.save(cur)

    ui.select(
        options, value=p.channels if p.channels in options else "",
        label=_("Send messages via"), on_change=_on_choice,
    ).props("outlined dark dense").classes("w-full")

    def _test() -> None:
        jobs.enqueue("send_test", username)
        ui.notify(_("Test message queued."), type="positive")

    ui.button(_("Send test message"), icon="send", on_click=_test).props(
        "flat dense size=sm"
    ).classes("mt-1")


def _reminders_section(username: str) -> None:
    _ = get_translator()
    from config.proactive import parse_offsets, proactive_settings
    from proactive import prefs as pprefs

    cfg = proactive_settings()
    p = pprefs.get(username)
    ui.label(_("Deadline reminders")).classes("text-sm font-semibold text-gray-200")
    default = ", ".join(str(n) for n in cfg.deadline_offsets)
    inp = ui.input(
        _("Days before a deadline"),
        value=", ".join(str(n) for n in p.deadline_offsets),
        placeholder=default,
    ).props("outlined dark dense").classes("w-full")
    ui.label(_("Empty = {default}.").format(default=default)).classes("text-xs text-gray-500")

    def _save() -> None:
        raw = (inp.value or "").strip()
        try:
            offsets = parse_offsets(raw) if raw else []
        except ValueError:
            ui.notify(_("Enter whole numbers of days, separated by commas."), type="warning")
            return
        cur = pprefs.get(username)
        cur.deadline_offsets = offsets
        pprefs.save(cur)
        inp.set_value(", ".join(str(n) for n in offsets))
        ui.notify(_("Saved"), type="positive")

    ui.button(_("Save"), on_click=_save).props("flat dense size=sm")

    # ── Weekly message limit (architecture §8) ───────────────────────────────
    ceiling = cfg.proactive_max_pushes_per_week_limit
    current = p.max_pushes_per_week if p.max_pushes_per_week is not None else cfg.proactive_max_pushes_per_week
    ui.label(_("Max. messages per week")).classes("text-sm font-semibold text-gray-200 mt-4")
    limit_in = ui.number(
        _("0 to {max}").format(max=ceiling), value=current, min=0, max=ceiling, step=1, format="%d",
    ).props("outlined dark dense").classes("w-full")
    ui.label(
        _(
            "Insights beyond this are held back and shown in the app, not dropped. "
            "Critical deadline reminders always arrive — also with 0."
        )
    ).classes("text-xs text-gray-500")

    def _save_limit() -> None:
        try:
            n = int(limit_in.value)
        except (TypeError, ValueError):
            n = -1
        if not 0 <= n <= ceiling:
            ui.notify(_("Choose a number from 0 to {max}.").format(max=ceiling), type="warning")
            return
        cur = pprefs.get(username)
        cur.max_pushes_per_week = n
        pprefs.save(cur)
        ui.notify(_("Saved"), type="positive")

    ui.button(_("Save"), on_click=_save_limit).props("flat dense size=sm")
