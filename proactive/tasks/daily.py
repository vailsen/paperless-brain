"""The daily run (`PROACTIVE_DAILY_RUN_TIME`): deadline reminders for every
eligible user — from the ledger only, no LLM — and (Phase 7) the quiet-period
digest."""

from __future__ import annotations

import logging

from proactive import deadlines, prefs, users
from proactive.jobs import Job, handler

log = logging.getLogger("proactive.daily")

DAILY_RUN = "daily_run"

#: Phase 7 adds the digest check here.
_extra: list = []


def also(fn):
    _extra.append(fn)
    return fn


@handler(DAILY_RUN)
async def daily_run(job: Job) -> None:
    from config.proactive import proactive_settings

    default = proactive_settings().deadline_offsets
    for user, _tok in users.eligible():
        offsets = prefs.get(user).deadline_offsets or default
        n = await deadlines.remind(user, offsets)
        if n:
            log.info("%s: %d deadline reminder(s)", user, n)
        for fn in _extra:
            await fn(user)
