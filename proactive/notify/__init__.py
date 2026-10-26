"""Notifier: one `Notification`, delivered on each channel the user chose
(architecture §7).

Delivery is a job per (notification, channel) — `notify_channel` — so a
failing channel is retried on its own and a working one never sends twice.
Only channels that are available (server config present), chosen by the user
*and* linked by them are used. The default content is title + urgency + link;
the full text only where the user opted in for that channel (both channels
leave the premises; Telegram bot chats are not end-to-end encrypted).

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
    "never": N_("Never again"),
    "done": N_("Done"),
    "wrong": N_("Wrongly detected"),
}
INSIGHT_ACTIONS = ["interested", "later", "never"]
DEADLINE_ACTIONS = ["done", "wrong"]


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
    )


def chosen_channels(user: str) -> list[str]:
    """Available ∩ chosen ∩ linked, in a fixed order."""
    from config.proactive import proactive_settings
    from proactive import linking, prefs

    cfg = proactive_settings()
    available = {"telegram": cfg.telegram_available, "email": cfg.email_available}
    links = linking.links(user)
    return [c for c in prefs.get(user).channel_list() if available[c] and c in links]


def enqueue(n: Notification) -> list[str]:
    """Queue delivery on every chosen channel. Returns the channels queued."""
    from proactive import jobs

    channels = chosen_channels(n.user)
    for ch in channels:
        jobs.enqueue(
            "notify_channel",
            n.user,
            {"channel": ch, "notification": n.to_payload()},
            dedupe_key=f"notify:{ch}:{n.finding_id}:{n.title}" if n.finding_id else None,
        )
    if not channels:
        log.info("%s: no linked channel chosen — %r not sent", n.user, n.title)
    return channels
