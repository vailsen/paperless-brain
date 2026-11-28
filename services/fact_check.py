"""Hard facts in a chat answer, checked against the sources it cites — in code.

A "hard fact" is what a reader acts on and a model gets wrong without noticing:
an amount, a date, an id (IBAN, invoice or file number, contract number). Each
one in an answer is looked up on the pages of the documents the answer rests on;
found, it gets a small check mark that opens the page with the fact marked.

No model is asked. The comparison forgives what differs between how a document
writes a fact and how an answer repeats it, and nothing else:

- **spaces and separators** inside an id: "DE89 3704 0044" = "de8937040044",
  "VR 1000631423" = "VR1000631423", "R-2026-0312" = "R 2026/0312";
- **case**: "ab-12c" = "AB-12C"; an id's letter O / digit 0 and l, I / 1 (OCR);
- **number formats**: "1.240,00" = "1,240.00" = "1240" = "1 240,00 €" — a number
  is compared by value, and an ambiguous one ("1.240") by both readings;
- **date formats**: "14.03.2026" = "2026-03-14" = "14. März 2026" = "March 14, 2026".

Not a hard fact, never marked: one- and two-digit numbers, a bare year, page
numbers and reference markers, anything inside a link target or code. A fact
that is not found gets no mark — the absence of a check is not an accusation;
the vision model's text of a page can miss what the page shows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

# ── Finding facts in text ────────────────────────────────────────────────────

_GROUP_RE = re.compile(r"[^\W_]+")
#: One of these between two groups joins them into one fact ("1.240,00", "R-12/3").
_JOIN = set(".,/:-'’")
_SPACES = set("    ")

_MONTHS = {
    # German
    "januar": 1, "jänner": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5, "juni": 6,
    "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12,
    # English
    "january": 1, "february": 2, "march": 3, "may": 5, "june": 6, "july": 7,
    "october": 10, "december": 12,
    # Short forms (with or without a dot)
    "jan": 1, "feb": 2, "mär": 3, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9,
    "sept": 9, "okt": 10, "oct": 10, "nov": 11, "dez": 12, "dec": 12,
}
_MONTH_ALT = "|".join(sorted(map(re.escape, _MONTHS), key=len, reverse=True))
# "14. März 2026", "14 March 2026", "March 14, 2026"
_WORD_DATE_RE = re.compile(
    rf"(?<![\w])(?:(\d{{1,2}})\.?\s+({_MONTH_ALT})\.?\s+(\d{{4}})|({_MONTH_ALT})\.?\s+(\d{{1,2}}),?\s+(\d{{4}}))(?![\w])",
    re.I,
)
# Where nothing is a fact: reference markers, links, code, HTML, image/link targets.
_MASK_RE = re.compile(
    r"\[\^[^\]]*\]"            # [^241-p3], [^Site]
    r"|\]\([^)]*\)"            # ](target)
    r"|https?://\S+"
    r"|`[^`]*`"
    r"|<[^>]*>"
    r"|(?<![\w&])#\d+"         # #241 — a document id
    r"|\b(?:p|S|Seite|page|pp)\.?\s*\d+(?:\s*[-–]\s*\d+)?\b",  # page numbers
    re.I,
)


@dataclass(frozen=True)
class Fact:
    start: int
    end: int
    text: str
    kind: str  # "date" | "number" | "code"


def _runs(text: str) -> list[tuple[int, int]]:
    """Spans of digit-bearing runs: groups of letters/digits joined by one
    separator ("1.240,00", "R-2026-0312"), or by one space where an id is
    written in blocks ("DE89 3704 0044", "VR 1000631423")."""
    groups = [(m.start(), m.end()) for m in _GROUP_RE.finditer(text)]
    out: list[tuple[int, int]] = []
    i = 0
    while i < len(groups):
        s, e = groups[i]
        j = i
        while j + 1 < len(groups):
            ns, ne = groups[j + 1]
            gap = text[e:ns]
            cur, nxt = text[groups[j][0]:e], text[ns:ne]
            if len(gap) == 1 and gap in _JOIN and (any(c.isdigit() for c in cur) or any(c.isdigit() for c in nxt)):
                pass
            elif (len(gap) == 1 and gap in _SPACES and any(c.isdigit() for c in nxt)
                  and (_block(cur) and _block(nxt) or (cur.isalpha() and cur.isupper() and len(cur) <= 4
                                                        and j == i and len(nxt) >= 4))):
                pass
            else:
                break
            j += 1
            e = ne
        if any(c.isdigit() for c in text[s:e]):
            out.append((s, e))
        i = j + 1
    return out


def _block(g: str) -> bool:
    """A block of an id written in groups: 2–5 characters with a digit,
    or a letter prefix like "DE89"."""
    return 2 <= len(g) <= 5 and any(c.isdigit() for c in g)


def _number_readings(s: str) -> set[Decimal]:
    """The values a written number can mean. "1.240" is 1240 in German and
    1.24 in English — both are kept; "1.240,00" and "1,240.00" are one."""
    t = s.replace("'", "").replace("’", "")
    t = re.sub(r"[\s   ]", "", t)
    if not re.fullmatch(r"\d+(?:[.,]\d+)*", t):
        return set()
    dots, commas = t.count("."), t.count(",")
    cands: list[str] = []
    if not dots and not commas:
        cands = [t]
    elif dots and commas:
        dec = "." if t.rfind(".") > t.rfind(",") else ","
        th = "," if dec == "." else "."
        cands = [t.replace(th, "").replace(dec, ".")]
    else:
        sep = "." if dots else ","
        parts = t.split(sep)
        if len(parts) > 2:                      # 12.345.678 — thousands only
            cands = ["".join(parts)]
        else:
            whole, frac = parts
            cands = [f"{whole}.{frac}"]         # a decimal mark
            if len(frac) == 3:                  # or a thousands mark
                cands.append(whole + frac)
    out: set[Decimal] = set()
    for c in cands:
        try:
            out.add(Decimal(c).normalize())
        except InvalidOperation:
            pass
    return out


def _date_readings(s: str) -> set[str]:
    """ISO dates a numeric date can mean ("14.03.2026", "2026-03-14",
    "03/14/2026" — d/m and m/d both kept when both are valid)."""
    m = re.fullmatch(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", s)
    if m:
        return _iso(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r"(\d{1,2})([./-])(\d{1,2})\2(\d{2}|\d{4})", s)
    if not m:
        return set()
    a, b, y = int(m.group(1)), int(m.group(3)), int(m.group(4))
    if y < 100:
        y += 2000 if y < 70 else 1900
    out = _iso(y, b, a)
    if m.group(2) == "/":
        out |= _iso(y, a, b)
    return out


def _iso(y: int, mo: int, d: int) -> set[str]:
    try:
        return {date(y, mo, d).isoformat()}
    except ValueError:
        return set()


def _kind(s: str) -> str:
    if _date_readings(s):
        return "date"
    if _number_readings(s):
        return "number"
    return "code"


def _compact(s: str) -> str:
    return "".join(c for c in s.lower() if c.isalnum())


def is_hard(text: str, kind: str) -> bool:
    """Worth a check mark: a full date, a number with a decimal part or three
    digits and more (not a bare year), an id of four characters and more."""
    if kind == "date":
        return True
    digits = sum(c.isdigit() for c in text)
    if kind == "number":
        if re.fullmatch(r"(19|20)\d\d", text):
            return False
        return digits >= 3 or bool(re.search(r"\d[.,]\d", text))
    return len(_compact(text)) >= 4 and digits >= 2


def facts(text: str, *, hard_only: bool = True) -> list[Fact]:
    """The facts in ``text``, in order — word dates first claimed, then runs."""
    text = text or ""
    masked = [(m.start(), m.end()) for m in _MASK_RE.finditer(text)]

    def free(s: int, e: int) -> bool:
        return not any(s < me and ms < e for ms, me in masked)

    out: list[Fact] = []
    taken: list[tuple[int, int]] = []
    for m in _WORD_DATE_RE.finditer(text):
        if free(m.start(), m.end()):
            out.append(Fact(m.start(), m.end(), m.group(0), "date"))
            taken.append((m.start(), m.end()))
    for s, e in _runs(text):
        if not free(s, e) or any(s < te and ts < e for ts, te in taken):
            continue
        raw = text[s:e]
        k = _kind(raw)
        if hard_only and not is_hard(raw, k):
            continue
        out.append(Fact(s, e, raw, k))
    out.sort(key=lambda f: f.start)
    return out


def _word_date(raw: str) -> set[str]:
    m = _WORD_DATE_RE.fullmatch(raw.strip())
    if not m:
        return set()
    if m.group(1):
        d, mo, y = int(m.group(1)), _MONTHS.get(m.group(2).lower().rstrip(".")), int(m.group(3))
    else:
        mo, d, y = _MONTHS.get(m.group(4).lower().rstrip(".")), int(m.group(5)), int(m.group(6))
    return _iso(y, mo, d) if mo else set()


def readings(f: Fact) -> set:
    if f.kind == "date":
        return _word_date(f.text) or _date_readings(f.text)
    if f.kind == "number":
        return _number_readings(f.text)
    return {_compact(f.text)}


# ── Finding a fact on a page ─────────────────────────────────────────────────

_OCR = {"0": "[0o]", "o": "[0o]", "1": "[1il]", "i": "[1il]", "l": "[1il]"}
_SEP_CLASS = r"[\s   .,/:\-'’]*"


def _code_pattern(compact: str) -> re.Pattern:
    """Case-insensitive, any separator between two characters, and not part
    of a longer id on either side."""
    body = _SEP_CLASS.join(_OCR.get(c, re.escape(c)) for c in compact)
    return re.compile(rf"(?<![^\W_]){body}(?![^\W_])", re.I)


def find(fact: Fact, page_text: str) -> list[tuple[int, int]]:
    """Where ``fact`` stands in ``page_text`` (spans), however it is written there."""
    page_text = page_text or ""
    want = readings(fact)
    if not want:
        return []
    spans: list[tuple[int, int]] = []
    if fact.kind == "date":
        for f in facts(page_text, hard_only=False):
            if (f.kind == "date" or _date_readings(f.text)) and readings(Fact(0, 0, f.text, "date")) & want:
                spans.append((f.start, f.end))
        return spans
    if fact.kind == "number":
        for f in facts(page_text, hard_only=False):
            if f.kind == "number" and _number_readings(f.text) & want:
                spans.append((f.start, f.end))
        if spans:
            return spans
        # A long number inside an id on the page ("4711" in "4711-0815",
        # "1000631423" in "VR 1000631423") — whole, never a piece of a number.
        digits = _compact(fact.text)
        if len(digits) >= 4 and digits.isdigit():
            return [m.span() for m in _code_pattern(digits).finditer(page_text)]
        return []
    compact = next(iter(want))
    return [m.span() for m in _code_pattern(compact).finditer(page_text)] if len(compact) >= 3 else []


# ── An answer against its sources ────────────────────────────────────────────

_CITE_RE = re.compile(r"\[\^\s*#?(\d+)(?:\s*-\s*p(\d+))?\s*\]")
_SENT_END_RE = re.compile(r"(?<=[.!?])\s+|\n")


@dataclass(frozen=True)
class Verified:
    start: int
    end: int
    text: str
    doc: int
    page: int


def _own_cites(text: str, f: Fact) -> list[tuple[int, int]]:
    """The citations of the sentence a fact stands in (markers up to the end
    of the sentence, and right after it), else of its line (a table row),
    each as (document, page or 0)."""
    line_s = text.rfind("\n", 0, f.start) + 1
    line_e = text.find("\n", f.end)
    line_e = len(text) if line_e < 0 else line_e
    nxt = _SENT_END_RE.search(text, f.end)
    sent_e = line_e if not nxt or nxt.start() > line_e else nxt.start()
    # Markers directly after the sentence end ("… 1.240,00 €.[^3-p1]").
    tail = re.match(r"(?:\s*\[\^[^\]]*\])+", text[sent_e:line_e])
    if tail:
        sent_e += tail.end()
    prev = max((m.end() for m in _SENT_END_RE.finditer(text, line_s, f.start)), default=line_s)
    own = [(int(m.group(1)), int(m.group(2) or 0)) for m in _CITE_RE.finditer(text, prev, sent_e)]
    if own:
        return own
    return [(int(m.group(1)), int(m.group(2) or 0)) for m in _CITE_RE.finditer(text, line_s, line_e)]


def verify(text: str, allowed: set[int], pages_of) -> list[Verified]:
    """The hard facts of ``text`` found in a cited source.

    A fact is looked up first in what its own sentence (else line) cites —
    the cited page, then the document's other pages — then in every other
    document the answer cites. Only documents in ``allowed`` are read: those a
    tool returned to this user, so a mark never tells anything about a document
    they cannot open. ``pages_of(doc)`` gives a document's [(page, text)]."""
    text = text or ""
    cited_all: list[int] = []
    for m in _CITE_RE.finditer(text):
        d = int(m.group(1))
        if d in allowed and d not in cited_all:
            cited_all.append(d)
    if not cited_all:
        return []
    cache: dict[int, list[tuple[int, str]]] = {}

    def pages(doc: int) -> list[tuple[int, str]]:
        if doc not in cache:
            try:
                cache[doc] = list(pages_of(doc) or [])
            except Exception:  # noqa: BLE001 — no sidecar: nothing to check against
                cache[doc] = []
        return cache[doc]

    out: list[Verified] = []
    for f in facts(text):
        order: list[tuple[int, int]] = []
        for d, p in _own_cites(text, f):
            if d in allowed and (d, p) not in order:
                order.append((d, p))
        for d in cited_all:
            if (d, 0) not in order:
                order.append((d, 0))
        hit = None
        for d, p in order:
            pg = pages(d)
            ranked = sorted(pg, key=lambda x: x[0] != p) if p else pg
            hit = next(((d, n) for n, t in ranked if find(f, t)), None)
            if hit:
                break
        if hit:
            out.append(Verified(f.start, f.end, f.text, hit[0], hit[1]))
    return out


def word_spans(fact_text: str, words: list[str]) -> list[int]:
    """Indices of the words (a page's text layer, in reading order) that hold
    ``fact_text`` — the first place it stands; [] when it is not there."""
    fs = facts(fact_text, hard_only=False)
    f = next((x for x in fs if x.start == 0 and x.end == len(fact_text.strip())), None)
    f = f or (fs[0] if fs else Fact(0, len(fact_text), fact_text, _kind(fact_text)))
    joined, starts = "", []
    for w in words:
        starts.append(len(joined))
        joined += w + " "
    spans = find(f, joined)
    if not spans:
        return []
    s, e = spans[0]
    return [i for i, st in enumerate(starts) if st < e and st + len(words[i]) > s]
