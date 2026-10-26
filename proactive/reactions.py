"""Reactions from every channel — Telegram buttons, email replies, the app —
and the system events that move a finding (surface, snooze wake-up).

One entry point, so the rules are the same whichever channel a reaction comes
from. The caller has already established that `user` owns the finding (the
linked chat / linked address / the signed-in user's own ledger); this module
only ever touches that user's files.

Effects of a reaction (architecture §4.3, §8):

- `interested` → status `interested`, count, enqueue `deep_dive`
- `later`      → status `snoozed` for `SNOOZE_DAYS`
- `never`      → status `dismissed`, count, topic muted
- `done`       → status `done`
- `wrong`      → status `dismissed` (deadline misread), logged for prompt tuning

A reaction that does not apply to the current status (the same button pressed
twice, or the second of two channels) changes nothing and says so.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from proactive import clock, findings, interests
from proactive.states import REACTIONS, transition

log = logging.getLogger("proactive.reactions")

SNOOZE_DAYS = 7


def owner_of(finding_id: str) -> str | None:
    f = findings.locate(finding_id)
    return f.user if f else None


def _message(lang: str, outcome: str) -> str:
    from i18n import translator_for

    _ = translator_for(lang)
    return {
        "interested": _("Noted — a closer look is being prepared."),
        "later": _("Snoozed for a week."),
        "never": _("Understood — no more messages on this topic."),
        "done": _("Marked as done."),
        "wrong": _("Thanks — marked as wrongly detected."),
        "ignored": _("Already handled."),
        "missing": _("This item no longer exists."),
    }[outcome]


async def react(user: str, finding_id: str, action: str, *, channel: str) -> str:
    """Apply a reaction. Returns a short message in the user's language."""
    if action not in REACTIONS:
        raise ValueError(f"unknown reaction {action!r}")
    before: dict = {}

    def mutate(f: findings.Finding) -> dict | None:
        before["status"], before["f"] = f.status, f
        nxt = transition(f.status, action)
        if nxt is None:
            return None
        changes: dict = {"status": nxt}
        if action == "later":
            changes["snoozed_until"] = (clock.local_now().date() + timedelta(days=SNOOZE_DAYS)).isoformat()
        return changes

    f = await findings.update(user, finding_id, mutate)
    if f is None:
        return _message("en", "missing")
    if transition(before["status"], action) is None:
        log.info("%s: %s on finding %s (%s) via %s ignored", user, action, finding_id, before["status"], channel)
        return _message(f.lang, "ignored")
    log.info("%s: %s on finding %s via %s → %s", user, action, finding_id, channel, f.status)

    if f.topic:
        if action == "interested":
            await interests.record(user, f.topic, "interested")
        elif action == "never":
            await interests.record(user, f.topic, "never", "dismissed", mute=True)
        elif action == "wrong":
            await interests.record(user, f.topic, "dismissed")
    if action == "interested":
        from proactive import jobs

        jobs.enqueue("deep_dive", user, {"finding_id": finding_id}, dedupe_key=f"deep_dive:{finding_id}")
    if action == "wrong":
        # Prompt tuning material: which extraction was wrong, nothing else.
        log.warning(
            "deadline marked wrong: user=%s finding=%s docs=%s deadline=%s kind=%s",
            user, finding_id, f.doc_ids, f.deadline, f.deadline_kind,
        )
    return _message(f.lang, action)


async def surface(user: str, finding_id: str, *, event: str = "push", counts: bool = True, full_text: str = "") -> bool:
    """Move a finding to `surfaced` and notify. False if the event does not
    apply (already surfaced, dismissed, …) — then nothing is sent."""
    from proactive import notify
    from proactive.notify import DEADLINE_ACTIONS, INSIGHT_ACTIONS

    moved: dict = {}

    def mutate(f: findings.Finding) -> dict | None:
        nxt = transition(f.status, event)
        moved["ok"] = nxt is not None
        if nxt is None:
            return None
        return {"status": nxt, "surfaced_at": clock.local_now().isoformat(timespec="seconds"), "snoozed_until": None}

    f = await findings.update(user, finding_id, mutate)
    if f is None or not moved.get("ok"):
        return False
    if f.topic:
        await interests.record(user, f.topic, "surfaced")
    n = notify.compose(
        user,
        f.lang,
        finding_id=f.finding_id,
        title=f.title,
        urgency=f.urgency,
        full_text=full_text or _summary(f.body),
        actions=DEADLINE_ACTIONS if f.kind == "deadline" else INSIGHT_ACTIONS,
        counts=counts,
    )
    notify.enqueue(n)
    return True


def _summary(body: str) -> str:
    """The first section's text — what "full text" shows in a message."""
    lines, started = [], False
    for line in body.splitlines():
        if line.startswith("## "):
            if started:
                break
            started = True
            continue
        if started:
            lines.append(line)
    return "\n".join(lines).strip()
