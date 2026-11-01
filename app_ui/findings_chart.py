"""The findings overview: a treemap of topics — one tile per topic, its area
the number of live findings, its name and count inside.

Only live findings count (open and relevant); done and dismissed are history.
Tiles share one colour unless the user gave the topic one (`app_ui/topic_colors`
— the same colour as its cards' outline, as a tint); a topic with something that calls for action
(`findings_view.is_pressing`: high or critical urgency, or a deadline within a
week) gets a red outline and its count of those as a third line.

Why a treemap: the question is "which topics are there, how big, where does
it burn". Bars needed a row per topic and folded the rest into "Other" (48
against 7, every real bar a sliver); a scatter of in-view against pressing
put almost every topic on two lines, since a topic rarely has more than one
pressing finding. A tile needs no axis: the 42 findings without a topic are a
big tile, not a scale that flattens the others.

Why HTML, not ECharts: on a canvas a label cannot wrap like text. zrender
broke "Betriebliche-Altersvorsorge" mid-word ("Altersvorso|rge"), ignored a
single line break inside rich text, and a tile too small for any name showed
"St", "Re" or nothing (screenshot of 2026-11-01). Here the layout is a small
squarified treemap in the browser (`TREEMAP_JS`), run again when the width
changes, and the text is ordinary DOM text: it wraps at hyphens and spaces,
hyphenates long words, and is clipped by its tile.

Small topics are folded in the browser, where the real size is known: a topic
whose tile would be smaller than `MIN_TILE_PX` square pixels — or comes out
narrower than 56 px or lower than 40 — joins one "N more topics" column on the
right, which lists them on tap. A folded pressing topic still
shows — the "more" tile carries the red outline and its count.
"""

from __future__ import annotations

import json
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
#: About 72 × 50 px: room for a two-line name and the count.
MIN_TILE_PX = 3600
MAX_TILES = 12


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


def tiles(data: list[dict], theme: str, labels: dict[str, str], colors: dict[str, int] | None = None) -> dict:
    """What the browser lays out: one entry per topic, largest first, plus the
    translated texts and the theme colours. `labels`: "none" (findings without
    a topic), "pressing" ("{n} pressing") and "more" ("{n} more topics").
    `colors`: the user's topic colours (`app_ui/topic_colors`, topic → slot);
    a topic without one keeps the common tile colour. Names are model text —
    the script sets them as text, never as HTML."""
    from app_ui.topic_colors import tint

    c = COLORS.get(theme, COLORS["light"])
    colors = colors or {}
    out = []
    for r in sorted(data, key=lambda r: (-(r["calm"] + r["act"]), -r["act"], r["label"].lower())):
        out.append({
            "key": r["topic"],
            "name": r["label"] or labels["none"],
            "value": r["calm"] + r["act"],
            "act": r["act"],
            "bg": tint(colors[r["topic"]], theme, c["surface"], TINT.get(theme, 0.4))
            if r["topic"] in colors else c["tile"],
        })
    return {
        "tiles": out,
        "pressing": labels["pressing"],
        "more": labels.get("more", "{n} more topics"),
        "colors": c,
        "minArea": MIN_TILE_PX,
        "maxTiles": MAX_TILES,
    }


def payload(spec: dict) -> str:
    """`tiles()` as the element's `data-tm` attribute."""
    return json.dumps(spec, ensure_ascii=False)


TREEMAP_CSS = """
.pb-treemap{position:relative;width:100%;overflow:hidden}
.pb-tm-tile{position:absolute;box-sizing:border-box;border-radius:4px;padding:5px 7px;overflow:hidden;
  cursor:pointer;line-height:1.25;font-size:12px;border:1px solid transparent}
.pb-tm-tile:hover{filter:brightness(.96)}
.pb-tm-name{font-weight:600;overflow-wrap:anywhere;hyphens:auto;display:-webkit-box;
  -webkit-box-orient:vertical;-webkit-line-clamp:3;overflow:hidden}
.pb-tm-act{font-size:11px;font-weight:600}
.pb-tm-more{border-style:dashed !important}
"""

