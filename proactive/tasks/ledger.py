"""Ledger scan: new findings get a push-or-hold decision (`scoring.decide`:
priority from urgency × the topic's interest weight, muted topics held, the
weekly limit), snoozed ones wake up.

Reads the files every time — there is no cached ledger to go stale, so a status
edited in Obsidian counts from the next scan on. Deadline findings are not
handled here; their reminders follow the deadline offsets (Phase 6).
"""

from __future__ import annotations

import logging

from proactive import clock, findings, interests, reactions, users
from proactive.jobs import Job, handler

log = logging.getLogger("proactive.ledger")

LEDGER_SCAN = "ledger_scan"


@handler(LEDGER_SCAN)
async def ledger_scan(job: Job) -> None:
    from proactive import scoring

    today = clock.local_now().date().isoformat()
    for user, _tok in users.eligible():
        known = interests.load(user)
        for f in findings.list_findings(user):
            if f.kind == "deadline":
                continue
            if f.status == "new":
                verdict, pri = scoring.decide(user, f, known)
                if verdict == "push":
                    await findings.update(user, f.finding_id, lambda x, p=pri: {"priority": p})
                    await reactions.surface(user, f.finding_id)
                    known = interests.load(user)
                else:
                    await findings.update(
                        user, f.finding_id,
                        lambda x, p=pri: {"status": "held", "priority": p} if x.status == "new" else None,
                    )
            elif f.status == "snoozed" and (f.snoozed_until or "") <= today:
                await reactions.surface(user, f.finding_id, event="wake")
