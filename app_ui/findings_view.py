"""How the findings list is filtered, sorted and grouped — pure, no UI.

Grouping is headlines only: a group is a heading followed by its findings,
never something to expand or collapse. Whoever wants less to scroll filters by
topic. With "topic, then status" each topic heading carries status
subheadings.

Order of the groups follows the chosen sort: the topic whose best finding
sorts first comes first. Status groups keep the order of the workflow —
what waits for the user first, what is closed last.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from i18n import N_

STATUS_LABELS = {
    "new": N_("New"),
    "surfaced": N_("Notified"),
    "held": N_("Held back"),
    "interested": N_("Interesting"),
    "researched": N_("Researched"),
    "done": N_("Done"),
    "dismissed": N_("Dismissed"),
    "snoozed": N_("Later"),
}
#: Material icons; they inherit the text colour like every icon in the app.
STATUS_ICONS = {
    "new": "fiber_new",
    "surfaced": "notifications_active",
    "held": "notifications_paused",
    "interested": "travel_explore",
    "researched": "fact_check",
    "done": "task_alt",
    "dismissed": "block",
    "snoozed": "snooze",
}
#: Workflow order: waiting for the user → being worked on → set aside → closed.
STATUS_ORDER = ("surfaced", "new", "researched", "interested", "snoozed", "held", "done", "dismissed")
OPEN_STATUSES = frozenset({"new", "surfaced", "held", "interested", "researched", "snoozed"})
CLOSED_STATUSES = frozenset({"done", "dismissed"})

GROUPS = {
    "none": N_("No grouping"),
    "topic": N_("By topic"),
    "status": N_("By status"),
    "topic_status": N_("By topic, then status"),
}
SORTS = {
    "relevance": N_("Relevance"),
    "urgency": N_("Urgency"),
    "deadline": N_("Deadline"),
    "newest": N_("Newest first"),
    "oldest": N_("Oldest first"),
}
DEFAULT_VIEW = {"status": "open", "topic": "", "group": "topic", "sort": "relevance"}

URGENCY_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0}
#: A deadline this close is something to react to — it is marked like high urgency.
DUE_SOON_DAYS = 7


def days_left(f, today: date) -> int | None:
    if not f.deadline:
        return None
    try:
        return (date.fromisoformat(str(f.deadline)[:10]) - today).days
    except ValueError:
        return None


def is_pressing(f, today: date) -> bool:
    """High or critical, or a deadline within a week — the warn signal. Never
    for a closed finding: done is done."""
    if f.status in CLOSED_STATUSES:
        return False
    d = days_left(f, today)
    return URGENCY_RANK.get(f.urgency, 1) >= 2 or (d is not None and d <= DUE_SOON_DAYS)


def relevance(f, today: date) -> float:
    """The ledger's priority (urgency × interest weight); a deadline finding
    has none, so its urgency stands in. A deadline within a week counts as
    critical."""
    from proactive.scoring import URGENCY_SCORE

    score = float(f.priority or 0) or URGENCY_SCORE.get(f.urgency, 0.5)
    d = days_left(f, today)
    if d is not None and d <= DUE_SOON_DAYS and f.status not in CLOSED_STATUSES:
        score = max(score, 1.0)
    return score


def sort_key(sort: str, today: date):
    if sort == "urgency":
        return lambda f: (-URGENCY_RANK.get(f.urgency, 1), -relevance(f, today), _neg_created(f))
    if sort == "deadline":
        return lambda f: (f.deadline or "9999", -URGENCY_RANK.get(f.urgency, 1), _neg_created(f))
    if sort == "newest":
        return _neg_created
    if sort == "oldest":
        return lambda f: f.created or ""
    return lambda f: (-relevance(f, today), -URGENCY_RANK.get(f.urgency, 1), _neg_created(f))


class _Desc(str):
    """A string that sorts backwards — newest first inside a tuple key."""

    def __lt__(self, other):  # type: ignore[override]
        return str.__gt__(self, other)

    def __gt__(self, other):  # type: ignore[override]
        return str.__lt__(self, other)


def _neg_created(f):
    return _Desc(f.created or "")


def keep(f, status: str, topic: str) -> bool:
    if status == "open" and f.status not in OPEN_STATUSES:
        return False
    if status not in ("open", "all") and f.status != status:
        return False
    return not topic or f.topic == topic


@dataclass
class Group:
    key: str  # topic key or status; "" for the one ungrouped list
    label: str  # topic as written, or the status key (translated at render)
    kind: str  # "topic" | "status" | "none"
    items: list = field(default_factory=list)
    subgroups: list["Group"] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.items) + sum(g.count for g in self.subgroups)


def effective_group(group: str, topic: str) -> str:
    """A topic filter leaves one topic: grouping by it would be one heading."""
    if topic and group == "topic":
        return "none"
    if topic and group == "topic_status":
        return "status"
    return group if group in GROUPS else "none"


def arrange(items: list, *, status: str, topic: str, group: str, sort: str, today: date) -> list[Group]:
    shown = sorted((f for f in items if keep(f, status, topic)), key=sort_key(sort, today))
    group = effective_group(group, topic)
    if group == "none":
        return [Group("", "", "none", shown)] if shown else []
    if group == "status":
        return _by_status(shown)
    topics = _by_topic(shown)
    if group == "topic_status":
        for t in topics:
            t.subgroups, t.items = _by_status(t.items), []
    return topics


def _by_status(shown: list) -> list[Group]:
    out = {s: Group(s, s, "status") for s in STATUS_ORDER}
    for f in shown:
        out.setdefault(f.status, Group(f.status, f.status, "status")).items.append(f)
    return [g for g in out.values() if g.items]


def _by_topic(shown: list) -> list[Group]:
    # Insertion order = order of each topic's first (best-sorted) finding.
    out: dict[str, Group] = {}
    for f in shown:
        key = f.topic or ""
        g = out.get(key)
        if g is None:
            g = out[key] = Group(key, f.topic_display if key else "", "topic")
        elif f.topic_label and g.label == key:
            g.label = f.topic_display  # a finding with the label as written wins
        g.items.append(f)
    # Findings without a topic last, whatever their rank.
    return sorted(out.values(), key=lambda g: g.key == "")


def counts(items: list, today: date) -> dict:
    """The summary line above the list: how many wait, how many press."""
    open_ = [f for f in items if f.status in OPEN_STATUSES]
    return {
        "open": len(open_),
        "waiting": sum(1 for f in open_ if f.status in ("surfaced", "new", "researched")),
        "pressing": sum(1 for f in open_ if is_pressing(f, today)),
        "researching": sum(1 for f in open_ if f.status == "interested"),
    }
