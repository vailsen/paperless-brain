"""Notifier: one `Notification`, delivered on each channel the user chose
(architecture §7).

Delivery is a job per (notification, channel) — `notify_channel` — so a
failing channel is retried on its own and a working one never sends twice.
Only channels that are available (server config present), chosen by the user
*and* linked by them are used. The default content is title + urgency + link;
the full text only where the user opted in for email (it leaves the premises
unencrypted). Push (`webpush`, end-to-end encrypted) carries title, note and
urgency; tapping it opens the link.

Everything here runs in the worker. The app only enqueues jobs.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field

from i18n import N_

log = logging.getLogger("proactive.notify")

URGENCY_LABELS = {
    "low": N_("Low"),
    "medium": N_("Medium"),
    "high": N_("High"),
    "critical": N_("Critical"),
}
ACTION_LABELS = {
    "interested": N_("Interesting"),
    "later": N_("Later"),
    "relevant": N_("Relevant"),
    "dismiss": N_("Not relevant"),
    "never": N_("Never again"),
    "done": N_("Done"),
    "wrong": N_("Wrongly detected"),
    "restore": N_("Restore"),
}
INSIGHT_ACTIONS = ["relevant", "interested", "never"]
DEADLINE_ACTIONS = ["done", "dismiss", "wrong"]


@dataclass
class Notification:
    user: str
    finding_id: str  # "" for messages that are not about a finding (test, digest)
    title: str  # localized
    urgency: str
    body_short: str  # title + urgency + link
    body_full: str = ""  # only with the channel's full-text opt-in
    actions: list[str] = field(default_factory=list)
    link: str = ""
    lang: str = "en"
    #: Counts against the weekly message limit (Phase 7). Requested deep-dive
    #: results and critical deadline reminders do not.
    counts: bool = True
    #: Push tag: a message with the tag of one still on the phone replaces it.
    #: Defaults to the finding id; a second kind of message about the same
    #: finding (the research result) needs its own.
    tag: str = ""

    def to_payload(self) -> dict:
        return asdict(self)

    @classmethod
    def from_payload(cls, d: dict) -> "Notification":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def app_link(path: str) -> str:
    from config.proactive import proactive_settings

    base = proactive_settings().proactive_app_base_url.rstrip("/")
    return f"{base}{path}" if base else path


def finding_link(finding_id: str, action: str = "") -> str:
    q = f"?action={action}" if action else ""
    return app_link(f"/findings/{finding_id}{q}")


def compose(
    user: str,
    lang: str,
    *,
    finding_id: str,
    title: str,
    urgency: str,
    full_text: str = "",
    actions: list[str] | None = None,
    counts: bool = True,
    note: str = "",
    tag: str = "",
) -> Notification:
    """Build a notification in the recipient's language. `note` is one short
    line that belongs in the default content (a reminder's "7 days left" —
    nothing more revealing than the title)."""
    from i18n import translator_for

    _ = translator_for(lang)
    link = finding_link(finding_id) if finding_id else app_link("/findings")
    urgency_label = _(URGENCY_LABELS.get(urgency, URGENCY_LABELS["medium"]))
    short = "\n".join(
        p for p in [title, note, _("Urgency: {urgency}").format(urgency=urgency_label), link] if p
    )
    full = "\n\n".join(p for p in (short, full_text) if p) if full_text else ""
    return Notification(
        user=user,
        finding_id=finding_id,
        title=title,
        urgency=urgency,
        body_short=short,
        body_full=full,
        actions=list(actions or []),
        link=link,
        lang=lang,
        counts=counts,
        tag=tag,
    )


def chosen_channels(user: str) -> list[str]:
    """Available ∩ chosen ∩ linked, in a fixed order."""
    from config.proactive import proactive_settings
    from proactive import linking, prefs

    from proactive.notify import webpush

    cfg = proactive_settings()
    available = {
        "push": cfg.push_available,
        "email": cfg.email_available,
    }
    linked = set(linking.links(user))
    if cfg.push_available and webpush.devices(user):
        linked.add("push")
    return [c for c in prefs.get(user).channel_list() if available[c] and c in linked]


def push_body(n: Notification) -> str:
    """The default content without the title (shown above it) and the link
    (tapping opens it)."""
    lines = n.body_short.split("\n")
    return "\n".join(x for x in lines if x and x != n.title and x != n.link)


EVENT_TITLES = {
    "sync": N_("Sync finished"),
    "werkbank": N_("Werkbank run done"),
}


def enqueue_event(user: str, kind: str, path: str, body: str = "") -> None:
    """An app event ("sync finished", "Werkbank run done") for the user's push
    devices — push only, never email, no weekly limit; each kind
    has its own switch. Called from the app; never raises into it."""
    try:
        from config.proactive import is_enabled, proactive_settings

        if not (user and is_enabled() and proactive_settings().push_available):
            return
        from proactive import jobs, prefs

        from i18n import translator_for

        p = prefs.get(user)
        if not {"sync": p.notify_sync, "werkbank": p.notify_werkbank}.get(kind, False):
            return
        title = translator_for(p.language or "en")(EVENT_TITLES[kind])
        jobs.enqueue(
            "push_event", user,
            {"title": title, "body": body[:200], "url": app_link(path), "tag": kind},
        )
    except Exception:
        log.warning("could not queue the %s notification for %s", kind, user, exc_info=True)


def quiet_wait(quiet_start: str, quiet_end: str, now):
    """How long a message created at `now` (local, aware) waits for quiet hours
    `quiet_start`–`quiet_end` (HH:MM, may cross midnight) to end; `timedelta(0)`
    outside them. Start == end means no quiet hours."""
    from datetime import datetime, time, timedelta

    start, end = time.fromisoformat(quiet_start), time.fromisoformat(quiet_end)
    if start == end:
        return timedelta(0)
    t = now.time().replace(tzinfo=None)
    inside = (start <= t < end) if start < end else (t >= start or t < end)
    if not inside:
        return timedelta(0)
    until = datetime.combine(now.date(), end, tzinfo=now.tzinfo)
    if until <= now:
        until += timedelta(days=1)
    return until - now


def quiet_delay(user: str):
    """`quiet_wait` for the user's own quiet hours, now."""
    from proactive import clock, prefs

    p = prefs.get(user)
    return quiet_wait(p.quiet_start, p.quiet_end, clock.local_now())


