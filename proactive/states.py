"""The finding state machine (architecture §4.3) — a pure function.

    new ──push──> surfaced ──interested──> interested ──researched──> researched ──done──> done
     │               ├──later──> snoozed ──wake──> surfaced
     └──hold──> held └──never──> dismissed (+ topic muted)
    held ──digest──> surfaced
    dismissed ──reopen──> new      (only when the evidence changed)

Reactions are accepted only from the states where they make sense. That is
what makes "Telegram and email" safe: the first reaction moves the status,
and the same or another reaction from the second channel finds a state it
does not apply to and is ignored.
"""

from __future__ import annotations

STATUSES = (
    "new", "surfaced", "held", "interested", "researched", "done", "dismissed", "snoozed",
)
REACTIONS = ("interested", "later", "never", "done", "wrong")

_OPEN = {"surfaced", "held", "snoozed"}

#: event → (allowed from, to)
TRANSITIONS: dict[str, tuple[frozenset[str], str]] = {
    # system events
    "push": (frozenset({"new", "held"}), "surfaced"),
    "hold": (frozenset({"new"}), "held"),
    "digest": (frozenset({"held"}), "surfaced"),
    "wake": (frozenset({"snoozed"}), "surfaced"),
    "researched": (frozenset({"interested"}), "researched"),
    "reopen": (frozenset({"dismissed", "done"}), "new"),
    # reactions
    "interested": (frozenset(_OPEN), "interested"),
    "later": (frozenset({"surfaced", "held"}), "snoozed"),
    "never": (frozenset(_OPEN | {"interested", "researched"}), "dismissed"),
    "done": (frozenset(_OPEN | {"interested", "researched"}), "done"),
    "wrong": (frozenset(_OPEN), "dismissed"),
}


def transition(status: str, event: str) -> str | None:
    """The next status, or None when `event` does not apply to `status`."""
    rule = TRANSITIONS.get(event)
    if rule is None:
        raise ValueError(f"unknown event {event!r}")
    allowed, target = rule
    return target if status in allowed else None


def reactions_for(status: str, kind: str) -> list[str]:
    """Buttons to offer for a finding in `status`."""
    offer = ["done", "wrong"] if kind == "deadline" else ["interested", "later", "never", "done"]
    return [r for r in offer if transition(status, r) is not None]
