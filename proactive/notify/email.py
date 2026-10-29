"""Email notifications through the one system mailbox (architecture §7.2).

Plain SMTP via the standard library, run in a thread: the spec suggested
`aiosmtplib`, but a notification rate of a few per user per week does not
justify a new dependency, and `smtplib` does STARTTLS, SSL and plain alike.

Send-only, like push: title, urgency and one link to the finding, where the
answers are. Nothing reads the mailbox — replies were parsed once (IMAP) and
that moved the user's own mail; removed 2026-10-27.
"""

from __future__ import annotations

import asyncio
import html
import logging
import secrets
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, parseaddr

from proactive.notify import Notification

log = logging.getLogger("proactive.email")



def from_domain() -> str:
    from config.proactive import proactive_settings

    addr = parseaddr(proactive_settings().smtp_from)[1]
    return addr.rpartition("@")[2] or "paperlessbrain.local"


def message_id(finding_id: str = "") -> str:
    prefix = finding_id or "msg"
    return f"<{prefix}.{secrets.token_hex(6)}@{from_domain()}>"


def build(to: str, n: Notification, *, full_text: bool) -> EmailMessage:
    from config.proactive import proactive_settings

    body = n.body_full if full_text and n.body_full else n.body_short
    msg = EmailMessage()
    msg["From"] = proactive_settings().smtp_from
    msg["To"] = to
    msg["Subject"] = n.title
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = message_id(n.finding_id)
    msg["Auto-Submitted"] = "auto-generated"

    # The body already holds the link (`notify.compose`); in HTML it is clickable.
    msg.set_content(body)
    lines = []
    for line in body.splitlines():
        esc = html.escape(line)
        if n.link and line == n.link:
            esc = f'<a href="{esc}">{esc}</a>'
        lines.append(esc)
    msg.add_alternative(f"<html><body><p>{'<br>'.join(lines)}</p></body></html>", subtype="html")
    return msg


def plain(to: str, subject: str, text: str) -> EmailMessage:
    from config.proactive import proactive_settings

    msg = EmailMessage()
    msg["From"] = proactive_settings().smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = message_id()
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(text)
    return msg


def _send_sync(msg: EmailMessage) -> None:
    from config.proactive import proactive_settings

    cfg = proactive_settings()
    if not cfg.email_available:
        raise RuntimeError("email is not configured")
    ctx = ssl.create_default_context()
    if cfg.smtp_security == "ssl":
        server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=ctx, timeout=30)
    else:
        server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
    with server:
        if cfg.smtp_security == "starttls":
            server.starttls(context=ctx)
        if cfg.smtp_user:
            server.login(cfg.smtp_user, cfg.smtp_password)
        server.send_message(msg)


async def send_message(msg: EmailMessage) -> None:
    await asyncio.to_thread(_send_sync, msg)


async def send(to: str, n: Notification, *, full_text: bool) -> str:
    """Send; returns the Message-ID used."""
    msg = build(to, n, full_text=full_text)
    await send_message(msg)
    return msg["Message-ID"]
