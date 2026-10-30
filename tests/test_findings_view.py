"""The findings list: filters, sort orders and headline groups (`app_ui/findings_view.py`)."""

from datetime import date

from app_ui import findings_view as fv
from proactive.findings import Finding

TODAY = date(2026, 10, 29)


def _f(fid, status="surfaced", topic="", urgency="medium", priority=0.0, created="2026-10-01", deadline=None,
       label="", kind="insight"):
    return Finding(finding_id=fid, kind=kind, title=fid, status=status, topic=topic, urgency=urgency,
                   priority=priority, created=created, deadline=deadline, topic_label=label)


ITEMS = [
    _f("kfz1", topic="kfz", label="Kfz", urgency="high", priority=0.6, created="2026-10-26"),
    _f("kfz2", "researched", topic="kfz", priority=0.4, created="2026-10-20"),
    _f("mob1", "interested", topic="mobilfunk", priority=0.5, created="2026-10-27"),
    _f("mob2", "snoozed", topic="mobilfunk", urgency="low", priority=0.2, created="2026-10-10"),
    _f("pay", "new", topic="strom", deadline="2026-11-02", kind="deadline", created="2026-10-28"),
    _f("old", "done", topic="kfz", urgency="critical", priority=0.9, created="2026-09-01"),
    _f("loose", topic="", urgency="low", priority=0.1, created="2026-10-02"),
]


def _ids(groups):
    return [[f.finding_id for f in g.items] for g in groups]


def test_open_by_topic_orders_topics_by_their_best_finding_and_loose_ones_last():
    groups = fv.arrange(ITEMS, status="open", topic="", group="topic", sort="relevance", today=TODAY)
    # the payment due in four days counts as critical; "done" is not open, and
    # researched / being researched are "relevant", not the inbox
    assert [g.key for g in groups] == ["strom", "kfz", "mobilfunk", ""]
    assert _ids(groups)[1] == ["kfz1"]
    assert groups[1].label == "Kfz"  # the topic as written wins over the key


def test_topic_then_status_puts_status_headlines_under_each_topic_in_workflow_order():
    groups = fv.arrange(ITEMS, status="all", topic="", group="topic_status", sort="newest", today=TODAY)
    kfz = next(g for g in groups if g.key == "kfz")
    assert kfz.items == [] and [s.key for s in kfz.subgroups] == ["open", "relevant", "done"]
    assert kfz.count == 3


def test_a_topic_filter_turns_topic_grouping_off():
    assert fv.effective_group("topic", "kfz") == "none"
    assert fv.effective_group("topic_status", "kfz") == "status"
    (g,) = fv.arrange(ITEMS, status="all", topic="kfz", group="topic", sort="oldest", today=TODAY)
    assert g.kind == "none" and [f.finding_id for f in g.items] == ["old", "kfz2", "kfz1"]


def test_by_status_and_single_status_filter():
    groups = fv.arrange(ITEMS, status="all", topic="", group="status", sort="urgency", today=TODAY)
    assert [g.key for g in groups] == ["open", "relevant", "done"]
    assert _ids(groups)[0][0] == "kfz1"  # high first
    (g,) = fv.arrange(ITEMS, status="relevant", topic="", group="none", sort="relevance", today=TODAY)
    assert [f.finding_id for f in g.items] == ["mob1", "kfz2"]
    # a status stored before the inbox ("snoozed") reads as "open"
    (g,) = fv.arrange(ITEMS, status="snoozed", topic="mobilfunk", group="none", sort="relevance", today=TODAY)
    assert [f.finding_id for f in g.items] == ["mob2"]


def test_deadline_sort_puts_findings_without_one_last():
    (g,) = fv.arrange(ITEMS, status="all", topic="", group="none", sort="deadline", today=TODAY)
    assert g.items[0].finding_id == "pay"


def test_pressing_is_high_urgency_or_due_within_a_week_and_never_closed():
    assert fv.is_pressing(ITEMS[0], TODAY)          # high
    assert fv.is_pressing(ITEMS[4], TODAY)          # due in four days
    assert not fv.is_pressing(ITEMS[1], TODAY)      # medium, no deadline
    assert not fv.is_pressing(ITEMS[5], TODAY)      # critical, but done
    assert fv.counts(ITEMS, TODAY) == {"open": 4, "relevant": 2, "pressing": 2, "researching": 1}


def test_every_status_has_a_label_an_icon_and_a_place():
    from proactive.states import DISPLAY, STATUSES, display_status

    assert set(DISPLAY) == set(fv.STATUS_LABELS) == set(fv.STATUS_ICONS) == set(fv.STATUS_ORDER)
    assert set().union(*DISPLAY.values()) == set(STATUSES)  # every stored status has its inbox place
    assert fv.OPEN_STATUSES | fv.CLOSED_STATUSES == set(STATUSES)
    assert {display_status(s) for s in ("new", "surfaced", "held", "snoozed")} == {"open"}
    assert {display_status(s) for s in ("relevant", "interested", "researched")} == {"relevant"}


def test_hints_show_notification_and_research_not_as_status():
    assert fv.notice(_f("h", "held")) [0] == "notifications_paused"
    notified = _f("n", "surfaced")
    notified.surfaced_at = "2026-10-28T10:00:00"
    assert fv.notice(notified)[0] == "notifications_active"
    assert fv.notice(_f("x", "new")) is None
    assert fv.research(_f("r", "interested"))[0] == "travel_explore"
    assert fv.research(_f("r", "researched"))[0] == "fact_check"


def test_overview_one_dot_per_topic_and_no_topic_is_its_own():
    from app_ui import findings_chart

    data = {r["topic"]: r for r in findings_chart.topics(ITEMS, TODAY)}
    assert set(data) == {"kfz", "mobilfunk", "strom", fv.NO_TOPIC}  # "done" is history
    assert (data["kfz"]["calm"], data["kfz"]["act"]) == (1, 1)     # kfz2 medium, kfz1 high
    assert (data["strom"]["calm"], data["strom"]["act"]) == (0, 1)  # due in four days
    opts = findings_chart.options(list(data.values()), "dark",
                                  {"calm": "In view", "act": "Action", "zone": "Action", "none": "No topic"})
    names = {d["name"] for d in opts["series"][0]["data"]}
    assert "No topic" in names or any(n.startswith("No topic") for n in names)


def test_an_outlier_is_cut_to_the_edge_with_its_true_count():
    from app_ui import findings_chart

    data = [{"topic": fv.NO_TOPIC, "label": "", "calm": 42, "act": 2},
            {"topic": "steuer", "label": "Steuer", "calm": 7, "act": 1},
            {"topic": "kfz", "label": "Kfz", "calm": 3, "act": 1}]
    pts, x_max, _y = findings_chart.points(data, "No topic")
    assert x_max < 42
    cut = next(p for p in pts if "No topic" in p["labels"])
    assert cut["cut"] and cut["x"] == x_max and cut["real_x"] == 42


def test_topics_on_the_same_spot_share_one_dot():
    from app_ui import findings_chart

    data = [{"topic": "a", "label": "A", "calm": 1, "act": 0}, {"topic": "b", "label": "B", "calm": 1, "act": 0}]
    (p,), _x, _y = findings_chart.points(data, "No topic")
    assert p["topics"] == ["a", "b"]


def test_no_topic_filter():
    (g,) = fv.arrange(ITEMS, status="open", topic=fv.NO_TOPIC, group="none", sort="relevance", today=TODAY)
    assert [f.finding_id for f in g.items] == ["loose"]