def enqueue(n: Notification, *, quiet: bool = True) -> list[str]:
    """Queue delivery on every chosen channel. Returns the channels queued.
    Inside the user's quiet hours delivery waits until they end (`quiet=False`
    only for what the user just asked for, the test message)."""
    from proactive import jobs, pushes

    channels = chosen_channels(n.user)
    delay = quiet_delay(n.user) if quiet and channels else None
    if channels and n.counts:
        pushes.record(n.user, "finding" if n.finding_id else "message", n.finding_id)
    if delay:
        # Held, not queued per message: at the end of quiet hours everything
        # goes out as one message per channel, not twenty in two minutes.
        hold(n, delay)
        return channels
    for ch in channels:
        jobs.enqueue(
            "notify_channel",
            n.user,
            {"channel": ch, "notification": n.to_payload()},
            dedupe_key=f"notify:{ch}:{n.finding_id}:{n.title}" if n.finding_id else None,
            **({"delay": delay} if delay else {}),
        )
    if not channels:
        log.info("%s: no linked channel chosen — %r not sent", n.user, n.title)
    return channels


# ── Quiet hours: hold, then one message ──────────────────────────────────────

QUIET_FLUSH = "quiet_flush"
#: Titles listed in the summary; the rest is "+ N more".
SUMMARY_LINES = 8


def hold(n: Notification, delay) -> None:
    """Keep `n` until the user's quiet hours end; one flush job per user."""
    import json

    from proactive import clock, jobs
    from proactive.db import connect

    with connect() as conn:
        conn.execute(
            "INSERT INTO quiet_hold (user, payload_json, created_at) VALUES (?,?,?)",
            (n.user, json.dumps(n.to_payload(), ensure_ascii=False), clock.now_iso()),
        )
    jobs.enqueue(QUIET_FLUSH, n.user, {}, delay=delay, dedupe_key=f"{QUIET_FLUSH}:{n.user}")
    log.info("%s: quiet hours — %r held for %s", n.user, n.title, delay)


def take_held(user: str) -> list[Notification]:
    import json

    from proactive.db import connect

    with connect() as conn:
        rows = conn.execute(
            "SELECT id, payload_json FROM quiet_hold WHERE user = ? ORDER BY id", (user,)
        ).fetchall()
        conn.execute("DELETE FROM quiet_hold WHERE user = ?", (user,))
    return [Notification.from_payload(json.loads(r["payload_json"])) for r in rows]


def summary(user: str, held: list[Notification]) -> Notification:
    """One message for everything that waited: the most urgent first."""
    from i18n import translator_for
    from proactive import prefs

    lang = (held[0].lang if held else "") or prefs.get(user).language or "en"
    _ = translator_for(lang)
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    items = sorted(held, key=lambda x: order.get(x.urgency, 2))
    lines = [f"• {x.title}" for x in items[:SUMMARY_LINES]]
    if len(items) > SUMMARY_LINES:
        lines.append(_("+ {n} more").format(n=len(items) - SUMMARY_LINES))
    top = items[0].urgency if items else "low"
    # TODO i18n-plural
    return compose(
        user, lang, finding_id="", title=_("{n} new while you were away").format(n=len(items)),
        urgency=top, note="\n".join(lines), counts=False,
    )
