"""The findings overview: one dot per topic — how much is in view (x) and how
much calls for action (y).

"Calls for action" is `findings_view.is_pressing`: high or critical urgency,
or a deadline within a week. Only live findings count (open and relevant);
done and dismissed are history. Everything above the line y = ½ is shaded
"action recommended"; a dot on the x axis is a topic with nothing pressing.

Why dots and not bars: a bar chart needs a row per topic, so beyond a handful
it folds topics into "Other" — which on the live archive was 48 findings
against 7 for the largest topic and squeezed every real bar to a sliver. A
dot needs no row. Topics on the same spot share one dot (labelled with the
first name and "+n"); a topic far beyond the rest (the findings without a
topic, typically) sits at the edge of a cut axis with an arrow and its true
count, so it cannot flatten the others.

Colours: one calm and one warm pole (blue / red), checked with the dataviz
validator in both themes. ECharts draws on a canvas, which cannot read CSS
variables, so the hex comes from here, per theme — the same values as the
`--viz-*` tokens in `app_ui/theme.py`.
"""

from __future__ import annotations

import math
from datetime import date

from app_ui import findings_view as fv

NO_TOPIC = fv.NO_TOPIC
COLORS = {
    "light": {"calm": "#2a78d6", "act": "#e34948", "text": "#3d4047", "muted": "#6b6f78", "grid": "#d5d8dd",
              "surface": "#f4f5f7", "zone": "rgba(227,73,72,0.07)"},
    "dark": {"calm": "#3987e5", "act": "#e66767", "text": "#d4d6da", "muted": "#8b8f98", "grid": "#33353b",
             "surface": "#16171a", "zone": "rgba(230,103,103,0.10)"},
}


def topics(items: list, today: date) -> list[dict]:
    """`[{topic, label, calm, act}]` for every topic with live findings;
    findings without a topic are one entry (`NO_TOPIC`, label "")."""
    by: dict[str, dict] = {}
    for f in items:
        if f.status not in fv.OPEN_STATUSES:
            continue
        key = f.topic or NO_TOPIC
        r = by.setdefault(key, {"topic": key, "label": f.topic_display if f.topic else "", "calm": 0, "act": 0})
        if f.topic_label:
            r["label"] = f.topic_display
        r["act" if fv.is_pressing(f, today) else "calm"] += 1
    return sorted(by.values(), key=lambda r: (-r["act"], -r["calm"], r["label"].lower()))


def axis_cap(values: list[int]) -> int:
    """Where to cut an axis: above the rest when the largest value is more than
    twice the next one and at least 5 beyond it; else no cut (the maximum)."""
    vs = sorted(set(values), reverse=True)
    if len(vs) >= 2 and vs[0] > 2 * vs[1] and vs[0] - vs[1] >= 5:
        return math.ceil(vs[1] * 1.4) + 1
    return vs[0] if vs else 1


def points(data: list[dict], other_label: str) -> tuple[list[dict], int, int]:
    """Dots, one per spot: `[{x, y, real_x, real_y, topics, labels, cut}]`
    plus the axis maxima."""
    x_cap = axis_cap([r["calm"] for r in data])
    y_cap = axis_cap([r["act"] for r in data])
    spots: dict[tuple[int, int], dict] = {}
    for r in data:
        x, y = min(r["calm"], x_cap), min(r["act"], y_cap)
        p = spots.setdefault((x, y), {"x": x, "y": y, "real_x": r["calm"], "real_y": r["act"], "topics": [],
                                      "labels": [], "cut": r["calm"] > x_cap or r["act"] > y_cap})
        p["topics"].append(r["topic"])
        p["labels"].append(r["label"] or other_label)
    return list(spots.values()), max(1, x_cap), max(1, y_cap)


def options(data: list[dict], theme: str, labels: dict[str, str]) -> dict:
    """ECharts options. `labels`: translated "calm", "act", "zone", "none"."""
    c = COLORS.get(theme, COLORS["light"])
    pts, x_max, y_max = points(data, labels["none"])

    def item(p: dict) -> dict:
        name = p["labels"][0] + (f" +{len(p['labels']) - 1}" if len(p["labels"]) > 1 else "")
        total = p["real_x"] + p["real_y"]
        cut_x = p["real_x"] > p["x"]
        return {
            "value": [p["x"], p["y"]],
            "name": name,
            "topics": p["topics"],
            "names": p["labels"],
            "real": [p["real_x"], p["real_y"]],
            # a cut dot is an arrow pointing off the axis, its true count beside it
            "symbol": "arrow" if p["cut"] else "circle",
            "symbolRotate": -90 if cut_x else 0,
            "symbolSize": 16 if p["cut"] else round(10 + 4 * math.sqrt(total)),
            "itemStyle": {"color": c["act"] if p["real_y"] else c["calm"],
                          "borderColor": c["surface"], "borderWidth": 2},
            "label": {"show": True, "position": "left" if cut_x else "right", "color": c["text"], "fontSize": 11,
                      "formatter": f"{name} ({p['real_x']} | {p['real_y']})" if p["cut"] else name},
        }

    return {
        "animation": False,
        # room for the axis titles: containLabel leaves them out
        "grid": {"left": 34, "right": 24, "top": 16, "bottom": 34, "containLabel": True},
        "tooltip": {"trigger": "item", "backgroundColor": c["surface"], "borderColor": c["grid"],
                    "textStyle": {"color": c["text"], "fontSize": 12},
                    ":formatter": (
                        "p => p.data.names.map(n => '<b>' + n + '</b>').join('<br>') + '<br>'"
                        f" + '{labels['calm']}: ' + p.data.real[0] + ' · {labels['act']}: ' + p.data.real[1]")},
        "xAxis": {"type": "value", "name": labels["calm"], "nameLocation": "middle", "nameGap": 26,
                  "nameTextStyle": {"color": c["muted"], "fontSize": 11},
                  "min": 0, "max": x_max + 1, "minInterval": 1,
                  "axisLabel": {"color": c["muted"], "fontSize": 11},
                  "splitLine": {"lineStyle": {"color": c["grid"]}}},
        "yAxis": {"type": "value", "name": labels["act"], "nameLocation": "middle", "nameGap": 28,
                  "nameTextStyle": {"color": c["muted"], "fontSize": 11},
                  "min": 0, "max": y_max + 1, "minInterval": 1,
                  "axisLabel": {"color": c["muted"], "fontSize": 11},
                  "splitLine": {"lineStyle": {"color": c["grid"]}}},
        "series": [{
            "type": "scatter",
            "data": [item(p) for p in pts],
            "labelLayout": {"hideOverlap": True},
            "emphasis": {"scale": 1.2, "label": {"show": True}},
            "cursor": "pointer",
            # the threshold: above y = ½ every dot has something pressing
            "markArea": {"silent": True, "itemStyle": {"color": c["zone"]},
                         "label": {"color": c["muted"], "fontSize": 11, "position": "insideTopLeft"},
                         "data": [[{"name": labels["zone"], "yAxis": 0.5}, {"yAxis": y_max + 1}]]},
        }],
    }


HEIGHT_PX = 300
