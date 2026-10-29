"""The no-op job: proves the tick loop and the runner are alive (Phase 1)."""

from __future__ import annotations

import logging

from proactive.jobs import Job, handler

log = logging.getLogger("proactive.heartbeat")


@handler("heartbeat")
async def heartbeat(job: Job) -> None:
    log.info("heartbeat (job %s)", job.id)
