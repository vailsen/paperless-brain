"""The finding state machine (architecture §4.3) — a pure function.

    new ──push──> surfaced ──interested──> interested ──researched──> researched ──done──> done
     │               ├──later──> snoozed ──wake──> surfaced
     └──hold──> held ├──dismiss──> dismissed (this one only)
                     └──never──> dismissed (+ topic muted)
    Reactions also apply to `new`: a finding is listed in the app before the
    ledger scan has decided whether to send it.
    held ──digest──> surfaced
    dismissed ──reopen──> new      (only when the evidence changed)
    dismissed/done ──restore──> surfaced   (the user takes it back; no new push)
    researched ──interested──> interested  ("Research again": a second result,
                                           stacked above the first)
    open ──relevant──> relevant ──remind──> surfaced
                       (read, it matters, keep it in view; a deadline reminder
                        puts it back in the inbox like a new mail)

What the user sees is an inbox (`display_status`): open, relevant, done,
dismissed. new / surfaced / held / snoozed are the notification bookkeeping
behind "open", interested / researched the research behind "relevant". "Later"
(snoozed) is no longer offered; a snoozed finding still wakes up.

Reactions are accepted only from the states where they make sense: the same
button pressed twice, or in two open tabs, finds a state it does not apply to
and is ignored.
"""

from __future__ import annotations

STATUSES = (
    "new", "surfaced", "held", "interested", "researched", "done", "dismissed", "snoozed", "relevant",
)
REACTIONS = ("interested", "relevant", "later", "dismiss", "never", "done", "wrong", "restore")

_OPEN = {"new", "surfaced", "held", "snoozed"}

#: event → (allowed from, to)
TRANSITIONS: dict[str, tuple[frozenset[str], str]] = {
    # system events
    "push": (frozenset({"new", "held"}), "surfaced"),
    "hold": (frozenset({"new"}), "held"),
    "digest": (frozenset({"held"}), "surfaced"),
    "wake": (frozenset({"snoozed"}), "surfaced"),
    "researched": (frozenset({"interested"}), "researched"),
    "remind": (frozenset({"relevant"}), "surfaced"),
    "reopen": (frozenset({"dismissed", "done"}), "new"),
    # reactions
    "interested": (frozenset(_OPEN | {"researched", "relevant"}), "interested"),
    "relevant": (frozenset(_OPEN), "relevant"),
    # not offered any more; kept so an old message link does not fail
    "later": (frozenset({"new", "surfaced", "held"}), "snoozed"),
    "dismiss": (frozenset(_OPEN | {"interested", "researched", "relevant"}), "dismissed"),
    "never": (frozenset(_OPEN | {"interested", "researched", "relevant"}), "dismissed"),
    "done": (frozenset(_OPEN | {"interested", "researched", "relevant"}), "done"),
    "wrong": (frozenset(_OPEN | {"relevant"}), "dismissed"),
    "restore": (frozenset({"dismissed", "done"}), "surfaced"),
}


def transition(status: str, event: str) -> str | None:
    """The next status, or None when `event` does not apply to `status`."""
    rule = TRANSITIONS.get(event)
    if rule is None:
        raise ValueError(f"unknown event {event!r}")
    allowed, target = rule
    return target if status in allowed else None


#: The inbox the user sees, and which stored statuses each one covers.
DISPLAY = {
    "open": frozenset({"new", "surfaced", "held", "snoozed"}),
    "relevant": frozenset({"relevant", "interested", "researched"}),
    "done": frozenset({"done"}),
    "dismissed": frozenset({"dismissed"}),
}


def display_status(status: str) -> str:
    return next((d for d, s in DISPLAY.items() if status in s), "open")


def reactions_for(status: str, kind: str) -> list[str]:
    """Buttons to offer for a finding in `status`."""
    offer = (["relevant", "done", "dismiss", "wrong"] if kind == "deadline"
             else ["relevant", "interested", "dismiss", "never", "done"]) + ["restore"]
    return [r for r in offer if transition(status, r) is not None]
