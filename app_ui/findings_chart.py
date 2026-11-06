"""The findings overview: one row per topic — what is in the inbox, what calls
for action, when the next deadline is.

Only live findings count (open and relevant); done and dismissed are history.
A topic with something pressing (`findings_view.is_pressing`: high or
critical urgency, or a deadline within a week) comes first and says how many;
then the topics by their next deadline, then by size. Findings without a topic
are one row, always last — it is the biggest, and the least telling.

Why rows, not a chart: the question is "which topics are there, how big, where
does it burn" — and every chart answered it by cutting the names. Bars needed
a row per topic and folded the rest into "Other"; a scatter put almost every
topic on two lines; a treemap (canvas, then HTML) broke "Betriebliche
Altersvorsorge" into "Betriebli-che-Altersv…" and left the small pressing
topics as slivers (screenshot of 2026-11-06). A row has room for the whole
name, the count and the date, and it reads on a phone.
"""

from __future__ import annotations

from datetime import date

from app_ui import findings_view as fv

NO_TOPIC = fv.NO_TOPIC
#: Rows shown before "Show all".
TOP_ROWS = 8


def topics(items: list, today: date) -> list[dict]:
    """`[{topic, label, open, act, due}]` for every topic with live findings,
    in display order; findings without a topic are one entry (`NO_TOPIC`,
    label ""), last. `due`: the earliest deadline of its live findings that
    has not passed, ISO, or ""."""
    by: dict[str, dict] = {}
    for f in items:
        if f.status not in fv.OPEN_STATUSES:
            continue
        key = f.topic or NO_TOPIC
        r = by.setdefault(key, {"topic": key, "label": f.topic_display if f.topic else "",
                                "open": 0, "act": 0, "due": ""})
        if f.topic_label:
            r["label"] = f.topic_display
        r["open"] += 1
        if fv.is_pressing(f, today):
            r["act"] += 1
        left = fv.days_left(f, today)
        if left is not None and left >= 0:
            day = str(f.deadline)[:10]
            r["due"] = min(r["due"], day) if r["due"] else day
    return sorted(by.values(), key=lambda r: (
        r["topic"] == NO_TOPIC, -r["act"], r["due"] or "9999", -r["open"], r["label"].casefold()))


def due_label(day: str, today: date) -> str:
    """"11-18" this year and next, the year alone further out ("2064")."""
    if not day:
        return ""
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return ""
    return d.strftime("%m-%d") if (d - today).days <= 300 else str(d.year)


CSS = """
.pb-topics{width:100%;display:flex;flex-direction:column}
.pb-trow{display:flex;align-items:center;gap:10px;padding:7px 2px;border-bottom:1px solid var(--c-border);
  cursor:pointer;min-width:0}
.pb-trow:last-child{border-bottom:0}
.pb-trow:hover{background:var(--c-surface-2)}
.pb-trow .dot{width:9px;height:9px;border-radius:50%;flex-shrink:0;background:transparent}
.pb-trow .name{flex:1;min-width:0;font-size:.86rem;color:var(--c-text);overflow-wrap:anywhere}
.pb-trow .act{font-size:.72rem;font-weight:600;color:var(--c-warn);flex-shrink:0}
.pb-trow .n{width:2.2rem;text-align:right;font-size:.8rem;color:var(--c-text-2);flex-shrink:0;
  font-variant-numeric:tabular-nums}
.pb-trow .due{width:3.2rem;text-align:right;font-size:.75rem;color:var(--c-text-muted);flex-shrink:0;
  font-variant-numeric:tabular-nums}
.pb-trow.is-none .name{color:var(--c-text-2);font-style:italic}
.pb-thead{display:flex;gap:10px;font-size:.68rem;color:var(--c-text-muted);padding:0 2px 2px}
.pb-thead .sp{flex:1}
.pb-thead .n{width:2.2rem;text-align:right}
.pb-thead .due{width:3.2rem;text-align:right}
"""
