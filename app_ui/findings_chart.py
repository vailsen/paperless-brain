"""The findings overview: a treemap of topics — one tile per topic, its area
the number of live findings, its name and count inside.

Only live findings count (open and relevant); done and dismissed are history.
Tiles share one colour unless the user gave the topic one (`app_ui/topic_colors`
— the same colour as its cards' outline, as a tint); a topic with something that calls for action
(`findings_view.is_pressing`: high or critical urgency, or a deadline within a
week) gets a red outline and its count of those as a second line.

Why a treemap: the question is "which topics are there, how big, where does
it burn". Bars needed a row per topic and folded the rest into "Other" (48
against 7, every real bar a sliver); a scatter of in-view against pressing
put almost every topic on two lines, since a topic rarely has more than one
pressing finding. A tile needs no axis: the 42 findings without a topic are a
big tile, not a scale that flattens the others.

Colours: one calm tile colour and one warm outline (blue / red), checked with
the dataviz validator in both themes. ECharts draws on a canvas, which cannot
read CSS variables, so the hex comes from here, per theme.
"""

from __future__ import annotations

import html
from datetime import date

from app_ui import findings_view as fv

NO_TOPIC = fv.NO_TOPIC
COLORS = {
    # the common tile is neutral: blue was the first topic colour, and an
    # uncoloured topic read as a coloured one
    "light": {"tile": "#e1e4e8", "text": "#1f2329", "act": "#e34948", "surface": "#f4f5f7"},
    "dark": {"tile": "#2b2e34", "text": "#eef0f3", "act": "#e66767", "surface": "#16171a"},
}
HEIGHT_PX = 260


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


#: How much of a topic's colour goes into its tile — enough to tell topics
#: apart, little enough that the text and the red outline stay readable.
TINT = {"light": 0.38, "dark": 0.55}


def options(data: list[dict], theme: str, labels: dict[str, str], colors: dict[str, int] | None = None) -> dict:
    """ECharts options. `labels`: translated "none" (findings without a topic)
    and "pressing" ("{n} pressing"). `colors`: the user's topic colours
    (`app_ui/topic_colors`, topic → slot); a topic without one keeps the
    common tile colour."""
    from app_ui.topic_colors import tint

    c = COLORS.get(theme, COLORS["light"])
    colors = colors or {}
    items = []
    for r in data:
        total = r["calm"] + r["act"]
        name = r["label"] or labels["none"]
        pressing = labels["pressing"].format(n=r["act"]) if r["act"] else ""
        lines = f"{{name|{name}}}\n{{count|{total}}}" + (f"\n{{act|{pressing}}}" if pressing else "")
        # the tooltip is HTML: a topic is model text
        tip = f"<b>{html.escape(name)}</b><br>{total}" + (f" · {html.escape(pressing)}" if pressing else "")
        items.append({
            "name": name, "value": total, "topics": [r["topic"]], "names": [name], "tip": tip,
            "itemStyle": {"color": tint(colors[r["topic"]], theme, c["surface"], TINT.get(theme, 0.4))
                          if r["topic"] in colors else c["tile"],
                          "borderColor": c["act"] if r["act"] else c["surface"],
                          "borderWidth": 3 if r["act"] else 1, "borderRadius": 4},
            "label": {"formatter": lines},
        })
    return {
        "animation": False,
        "tooltip": {"backgroundColor": c["surface"], "textStyle": {"color": c["text"], "fontSize": 12},
                    ":formatter": "p => p.data.tip"},
        "series": [{
            "type": "treemap",
            "data": items,
            "roam": False,
            "nodeClick": False,
            "breadcrumb": {"show": False},
            "width": "100%", "height": "100%", "top": 0, "left": 0, "right": 0, "bottom": 0,
            "itemStyle": {"gapWidth": 2, "borderColor": c["surface"]},
            "label": {"show": True, "position": "insideTopLeft", "overflow": "truncate",
                      "rich": {"name": {"color": c["text"], "fontSize": 12, "fontWeight": 600, "lineHeight": 16},
                               "count": {"color": c["text"], "fontSize": 12, "lineHeight": 16},
                               "act": {"color": c["act"], "fontSize": 11, "fontWeight": 600, "lineHeight": 15}}},
            "upperLabel": {"show": False},
            "emphasis": {"itemStyle": {"borderColor": c["text"]}},
            "cursor": "pointer",
        }],
    }
