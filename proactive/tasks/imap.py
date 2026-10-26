"""IMAP reply poller job (Phase 5). Scheduled only when email is configured."""

from __future__ import annotations

from proactive.jobs import Job, handler

IMAP_POLL = "imap_poll"


@handler(IMAP_POLL)
async def imap_poll(job: Job) -> None:
    from proactive.imap_replies import poll_once

    await poll_once()
