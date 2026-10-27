"""Notification delivery jobs (worker side of `proactive.notify`)."""

from __future__ import annotations

import logging

from proactive import linking, prefs
from proactive.jobs import Job, handler
from proactive.notify import Notification, compose

log = logging.getLogger("proactive.notify")


@handler("notify_channel")
async def notify_channel(job: Job) -> None:
    """Deliver one notification on one channel. Retried on its own."""
    channel = job.payload["channel"]
    n = Notification.from_payload(job.payload["notification"])
    if channel == "push":
        from proactive.notify import push_body, webpush

        await webpush.send(
            job.user, webpush.payload(n.title, push_body(n), n.link, tag=n.finding_id)
        )
        return
    link = linking.links(job.user).get(channel)
    if link is None:
        log.info("%s: %s no longer linked — not sent", job.user, channel)
        return
    if channel == "telegram":
        from proactive.notify import telegram

        await telegram.send(link["address"], n, full_text=link["full_text"])
    elif channel == "email":
        from proactive.notify import email

        mid = await email.send(link["address"], n, full_text=link["full_text"])
        log.info("%s: email sent %s", job.user, mid)
    else:
        raise ValueError(f"unknown channel {channel!r}")


@handler("push_event")
async def push_event(job: Job) -> None:
    """An app event (`notify.enqueue_event`) to the user's push devices."""
    from proactive.notify import webpush

    p = job.payload
    await webpush.send(job.user, webpush.payload(p["title"], p.get("body", ""), p["url"], p.get("tag", "")))


@handler("send_test")
async def send_test(job: Job) -> None:
    """The settings page's "Send test message" button."""
    from i18n import translator_for

    lang = prefs.get(job.user).language
    _ = translator_for(lang)
    n = compose(
        job.user,
        lang,
        finding_id="",
        title=_("PaperlessBrain test message"),
        urgency="low",
        full_text=_("If you can read this, notifications reach you on this channel."),
        counts=False,
    )
    from proactive import notify

    notify.enqueue(n)  # logs when no linked channel is chosen


@handler("email_verify")
async def email_verify(job: Job) -> None:
    """Mail the verification code to an address the user entered."""
    from i18n import translator_for
    from proactive.db import connect
    from proactive.notify import email

    lang = prefs.get(job.user).language
    _ = translator_for(lang)
    code = job.payload.get("code", "")
    to = job.payload.get("address", "")
    if not code or not to:
        return
    msg = email.plain(
        to,
        _("Your PaperlessBrain confirmation code"),
        _(
            "Your code: {code}\n\nEnter it in PaperlessBrain under Settings > Proactive "
            "within 10 minutes. If you did not ask for this, ignore this message."
        ).format(code=code),
    )
    await email.send_message(msg)
    # The code is only needed until it is sent; do not leave it in the queue.
    with connect() as conn:
        conn.execute("UPDATE jobs SET payload_json = '{}' WHERE id = ?", (job.id,))