#: Squarified treemap (Bruls, Huizing, van Wijk) + fold, re-run on resize.
#: Self-initialising: every `.pb-treemap[data-tm]` that appears is laid out,
#: also after the list re-renders. A tap dispatches `pb-pick` on the element.
TREEMAP_JS = r"""
(() => {
  if (window.__pbTreemap) return;
  window.__pbTreemap = true;
  const GAP = 2;
  function worst(row, len) {
    const s = row.reduce((a, r) => a + r.a, 0);
    let mx = 0, mn = Infinity;
    for (const r of row) { mx = Math.max(mx, r.a); mn = Math.min(mn, r.a); }
    return Math.max(len * len * mx / (s * s), (s * s) / (len * len * mn));
  }
  function place(row, box, out) {
    const s = row.reduce((a, r) => a + r.a, 0);
    if (box.w >= box.h) {
      const cw = s / box.h; let y = box.y;
      for (const r of row) { const h = r.a / cw; out.push({...r, x: box.x, y, w: cw, h}); y += h; }
      return {x: box.x + cw, y: box.y, w: box.w - cw, h: box.h};
    }
    const rh = s / box.w; let x = box.x;
    for (const r of row) { const w = r.a / rh; out.push({...r, x, y: box.y, w, h: rh}); x += w; }
    return {x: box.x, y: box.y + rh, w: box.w, h: box.h - rh};
  }
  function squarify(items, box) {
    const out = []; let row = []; let i = 0;
    while (i < items.length) {
      const len = Math.min(box.w, box.h);
      if (!row.length || worst([...row, items[i]], len) <= worst(row, len)) { row.push(items[i]); i++; }
      else { box = place(row, box, out); row = []; }
    }
    if (row.length) place(row, box, out);
    return out;
  }
  function moreTile(spec, rest) {
    return {
      name: spec.more.replace('{n}', rest.length), more: true,
      value: rest.reduce((a, t) => a + t.value, 0), act: rest.reduce((a, t) => a + t.act, 0),
      bg: spec.colors.tile, keys: rest.map(t => t.key), names: rest.map(t => t.name + ' (' + t.value + ')'),
    };
  }
  // The topics with a tile of their own squarified, the folded rest one
  // full-height column on the right. Fold more while any tile is too narrow
  // or too low for its name.
  function layout(spec, W, H) {
    const all = spec.tiles.map(t => ({...t, keys: [t.key], names: [t.name]}));
    const total = all.reduce((a, t) => a + t.value, 0) || 1;
    let n = all.filter((t, i) => i < spec.maxTiles && t.value / total * W * H >= spec.minArea).length;
    if (all.length - n <= 1) n = all.length;
    for (; n >= 1; n--) {
      const keep = all.slice(0, n), rest = all.slice(n);
      const more = rest.length ? moreTile(spec, rest) : null;
      const mw = more ? Math.max(more.value / total * W, Math.min(96, W / 3)) : 0;
      const kt = keep.reduce((a, t) => a + t.value, 0) || 1;
      const rects = squarify(keep.map(t => ({...t, a: t.value / kt * (W - mw) * H})),
                             {x: 0, y: 0, w: W - mw, h: H});
      const ok = rects.every(r => r.w >= 56 && r.h >= 40);
      if (ok || n === 1) {
        if (more) rects.push({...more, x: W - mw, y: 0, w: mw, h: H});
        return rects;
      }
      if (n === all.length) n = all.length - 1;  // next round folds two: one alone is no "more"
    }
    return [];
  }
  function render(el) {
    let spec; try { spec = JSON.parse(el.dataset.tm); } catch (e) { return; }
    const W = el.clientWidth, H = el.clientHeight;
    if (!W || !H) return;
    const rects = layout(spec, W, H);
    el.replaceChildren();
    const c = spec.colors;
    for (const r of rects) {
      const d = document.createElement('div');
      d.className = 'pb-tm-tile' + (r.more ? ' pb-tm-more' : '');
      d.style.cssText = `left:${r.x + GAP / 2}px;top:${r.y + GAP / 2}px;width:${Math.max(r.w - GAP, 0)}px;`
        + `height:${Math.max(r.h - GAP, 0)}px;background:${r.bg};color:${c.text};`
        + (r.act ? `border:3px solid ${c.act};padding:3px 5px;` : (r.more ? `border-color:${c.text}55;` : ''));
      const name = document.createElement('div'); name.className = 'pb-tm-name'; name.textContent = r.name;
      const count = document.createElement('div'); count.textContent = r.value;
      d.append(name, count);
      if (r.act) {
        const a = document.createElement('div'); a.className = 'pb-tm-act'; a.style.color = c.act;
        a.textContent = spec.pressing.replace('{n}', r.act); d.append(a);
      }
      el.append(d);
      // too little height: fewer name lines first, then no count line
      for (let lines = 2; d.scrollHeight > d.clientHeight + 1 && lines >= 1; lines--) {
        name.style.webkitLineClamp = lines;
      }
      if (d.scrollHeight > d.clientHeight + 1) count.remove();
      d.title = r.names.join('\n');
      d.addEventListener('click', () => el.dispatchEvent(
        new CustomEvent('pb-pick', {detail: {keys: r.keys, names: r.names}})));
    }
  }
  const sized = new ResizeObserver(es => es.forEach(e => render(e.target)));
  function scan(root) {
    (root.querySelectorAll ? root.querySelectorAll('.pb-treemap[data-tm]') : []).forEach(el => {
      if (el.__pbTm === el.dataset.tm) return;
      el.__pbTm = el.dataset.tm; sized.observe(el); render(el);
    });
  }
  new MutationObserver(ms => ms.forEach(m => {
    m.addedNodes.forEach(n => { if (n.nodeType === 1) { scan(n.parentNode || n); } });
    if (m.type === 'attributes') scan(m.target.parentNode || document);
  })).observe(document.documentElement, {childList: true, subtree: true, attributes: true,
                                          attributeFilter: ['data-tm']});
  document.addEventListener('DOMContentLoaded', () => scan(document));
  scan(document);
})();
"""
