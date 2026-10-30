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

Reactions are accepted only from the states where they make sense: the same
button pressed twice, or in two open tabs, finds a state it does not apply to
and is ignored.
"""

from __future__ import annotations

STATUSES = (
    "new", "surfaced", "held", "interested", "researched", "done", "dismissed", "snoozed",
)
REACTIONS = ("interested", "later", "dismiss", "never", "done", "wrong", "restore")

_OPEN = {"new", "surfaced", "held", "snoozed"}

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
    "interested": (frozenset(_OPEN | {"researched"}), "interested"),
    "later": (frozenset({"new", "surfaced", "held"}), "snoozed"),
    "dismiss": (frozenset(_OPEN | {"interested", "researched"}), "dismissed"),
    "never": (frozenset(_OPEN | {"interested", "researched"}), "dismissed"),
    "done": (frozenset(_OPEN | {"interested", "researched"}), "done"),
    "wrong": (frozenset(_OPEN), "dismissed"),
    "restore": (frozenset({"dismissed", "done"}), "surfaced"),
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
    offer = (["done", "dismiss", "wrong"] if kind == "deadline"
             else ["interested", "later", "dismiss", "never", "done"]) + ["restore"]
    return [r for r in offer if transition(status, r) is not None]
