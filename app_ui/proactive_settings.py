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
        _analyse_now_section(username, body.refresh)
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
    """Where notifications go: push devices, email; choose, opt in to full text."""
    _ = get_translator()
    from config.proactive import proactive_settings
    from proactive import jobs, linking
    from proactive import prefs as pprefs

    cfg = proactive_settings()
    links = linking.links(username)
    ui.label(_("Notification channels")).classes("text-sm font-semibold text-gray-200")
    ui.label(
        _(
            "By default a message contains only a title, the urgency and a link into "
            "the app. Full text by email only where you switch it on — email leaves the "
            "server unencrypted."
        )
    ).classes("text-xs text-gray-400 leading-relaxed mb-2")

    def _full_text_switch(channel: str) -> None:
        def _on(e, ch=channel) -> None:
            linking.set_full_text(username, ch, bool(e.value))

        ui.switch(
            _("Include the full text"), value=links[channel]["full_text"], on_change=_on
        ).classes("text-xs text-gray-300")

    _push_block(username, refresh)

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
                wait = linking.email_code_wait(username)
                if wait is not None:
                    ui.notify(_("Too many codes sent. Try again in {n} minutes.").format(
                        n=max(1, -(-int(wait.total_seconds()) // 60))), type="warning")
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
    from proactive.notify import webpush

    linked = set(links)
    if cfg.push_available and webpush.devices(username):
        linked.add("push")
    available = {"push": cfg.push_available, "email": cfg.email_available}
    labels = {"push": _("This app (push)"), "email": _("Email")}
    usable = [c for c in pprefs.CHANNELS if available[c] and c in linked]
    p = pprefs.get(username)
    chosen = set(p.channel_list())

    ui.label(_("Send reminders and insights via")).classes("text-xs font-semibold text-gray-300")
    if not usable:
        ui.label(_("Turn on at least one channel above.")).classes("text-xs text-gray-500")
    for ch in usable:
        def _on_choice(e, ch=ch) -> None:
            cur = pprefs.get(username)
            now = set(cur.channel_list())
            now.add(ch) if e.value else now.discard(ch)
            cur.channels = ",".join(c for c in pprefs.CHANNELS if c in now)
            pprefs.save(cur)

        ui.checkbox(labels[ch], value=ch in chosen, on_change=_on_choice).props("dense").classes(
            "text-xs text-gray-300"
        )

    def _test() -> None:
        jobs.enqueue("send_test", username)
        ui.notify(_("Test message queued."), type="positive")

    ui.button(_("Send test message"), icon="send", on_click=_test).props(
        "flat dense size=sm"
    ).classes("mt-1")


def _push_block(username: str, refresh) -> None:
    """Web Push on this device. Turning it on must run in the click itself:
    iOS (and Chrome's quiet UI) only grant the permission inside a user
    gesture, which a round trip to the server would end — hence a JS handler,
    not `on_click`."""
    import json

    _ = get_translator()
    from config.proactive import proactive_settings
    from proactive import prefs as pprefs
    from proactive.notify import webpush

    cfg = proactive_settings()
    with ui.column().classes("w-full gap-1 mb-3"):
        ui.label(_("This app (push)")).classes("text-xs font-semibold text-gray-300")
        if not cfg.push_available:
            ui.label(_("Not set up on this server.")).classes("text-xs text-gray-500")
            return
        ui.label(
            _(
                "Notifications on this phone or computer, also when the app is closed. "
                "Tap one to open it in the app. Turn it on on every device you want them on. "
                "On an iPhone, add the app to the home screen first."
            )
        ).classes("text-xs text-gray-400 leading-relaxed")
        msgs = {
            "on": _("On for this device."),
            "off": _("Off for this device."),
            "unsupported": _(
                "This browser cannot receive push messages. On an iPhone, open the app "
                "from the home screen."
            ),
            "denied": _("Notifications are blocked for this site in the browser settings."),
            "failed": _("Could not turn on notifications."),
        }
        ui.label("").props("id=pb-push-status").classes("text-xs text-gray-300")
        ui.run_javascript(
            f"window.__pbPushMsgs = {json.dumps(msgs)};"
            f"window.__pbPushKey = {json.dumps(cfg.push_vapid_public_key)};"
            + _PUSH_JS
            + "window.__pbPushState();"
        )
        with ui.row().classes("gap-2"):
            ui.button(_("Turn on for this device"), icon="notifications_active").props(
                "flat dense size=sm"
            ).on("click", js_handler="() => window.__pbPushOn()")
            ui.button(_("Turn off for this device"), icon="notifications_off").props(
                "flat dense size=sm"
            ).on("click", js_handler="() => window.__pbPushOff()")

        devs = webpush.devices(username)
        if devs:
            ui.label(_("Devices")).classes("text-xs text-gray-400 mt-1")
        for d in devs:
            with ui.row().classes("items-center gap-2 w-full min-w-0"):
                ui.label(d["device"] or _("Unknown device")).classes("text-xs text-gray-300")
                ui.label(d["created_at"][:10]).classes("text-xs text-gray-500")
                ui.label(_push_status(d)).classes("text-xs text-gray-500")

                def _remove(ep=d["endpoint"]) -> None:
                    webpush.unsubscribe(username, ep)
                    refresh()

                ui.button(icon="close", on_click=_remove).props("flat dense round size=xs").tooltip(
                    _("Remove")
                )

        p = pprefs.get(username)

        def _toggle(field: str):
            def _on(e) -> None:
                cur = pprefs.get(username)
                setattr(cur, field, bool(e.value))
                pprefs.save(cur)
            return _on

        ui.label(_("Also notify me here (push only)")).classes("text-xs text-gray-400 mt-1")
        ui.switch(_("When a sync finishes"), value=p.notify_sync,
                  on_change=_toggle("notify_sync")).props("dense").classes("text-xs text-gray-300")
        ui.switch(_("When a Werkbank run is done"), value=p.notify_werkbank,
                  on_change=_toggle("notify_werkbank")).props("dense").classes("text-xs text-gray-300")


# Defined once per page load; reads __pbPushMsgs / __pbPushKey set just before.
_PUSH_JS = r"""
window.__pbPushSay = (k) => {
  const el = document.getElementById('pb-push-status');
  if (el) el.textContent = (window.__pbPushMsgs || {})[k] || '';
};
window.__pbPushSupported = () =>
  'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
window.__pbPushDevice = () => {
  const ua = navigator.userAgent;
  const os = /iPhone/.test(ua) ? 'iPhone' : /iPad/.test(ua) ? 'iPad' : /Android/.test(ua) ? 'Android'
    : /Windows/.test(ua) ? 'Windows' : /Mac OS X/.test(ua) ? 'Mac' : /Linux/.test(ua) ? 'Linux' : '';
  const br = /Edg\//.test(ua) ? 'Edge' : /Firefox\//.test(ua) ? 'Firefox'
    : /Chrome\//.test(ua) ? 'Chrome' : /Safari\//.test(ua) ? 'Safari' : '';
  return [os, br].filter(Boolean).join(' · ');
};
window.__pbPushState = async () => {
  if (!window.__pbPushSupported()) return window.__pbPushSay('unsupported');
  if (Notification.permission === 'denied') return window.__pbPushSay('denied');
  const reg = await navigator.serviceWorker.getRegistration('/');
  const sub = reg && await reg.pushManager.getSubscription();
  window.__pbPushSay(sub && Notification.permission === 'granted' ? 'on' : 'off');
};
window.__pbPushOn = async () => {
  try {
    if (!window.__pbPushSupported()) return window.__pbPushSay('unsupported');
    const perm = await Notification.requestPermission();
    if (perm !== 'granted') return window.__pbPushSay(perm === 'denied' ? 'denied' : 'off');
    await navigator.serviceWorker.register('/sw.js');
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      const b64 = window.__pbPushKey.replace(/-/g, '+').replace(/_/g, '/');
      const raw = atob(b64 + '='.repeat((4 - b64.length % 4) % 4));
      const key = Uint8Array.from(raw, (c) => c.charCodeAt(0));
      sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
    }
    const r = await fetch('/api/push/subscribe', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ subscription: sub.toJSON(), device: window.__pbPushDevice() }),
    });
    if (!r.ok) return window.__pbPushSay('failed');
    location.reload();
  } catch (e) {
    console.warn('push subscribe failed', e);
    window.__pbPushSay('failed');
  }
};
window.__pbPushOff = async () => {
  try {
    const reg = window.__pbPushSupported() && await navigator.serviceWorker.getRegistration('/');
    const sub = reg && await reg.pushManager.getSubscription();
    if (sub) {
      await fetch('/api/push/unsubscribe', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ endpoint: sub.endpoint }),
      });
      await sub.unsubscribe();
    }
    location.reload();
  } catch (e) {
    console.warn('push unsubscribe failed', e);
    window.__pbPushSay('failed');
  }
};
"""


def _analyse_now_section(username: str, refresh) -> None:
    """Work through part of the archive now instead of waiting for the night."""
    _ = get_translator()
    from proactive.tasks import sweep

    ui.label(_("Analyse the archive now")).classes("text-sm font-semibold text-gray-200")
    ui.label(
        _(
            "Normally your existing documents are analysed at night. Choose how many "
            "to analyse right away — about 15 seconds each on the GPU. A chat always "
            "goes first; the analysis continues afterwards."
        )
    ).classes("text-xs text-gray-400 leading-relaxed")
    left = sweep.now_remaining(username)
    _last, finished = sweep.cursor(username)
    if finished:
        ui.label(_("All documents are analysed.")).classes("text-xs text-gray-300")
        return
    if left:
        with ui.row().classes("items-center gap-2"):
            ui.label(_("{n} documents still to go in this run.").format(n=left)).classes(
                "text-xs text-gray-300")  # TODO i18n-plural

            def _stop() -> None:
                sweep.stop_now(username)
                refresh()

            ui.button(_("Stop"), on_click=_stop).props("flat dense size=sm")
            ui.button(icon="refresh", on_click=refresh).props("flat dense round size=sm")
        return
    with ui.row().classes("items-end gap-2 w-full no-wrap"):
        n_in = ui.number(_("Number of documents"), value=50, min=1, max=sweep.SWEEP_NOW_MAX,
                         step=10, format="%d").props("outlined dark dense").classes("flex-1 min-w-0")

        def _start() -> None:
            try:
                n = int(n_in.value)
            except (TypeError, ValueError):
                n = 0
            if not 1 <= n <= sweep.SWEEP_NOW_MAX:
                ui.notify(_("Choose a number from 1 to {max}.").format(max=sweep.SWEEP_NOW_MAX),
                          type="warning")
                return
            sweep.start_now(username, n)
            ui.notify(_("Started — {n} documents.").format(n=n), type="positive")
            refresh()

        ui.button(_("Analyse now"), icon="play_arrow", on_click=_start).props(
            "unelevated dense color=primary")


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

    # ── Quiet hours ──────────────────────────────────────────────────────────
    ui.label(_("Quiet hours")).classes("text-sm font-semibold text-gray-200 mt-4")
    with ui.row().classes("gap-2 w-full no-wrap"):
        q_from = ui.input(_("From"), value=p.quiet_start).props(
            "outlined dark dense type=time").classes("flex-1 min-w-0")
        q_to = ui.input(_("Until"), value=p.quiet_end).props(
            "outlined dark dense type=time").classes("flex-1 min-w-0")
    ui.label(
        _(
            "Reminders and insights found in this time are sent when it ends. "
            "The same time twice = no quiet hours."
        )
    ).classes("text-xs text-gray-500")

    def _save_quiet() -> None:
        cur = pprefs.get(username)
        cur.quiet_start, cur.quiet_end = (q_from.value or "")[:5], (q_to.value or "")[:5]
        try:
            pprefs.save(cur)
        except ValueError:
            ui.notify(_("Enter times as HH:MM."), type="warning")
            return
        ui.notify(_("Saved"), type="positive")

    ui.button(_("Save"), on_click=_save_quiet).props("flat dense size=sm")

    # ── Findings language ────────────────────────────────────────────────────
    from i18n import SUPPORTED_LANGUAGES

    ui.label(_("Language of findings")).classes("text-sm font-semibold text-gray-200 mt-4")
    options = {"": _("Same as the app"), **SUPPORTED_LANGUAGES}

    def _on_lang(e) -> None:
        cur = pprefs.get(username)
        cur.findings_language = e.value or ""
        pprefs.save(cur)
        ui.notify(_("Saved — applies to new findings."), type="positive")

    ui.select(options, value=p.findings_language if p.findings_language in options else "",
              on_change=_on_lang).props("outlined dark dense").classes("w-full")


def _push_status(d: dict) -> str:
    """What the push service said to the last message for this device —
    accepted means it is on its way; the phone showing it is up to the phone."""
    _ = get_translator()
    status, sent = d.get("last_status"), d.get("last_sent")
    if not sent:
        return _("nothing sent yet")
    from proactive import clock

    when = clock.parse(sent).astimezone(clock.local_now().tzinfo).strftime("%d.%m. %H:%M")
    if status in (200, 201, 202):
        return _("last message accepted {when}").format(when=when)
    return _("last message refused ({status}) {when}").format(status=status or "—", when=when)
