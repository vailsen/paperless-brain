"""Ledger scan: new findings get a push-or-hold decision, snoozed ones wake up.

Reads the files every time — there is no cached ledger to go stale, so a status
edited in Obsidian counts from the next scan on. Deadline findings are not
handled here; their reminders follow the deadline offsets (Phase 6).
"""

from __future__ import annotations

import logging

from proactive import clock, findings, reactions, users
from proactive.jobs import Job, handler

log = logging.getLogger("proactive.ledger")

LEDGER_SCAN = "ledger_scan"


async def decide(user: str, f: findings.Finding) -> str:
    """`push` or `hold` for a new insight. Scoring arrives in Phase 7; until
    then every new insight is pushed."""
    return "push"


@handler(LEDGER_SCAN)
async def ledger_scan(job: Job) -> None:
    today = clock.local_now().date().isoformat()
    for user, _tok in users.eligible():
        for f in findings.list_findings(user):
            if f.kind == "deadline":
                continue
            if f.status == "new":
                verdict = await decide(user, f)
                if verdict == "push":
                    await reactions.surface(user, f.finding_id)
                else:
                    await findings.update(user, f.finding_id, lambda x: {"status": "held"} if x.status == "new" else None)
            elif f.status == "snoozed" and (f.snoozed_until or "") <= today:
                await reactions.surface(user, f.finding_id, event="wake")


@handler("deep_dive")
async def deep_dive(job: Job) -> None:
    """Placeholder until Phase 7 (`run_deep_dive`)."""
    log.info("%s: deep dive for finding %s requested", job.user, job.payload.get("finding_id"))
