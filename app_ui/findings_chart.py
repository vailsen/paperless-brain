"""The findings overview: per topic, what is in view and what calls for action.

A diverging bar per topic, centred on the threshold "action recommended":
to the left the findings that wait (low or medium urgency, no deadline within
a week), to the right those that press (`findings_view.is_pressing` — high or
critical, or due within a week). Only live findings (open and relevant);
done and dismissed are history. Topics sort by how much presses, the rest
fold into "Other" so the chart stays a glance.

Colours: one calm and one warm pole (blue / red), checked with the dataviz
validator in both themes. ECharts draws on a canvas, which cannot read CSS
variables, so the hex comes from here, per theme — the same values as the
`--viz-*` tokens in `app_ui/theme.py`.
"""

from __future__ import annotations

from datetime import date

from app_ui import findings_view as fv

MAX_TOPICS = 8
COLORS = {
    "light": {"calm": "#2a78d6", "act": "#e34948", "text": "#3d4047", "muted": "#6b6f78", "grid": "#d5d8dd",
              "surface": "#f4f5f7"},
    "dark": {"calm": "#3987e5", "act": "#e66767", "text": "#d4d6da", "muted": "#8b8f98", "grid": "#33353b",
             "surface": "#16171a"},
}


def rows(items: list, today: date) -> list[dict]:
    """`[{topic, label, calm, act, urgency: {level: n}}]`, most pressing first,
    at most `MAX_TOPICS` plus one "Other" row (topic "")."""
    by: dict[str, dict] = {}
    for f in items:
        if f.status not in fv.OPEN_STATUSES:
            continue
        key = f.topic or ""
        r = by.setdefault(key, {"topic": key, "label": f.topic_display if key else "", "calm": 0, "act": 0,
                                "urgency": {}})
        if f.topic_label:
            r["label"] = f.topic_display
        r["act" if fv.is_pressing(f, today) else "calm"] += 1
        r["urgency"][f.urgency] = r["urgency"].get(f.urgency, 0) + 1
    ordered = sorted(by.values(), key=lambda r: (r["topic"] == "", -r["act"], -(r["act"] + r["calm"]),
                                                 r["label"].lower()))
    if len(ordered) <= MAX_TOPICS:
        return ordered
    head, tail = ordered[:MAX_TOPICS], ordered[MAX_TOPICS:]
    other = {"topic": "", "label": "", "calm": 0, "act": 0, "urgency": {}}
    for r in tail:
        other["calm"] += r["calm"]
        other["act"] += r["act"]
        for k, v in r["urgency"].items():
            other["urgency"][k] = other["urgency"].get(k, 0) + v
    return head + [other]


def options(data: list[dict], theme: str, labels: dict[str, str]) -> dict:
    """ECharts options. `labels`: translated "calm", "act", "other"."""
    c = COLORS.get(theme, COLORS["light"])
    names = [r["label"] or labels["other"] for r in data][::-1]  # top row = first
    calm = [-r["calm"] for r in data][::-1]
    act = [r["act"] for r in data][::-1]
    peak = max([1] + [r["calm"] for r in data] + [r["act"] for r in data])

    def series(name: str, values: list[int], color: str, radius: list[int], position: str) -> dict:
        return {
            "name": name, "type": "bar", "stack": "all", "data": values, "barWidth": 12,
            "itemStyle": {"color": color, "borderRadius": radius},
            "label": {"show": True, "position": position, "color": c["text"], "fontSize": 11,
                      ":formatter": "p => p.value ? Math.abs(p.value) : ''"},
            "emphasis": {"focus": "series"},
        }

    return {
        "animation": False,
        "grid": {"left": 8, "right": 28, "top": 34, "bottom": 8, "containLabel": True},
        "legend": {"top": 0, "left": 0, "itemWidth": 12, "itemHeight": 8,
                   "textStyle": {"color": c["text"], "fontSize": 12}, "icon": "roundRect"},
        "tooltip": {"trigger": "axis", "axisPointer": {"type": "shadow"}, "backgroundColor": c["surface"],
                    "borderColor": c["grid"], "textStyle": {"color": c["text"], "fontSize": 12},
                    ":valueFormatter": "v => Math.abs(v)"},
        "xAxis": {"type": "value", "min": -peak, "max": peak, "minInterval": 1,
                  "axisLabel": {"color": c["muted"], "fontSize": 11, ":formatter": "v => Math.abs(v)"},
                  "splitLine": {"lineStyle": {"color": c["grid"]}}},
        "yAxis": {"type": "category", "data": names, "axisTick": {"show": False},
                  "axisLine": {"lineStyle": {"color": c["muted"]}},
                  "axisLabel": {"color": c["text"], "fontSize": 12, "width": 120, "overflow": "truncate"}},
        "series": [
            series(labels["calm"], calm, c["calm"], [4, 0, 0, 4], "left"),
            series(labels["act"], act, c["act"], [0, 4, 4, 0], "right"),
        ],
    }


def height_px(data: list[dict]) -> int:
    return 52 + 30 * max(1, len(data))
