"""Faceted filtering of the document list, in memory (the browser, Phase 3 of
docs/ux-overhaul-plan.md).

The list is the user's whole catalogue (`PaperlessClient.list_catalogue`,
metadata only, their own token); every click filters it here, which is what
makes the browser live without a round trip per click.

Semantics are those of an expert scope (`services/expert_scope.py`), so the
user learns them once: values within one facet OR (tags may be switched to
AND), facets AND, and an excluded value takes documents out whatever else
matches. Counts are *disjunctive*: a facet's counts ignore that facet's own
selection, so picking one tag still shows what the others would add.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

#: Facets over Paperless data, in panel order.
DATA_FACETS = ("tags", "correspondent", "document_type", "owner", "created")
#: What PaperlessBrain knows about a document — never mixed into the tags.
FLAG_FACET = "flags"
ADDED_FACET = "added"
ADDED_PRESETS = ("7d", "30d", "365d")
NONE = ""  # the value of a document without a correspondent / type / owner


@dataclass
class Filters:
    #: facet → {value: +1 include | -1 exclude}
    sel: dict[str, dict[str, int]] = field(default_factory=dict)
    tags_all: bool = False
    added: str = ""  # one of ADDED_PRESETS, or ""
    text: str = ""  # quick filter on title / correspondent / type / #id

    def state(self, facet: str, value: str) -> int:
        return self.sel.get(facet, {}).get(value, 0)

    def cycle(self, facet: str, value: str) -> None:
        """none → include → exclude → none."""
        cur = self.state(facet, value)
        nxt = {0: 1, 1: -1, -1: 0}[cur]
        vals = self.sel.setdefault(facet, {})
        if nxt:
            vals[value] = nxt
        else:
            vals.pop(value, None)
            if not vals:
                self.sel.pop(facet, None)
        # A month only narrows its year: choosing one drops the year itself.
        if facet == "created" and nxt and len(value) == 7:
            vals.pop(value[:4], None)

    def active(self) -> int:
        """How many choices are set — for the "Filters (n)" button."""
        return sum(len(v) for v in self.sel.values()) + (1 if self.added else 0) + (1 if self.text.strip() else 0)

    def clear(self) -> None:
        self.sel.clear()
        self.added = ""
        self.text = ""
        self.tags_all = False


def values_of(doc, facet: str, flags: dict[int, set[str]] | None = None) -> list[str]:
    """The values a document has in a facet."""
    if facet == "tags":
        return list(doc.tags or []) or [NONE]  # "(none)": documents without tags
    if facet == "correspondent":
        return [doc.correspondent or NONE]
    if facet == "document_type":
        return [doc.document_type or NONE]
    if facet == "owner":
        return [doc.owner_name or NONE]
    if facet == "created":
        c = _date(doc.created)
        return [f"{c:%Y}", f"{c:%Y-%m}"] if c else []
    if facet == FLAG_FACET:
        return sorted((flags or {}).get(doc.id, set()))
    return []


def _date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return None


def _facet_ok(doc, facet: str, chosen: dict[str, int], tags_all: bool, flags) -> bool:
    have = set(values_of(doc, facet, flags))
    inc = {v for v, s in chosen.items() if s > 0}
    exc = {v for v, s in chosen.items() if s < 0}
    if have & exc:
        return False
    if not inc:
        return True
    if facet == "tags" and tags_all:
        return inc <= have
    return bool(have & inc)


def _text_ok(doc, text: str) -> bool:
    t = text.strip().lower()
    if not t:
        return True
    if t.startswith("#") and t[1:].isdigit():
        return doc.id == int(t[1:])
    hay = " ".join(str(x or "") for x in (doc.title, doc.correspondent, doc.document_type, doc.id)).lower()
    return all(w in hay for w in t.split())


def _added_ok(doc, preset: str, today: date) -> bool:
    if not preset:
        return True
    days = {"7d": 7, "30d": 30, "365d": 365}.get(preset)
    a = _date(doc.added)
    return days is None or (a is not None and a >= today - timedelta(days=days))


def matches(doc, f: Filters, *, skip: str | None = None, flags=None, today: date | None = None) -> bool:
    today = today or date.today()
    for facet, chosen in f.sel.items():
        if facet == skip or not chosen:
            continue
        if not _facet_ok(doc, facet, chosen, f.tags_all, flags):
            return False
    if skip != ADDED_FACET and not _added_ok(doc, f.added, today):
        return False
    return _text_ok(doc, f.text)


def apply(docs: list, f: Filters, *, flags=None, today: date | None = None) -> list:
    return [d for d in docs if matches(d, f, flags=flags, today=today)]


def counts(docs: list, f: Filters, *, flags=None, today: date | None = None) -> dict[str, Counter]:
    """facet → Counter(value → documents), each facet counted with every
    filter applied except its own."""
    out: dict[str, Counter] = {}
    for facet in (*DATA_FACETS, FLAG_FACET):
        c: Counter = Counter()
        for d in docs:
            if matches(d, f, skip=facet, flags=flags, today=today):
                c.update(set(values_of(d, facet, flags)))
        out[facet] = c
    added: Counter = Counter()
    for d in docs:
        if matches(d, f, skip=ADDED_FACET, flags=flags, today=today):
            for p in ADDED_PRESETS:
                if _added_ok(d, p, today or date.today()):
                    added[p] += 1
    out[ADDED_FACET] = added
    return out


# ── The address of a view ────────────────────────────────────────────────────

_KEYS = {"tags": "t", "correspondent": "c", "document_type": "d", "owner": "o", "created": "y", FLAG_FACET: "f"}


def to_query(f: Filters, *, mode: str = "", q: str = "", view: str = "", sort: str = "") -> list[tuple[str, str]]:
    """Query parameters for `history.replaceState`: a filtered view can be
    bookmarked and is restored on reload. Excluded values carry a leading '!'."""
    out: list[tuple[str, str]] = []
    for facet, key in _KEYS.items():
        for v, s in sorted(f.sel.get(facet, {}).items()):
            out.append((key, ("!" if s < 0 else "") + (v or "~")))
    if f.tags_all:
        out.append(("tall", "1"))
    if f.added:
        out.append(("a", f.added))
    if f.text.strip():
        out.append(("x", f.text.strip()))
    for k, v in (("m", mode), ("q", q), ("v", view), ("s", sort)):
        if v:
            out.append((k, v))
    return out


def from_query(params) -> tuple[Filters, dict[str, str]]:
    """`params`: a mapping with `getlist` (Starlette's QueryParams)."""
    f = Filters()
    for facet, key in _KEYS.items():
        for raw in params.getlist(key):
            neg = raw.startswith("!")
            v = raw[1:] if neg else raw
            f.sel.setdefault(facet, {})["" if v == "~" else v] = -1 if neg else 1
    f.tags_all = params.get("tall") == "1"
    f.added = params.get("a") if params.get("a") in ADDED_PRESETS else ""
    f.text = params.get("x") or ""
    extra = {k: params.get(k) or "" for k in ("m", "q", "v", "s")}
    return f, extra
