# services/citations.py

"""Where on a page a cited statement comes from — the passage and its boxes.

A chat answer cites a document as ``[^241-p3]`` (document 241, page 3) or
``[^241]`` (the document as a whole). The model names the page, never the
passage: asking it to quote would be one more thing to get wrong, and a quote
that does not occur in the document is worse than no highlight. The passage is
found in code instead, from two things the model cannot make up:

- the **claim** — the sentence of the answer the marker stands behind, and
- the **hint chunks** — the passages the search embedding actually matched.

``best_passage`` picks the stretch of the page text that shares the most
content words (numbers counting triple) with the claim. ``align_words`` then
looks that passage up in the PDF's own text layer to get boxes. Both may come
back empty — a claim in another language than the document, a scan without a
text layer — and the preview then shows the page without a highlight. A wrong
highlight would be a confident false statement; a missing one is not.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

_log = logging.getLogger(__name__)

# [^241-p3], [^241] — and the variants models write when they half-remember
# the format: [^#241 p.3], [^241, S. 3], [^241:p3]. A caret doubled in front
# ("cbm)^[^577-p1]", seen live) goes with the marker.
CITE_RE = re.compile(
    r"\^?\[\^\s*#?(\d{1,7})(?:\s*[-,:]?\s*(?:p|S|page|Seite)\.?\s*(\d{1,4}))?\s*\]",
    re.IGNORECASE,
)


def cite_ref(doc_id: int, page: int | None = None) -> str:
    """The marker the model copies from a tool result."""
    return f"[^{int(doc_id)}-p{int(page)}]" if page else f"[^{int(doc_id)}]"


def number_citations(text: str, render) -> tuple[str, list[tuple[int, int, int]]]:
    """Replace every marker with ``render(n, doc_id, page)``; number by first use.

    The same source cited twice keeps its number; ``page`` is 0 for a citation
    of the whole document. A marker repeated right after itself is dropped.
    Returns the new text and the sources as ``[(n, doc_id, page), …]``.
    """
    numbers: dict[tuple[int, int], int] = {}
    last_end = [-1]
    last_key = [None]

    def sub(m: re.Match) -> str:
        key = (int(m.group(1)), int(m.group(2) or 0))
        if key not in numbers:
            numbers[key] = len(numbers) + 1
        repeat = m.start() == last_end[0] and key == last_key[0]
        last_end[0], last_key[0] = m.end(), key
        return "" if repeat else render(numbers[key], *key)

    out = CITE_RE.sub(sub, text or "")
    return out, [(n, doc, page) for (doc, page), n in numbers.items()]


def plain_citations(text: str, page_label: str = "p. {n}") -> str:
    """Markers as superscript numbers plus a source list — for a PDF or a copy."""
    from services.web_citations import plain_web_refs

    out, sources = number_citations(plain_web_refs(text), lambda n, _d, _p: f"<sup>{n}</sup>")
    if not sources:
        return out
    items = "  \n".join(
        f"<sup>{n}</sup> #{doc}" + (" · " + page_label.format(n=page) if page else "")
        for n, doc, page in sources
    )
    return f"{out.rstrip()}\n\n---\n\n{items}\n"


# Words that carry no content in either archive language. Kept short on
# purpose: a missing stopword costs a point of score on every segment alike.
_STOPWORDS = frozenset(
    """
    der die das den dem des ein eine einen einem einer eines und oder aber
    mit von vom zum zur für auf aus bei bis nach über unter vor ist sind war
    wird werden wurde hat haben hatte nicht kein keine auch noch nur schon
    sich sie ihr ihre ihren ihm ihn wir uns unser unsere euer dass wenn als
    wie was wer welche welcher welches dieser diese dieses jener muss müssen
    kann können soll sollen darf dürfen laut gemäß demnach dabei dazu daher
    the and or but with from for on of to in at by is are was were be been
    has have had not no also only this that these those which who what when
    as it its their they you your our must can shall should may per
    dokument document seite page
    """.split()
)

# A code with a digit after a letter ("4F60", "2C91", "ESt2018") is one token:
# split, the barcode "02 2FF7 4F60" offered a lone "4" that matched "um 4 %".
# A number followed by a unit ("700C", "176m") still splits.
_TOKEN_RE = re.compile(r"\d*[^\W\d_]+\d[^\W_]*|\d+(?:[.,]\d+)*|[^\W\d_]+", re.UNICODE)
# Digit groups of an id ("12 345 678 901", an IBAN): the model writes them
# grouped or not, so both readings are tokens.
_DIGIT_GROUPS_RE = re.compile(r"(?<![\d.,])\d{1,4}(?: \d{1,4}){2,}(?![\d.,])")
_YEAR_RE = re.compile(r"(?:19|20)\d\d")
# "10.847,52 €" in the answer, "Euro: 10847 Cent: 52" in a form: the euros
# alone are a token too.
_AMOUNT_RE = re.compile(r"(?<![\d.,])(\d{1,3}(?:\.\d{3})+|\d{3,}),\d{2}(?![\d.,])")


def _norm_number(tok: str) -> str:
    # "1.014,01" and "1014.01" are the same amount; compare digits only.
    return re.sub(r"[.,]", "", tok)


def _fold(word: str) -> str:
    # OCR text layers drop umlauts as often as they keep them ("Warmebedarfs");
    # compare without diacritics on both sides.
    word = word.lower().replace("ß", "ss")
    return "".join(
        c for c in unicodedata.normalize("NFKD", word) if not unicodedata.combining(c)
    )


def tokens(text: str) -> list[str]:
    """Every word and number of ``text``: lowercased, without diacritics; numbers
    without separators."""
    out = []
    for tok in _TOKEN_RE.findall(text or ""):
        if tok[0].isdigit() and _norm_number(tok).isdigit():
            out.append(_norm_number(tok))
        else:
            out.append(_fold(tok))
    return out


def _is_num(tok: str) -> bool:
    return tok.isdigit()


# "Seite 5 von 5", "Page 2", "S. 3": a page number is where the text sits, not
# what it says. A claim headed "Seite 5: …" matched the footer "Seite 5 von 5"
# for its number alone, which counts triple.
_PAGE_NO_RE = re.compile(
    r"\b(?:seite|page|s\.|p\.)\s*\d+(?:\s*(?:von|of|/)\s*\d+)?", re.IGNORECASE
)


def content_tokens(text: str) -> set[str]:
    text = _PAGE_NO_RE.sub(" ", text or "")
    out = {
        t for t in tokens(text)
        if _is_num(t) or (len(t) >= 3 and t not in _STOPWORDS)
    }
    out |= {m.group(0).replace(" ", "") for m in _DIGIT_GROUPS_RE.finditer(text)}
    out |= {m.group(1).replace(".", "") for m in _AMOUNT_RE.finditer(text)}
    return out


def _weight(tok: str) -> float:
    """A distinctive number (an amount, an id) counts triple. A year, a day or
    a "4" does not: "2018" stands in every footer of the 2018 tax return, and
    it outscored the line with the tax id the claim was about."""
    if not _is_num(tok):
        return 1.5 if any(c.isdigit() for c in tok) else 1.0  # a code
    if len(tok) <= 2 or (len(tok) == 4 and _YEAR_RE.fullmatch(tok)):
        return 1.0
    return 3.0


def _stem(tok: str) -> str:
    # German inflects heavily ("erneuerbaren" / "erneuerbare"); a shared
    # prefix of six letters is close enough for picking a passage.
    return tok if _is_num(tok) or len(tok) < 7 else tok[:6]


def _same_amount(a: str, b: str) -> bool:
    # Separators are gone, so "176 m³" and "176,000 cbm" read 176 and 176000.
    # Trailing zeros are decimals often enough to count — at reduced weight.
    a0, b0 = a.rstrip("0"), b.rstrip("0")
    return a0 == b0 and len(a0) >= 2 and abs(len(a) - len(b)) <= 3


def _score(seg_tokens: set[str], claim: set[str]) -> float:
    """How much of the claim ``seg_tokens`` covers — each claim word counts once.

    Coverage, not overlap: a paragraph repeating "Energie", "Energieträger",
    "Energien" must not outscore the one sentence that has the claim's number.
    """
    if not seg_tokens or not claim:
        return 0.0
    seg_stems = {_stem(t) for t in seg_tokens}
    score = 0.0
    for t in claim:
        if t in seg_tokens:
            score += _weight(t)
        elif _is_num(t):
            if any(_is_num(s) and _same_amount(t, s) for s in seg_tokens):
                score += 0.7 * _weight(t)
        elif _stem(t) in seg_stems or (
            # German compounds: "Wärme" is in "Wärmebedarfs".
            len(t) >= 4 and not _is_num(t) and any(s.startswith(t) for s in seg_tokens)
        ):
            score += 0.7 * _weight(t)
    return score


# Long lines are split at sentence ends; short lines ("Field: value") are
# segments of their own.
# Only after a word of two or more letters: "nach dem 1. Juli" and "z. B." are
# not sentence ends.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[^\W\d_]{2}[.!?;])\s+(?=[A-ZÄÖÜ\"„(])")


def segments(page_text: str) -> list[str]:
    out = []
    for line in (page_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        out.extend(s.strip() for s in _SENTENCE_SPLIT_RE.split(line) if s.strip())
    return out


def _squash(text: str) -> str:
    return " ".join(tokens(text))


MAX_WINDOW = 3
#: A passage must cover half the claim's weight, or this much: a lone year or
#: a lone "4" shared with a footer is no support.
MIN_SUPPORT = 2.0


def claim_weight(claim: set[str]) -> float:
    return sum(_weight(t) for t in claim)


def best_passage(
    page_text: str, claim: str, hints: list[str] | tuple[str, ...] = ()
) -> tuple[str, float]:
    """The stretch of ``page_text`` (1–3 segments) that best supports ``claim``.

    Returns ``(passage, score)``; ``("", 0.0)`` when nothing on the page shares
    a content word with the claim and no hint chunk lies on it.
    """
    segs = segments(page_text)
    if not segs:
        return "", 0.0
    claim_toks = content_tokens(claim)
    seg_toks = [content_tokens(s) for s in segs]
    hint_texts = [_squash(h) for h in hints if h]

    def in_hint(i: int) -> bool:
        s = _squash(segs[i])
        return bool(s) and any(s in h for h in hint_texts)

    hinted = [in_hint(i) for i in range(len(segs))]

    best = ("", 0.0, 0)
    for i in range(len(segs)):
        union: set[str] = set()
        bonus = 0.0
        for j in range(i, min(i + MAX_WINDOW, len(segs))):
            # A window never bridges a segment that shares nothing with the
            # claim — that segment would be highlighted for no reason.
            if j > i and _score(seg_toks[j], claim_toks) == 0 and not hinted[j]:
                break
            union |= seg_toks[j]
            bonus += 0.5 if hinted[j] else 0.0
            # Each added segment must earn its place, or a long window wins
            # by accident of length.
            score = _score(union, claim_toks) + bonus - 1.0 * (j - i)
            text = " ".join(segs[i : j + 1])
            if score > best[1] or (score == best[1] and score > 0 and len(text) < best[2]):
                best = (text, score, len(text))
    if best[1] > 0.5 and best[1] >= min(MIN_SUPPORT, 0.5 * claim_weight(claim_toks)):
        return best[0], best[1]
    # No overlap with the claim (e.g. answer in English, document in German):
    # the hint chunk is still where the search found the document.
    for i, h in enumerate(hinted):
        if h:
            j = i
            while j + 1 < len(segs) and hinted[j + 1] and j + 1 < i + MAX_WINDOW:
                j += 1
            return " ".join(segs[i : j + 1]), 0.0
    return "", 0.0


def best_page(
    pages: list[tuple[int, str]], claim: str, hints: list[str] | tuple[str, ...] = ()
) -> int:
    """For a citation without a page: the page whose best passage scores highest."""
    best_no, best_score = 0, 0.0
    for number, text in pages:
        passage, score = best_passage(text, claim, hints)
        if passage and (score > best_score or not best_no):
            best_no, best_score = number, score
    return best_no or (pages[0][0] if pages else 1)


def sidecar_page_texts(sidecar: dict | None) -> list[tuple[int, str]]:
    """Per page: its text and the text of its pictures (title + description)."""
    from services.page_images import page_images

    out = []
    for n, p in enumerate((sidecar or {}).get("pages") or [], 1):
        pics = [
            " ".join(x for x in (i.get("belongs_to"), i.get("description")) if x)
            for i in page_images(p)
        ]
        out.append((int(p.get("page_number") or n), "\n".join([p.get("page_text") or "", *pics])))
    return out


# A number counts 3, so a page reaches this with one shared number or three words.
PAGE_FOR_MIN = 3.0


def page_for(pages: list[tuple[int, str]], text: str) -> int | None:
    """The page ``text`` (a summary sentence, a reference number) comes from —
    None unless one page clearly wins. Lets a tool print [^241-p3] next to a
    fact that has a place, instead of the bare [^241] that has none."""
    scored = sorted(((best_passage(t, text)[1], n) for n, t in pages), reverse=True)
    if not scored or scored[0][0] < PAGE_FOR_MIN:
        return None
    if len(scored) > 1 and scored[1][0] > scored[0][0] - 1.0:
        return None
    return scored[0][1]


# Title abbreviations end in a period but not a sentence: "für die Dr. Möldner".
_ABBREVIATIONS = frozenset(
    "dr prof nr hr fr st ca bzw inkl ggf vgl mr mrs ms no".split()
)


def split_sentences(text: str) -> list[str]:
    out: list[str] = []
    for piece in _SENTENCE_SPLIT_RE.split(" ".join((text or "").split())):
        if not piece.strip():
            continue
        last = out[-1].rsplit(" ", 1)[-1].rstrip(".").lower() if out else ""
        if out and out[-1].endswith(".") and last in _ABBREVIATIONS:
            out[-1] += " " + piece.strip()
        else:
            out.append(piece.strip())
    return out


# ── PDF text layer ───────────────────────────────────────────────────────────

Box = tuple[float, float, float, float]  # x, y, w, h — page fractions, top-left


@dataclass
class Word:
    text: str
    box: Box


def pdf_words(pdf_bytes: bytes, page_number: int) -> list[Word]:
    """The words of one page with their boxes, from the PDF's text layer.

    Empty for a page without one (a scan Paperless did not OCR), and for a
    rotated page — its char boxes are in unrotated space and would land
    beside the text in the render.
    """
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        if not 1 <= page_number <= len(doc):
            return []
        page = doc[page_number - 1]
        if page.get_rotation():
            return []
        left, bottom, right, top = page.get_cropbox()
        width, height = right - left, top - bottom
        if width <= 0 or height <= 0:
            return []
        textpage = page.get_textpage()
        n = textpage.count_chars()
        if n <= 0:
            return []
        text = textpage.get_text_range(0, n)
        if len(text) != n:  # surrogate pairs shift indices — give up, no guess
            return []
        words: list[Word] = []
        cur: list[str] = []
        x0 = y0 = float("inf")
        x1 = y1 = float("-inf")

        def flush() -> None:
            if cur and x1 > x0 and y1 > y0:
                words.append(
                    Word(
                        "".join(cur),
                        (
                            (x0 - left) / width,
                            (top - y1) / height,
                            (x1 - x0) / width,
                            (y1 - y0) / height,
                        ),
                    )
                )

        for i, ch in enumerate(text):
            # pdfium marks a hyphen at a line end with U+FFFE (or U+0002) and
            # glues both halves into one "word" — whose box would then span
            # two lines. Each half keeps its own box.
            if ch.isspace() or ch in "\x00\x02\ufffe\xad":
                flush()
                cur, x0, y0, x1, y1 = [], float("inf"), float("inf"), float("-inf"), float("-inf")
                continue
            l, b, r, t = textpage.get_charbox(i)
            cur.append(ch)
            x0, y0, x1, y1 = min(x0, l), min(y0, b), max(x1, r), max(y1, t)
        flush()
        return words
    finally:
        doc.close()


MIN_MATCH = 0.5


def align_words(words: list[Word], passage: str) -> list[Box]:
    """Boxes (one per text line) covering ``passage`` in ``words``, or [].

    Slides a window as long as the passage over the page's words and keeps the
    one sharing the most tokens with it — the PDF's OCR and the vision model's
    reading differ in details, so an exact search would rarely hit. Below half
    the passage's tokens nothing is returned.
    """
    target = tokens(passage)
    if not target or not words:
        return []
    wtoks = [tokens(w.text) for w in words]
    # A PDF "word" may hold several tokens ("15%," → "15"), and many hold none.
    flat: list[tuple[str, int]] = [(t, i) for i, ts in enumerate(wtoks) for t in ts]
    if not flat:
        return []
    need = Counter(target)
    size = len(target)

    window: Counter = Counter()
    matched = 0
    best = (0, 0, -1)  # matched, start, end (indices into flat)

    def add(tok: str) -> int:
        window[tok] += 1
        return 1 if window[tok] <= need.get(tok, 0) else 0

    def remove(tok: str) -> int:
        window[tok] -= 1
        return 1 if window[tok] < need.get(tok, 0) else 0

    for end in range(len(flat)):
        matched += add(flat[end][0])
        start = end - size + 1
        if start > 0:
            matched -= remove(flat[start - 1][0])
        if matched > best[0]:
            best = (matched, max(start, 0), end)
    if best[0] < max(2, MIN_MATCH * size):
        return _anchor_boxes(words, wtoks, target)

    _, s, e = best
    # Trim the window to its first and last token that belongs to the passage.
    while s < e and flat[s][0] not in need:
        s += 1
    while e > s and flat[e][0] not in need:
        e -= 1
    in_window = {flat[k][0] for k in range(s, e + 1)}
    nums = [t for t in need if _is_num(t) and _weight(t) >= 3.0]
    if any(t not in in_window for t in nums):
        # An amount of the passage is not in the window: a table, whose text layer
        # holds the label column first and the value column after it ("37,24"
        # 28 words from "Sockelbeteiligung"). The label words then matched a
        # sentence below the table instead. Mark the amount's row.
        rows = _row_boxes(words, wtoks, nums)
        if rows:
            return rows
    picked = sorted({flat[k][1] for k in range(s, e + 1)})
    return _line_boxes([words[i].box for i in picked])


def _row_boxes(words: list[Word], wtoks: list[list[str]], nums: list[str]) -> list[Box]:
    """The text lines through each of ``nums`` that occurs exactly once on the
    page — in a table, the label and the value share the line."""
    counts = Counter(t for ts in wtoks for t in ts)
    hits = [i for i, ts in enumerate(wtoks) if any(t in nums and counts[t] == 1 for t in ts)]
    picked: set[int] = set()
    for i in hits:
        _x, y, _w, h = words[i].box
        for j, w in enumerate(words):
            cy = w.box[1] + w.box[3] / 2
            if y <= cy <= y + h:
                picked.add(j)
    return _line_boxes([words[i].box for i in sorted(picked)])


# A form puts the label where the template has it and the filled-in value at
# the end of the text stream: "Datum:" and "22.03.2026" are 40 words apart, and
# no window holds both. Short passages fall back to their distinctive tokens.
ANCHOR_MAX_TOKENS = 12


def _anchor_boxes(words: list[Word], wtoks: list[list[str]], target: list[str]) -> list[Box]:
    """Boxes of the passage's tokens that occur exactly once on the page.

    Only numbers and long words count — "in" or "Datum" occur anywhere — and
    at least one number or two words must be found, or nothing is returned.
    """
    if len(target) > ANCHOR_MAX_TOKENS:
        return []
    counts = Counter(t for ts in wtoks for t in ts)
    anchors = {
        t for t in target
        if counts.get(t) == 1 and (len(t) >= 2 if _is_num(t) else len(t) >= 6)
    }
    if not any(_is_num(t) for t in anchors) and len(anchors) < 2:
        return []
    picked = [i for i, ts in enumerate(wtoks) if anchors.intersection(ts)]
    return _line_boxes([words[i].box for i in picked])


def _line_boxes(boxes: list[Box]) -> list[Box]:
    """Merge word boxes into one box per text line (reading order kept)."""
    lines: list[list[float]] = []  # x0, y0, x1, y1
    for x, y, w, h in boxes:
        cy = y + h / 2
        for ln in lines:
            if ln[1] <= cy <= ln[3] and x >= ln[0] - 0.02:
                ln[0], ln[1] = min(ln[0], x), min(ln[1], y)
                ln[2], ln[3] = max(ln[2], x + w), max(ln[3], y + h)
                break
        else:
            lines.append([x, y, x + w, y + h])
    return [(x0, y0, x1 - x0, y1 - y0) for x0, y0, x1, y1 in lines]


# ── Resolution ───────────────────────────────────────────────────────────────


@dataclass
class CiteTarget:
    doc_id: int
    title: str
    page: int
    page_count: int
    passage: str = ""
    boxes: list[Box] = field(default_factory=list)
    # The same number stands behind other sentences of the answer too: their
    # passages on this page, marked more lightly than the clicked one.
    other_boxes: list[Box] = field(default_factory=list)
    # width / height of every page, so the preview can reserve each page's
    # space before its image loads — the scroll position stays put.
    ratios: list[float] = field(default_factory=list)


def page_ratios(pdf_bytes: bytes) -> list[float]:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        out = []
        for i in range(len(doc)):
            w, h = doc[i].get_size()
            if doc[i].get_rotation() in (90, 270):
                w, h = h, w
            out.append(w / h if w > 0 and h > 0 else 1 / 1.414)
        return out
    finally:
        doc.close()


def _sidecar_pages(doc_id: int) -> list[tuple[int, str]]:
    from services.clients import sidecar_service

    sc = sidecar_service.load_sidecar(doc_id) or {}
    return [
        (int(p.get("page_number") or n), p.get("page_text") or "")
        for n, p in enumerate(sc.get("pages") or [], 1)
    ]


def _sidecar_pictures(doc_id: int) -> dict[int, list[tuple[str, Box]]]:
    """Per page: each located picture's text (title + description) and box."""
    from services.clients import sidecar_service
    from services.page_images import page_images

    sc = sidecar_service.load_sidecar(doc_id) or {}
    out: dict[int, list[tuple[str, Box]]] = {}
    for n, p in enumerate(sc.get("pages") or [], 1):
        for img in page_images(p):
            box = img.get("bbox")
            if box and len(box) == 4:
                text = " ".join(x for x in (img.get("belongs_to"), img.get("description")) if x)
                out.setdefault(int(p.get("page_number") or n), []).append((text, tuple(box)))
    return out


def best_pictures(pictures: list[tuple[str, Box]], claim: str) -> tuple[list[Box], float, str]:
    """The pictures that support ``claim``: ``(boxes, score, text of the best)``.

    An X-ray page carries one line of text; what the answer says about it comes
    from the picture's description, so the picture is the place to mark. Every
    picture close to the best counts — "four radiographs" is all four.
    """
    claim_toks = content_tokens(claim)
    scored = [(_score(content_tokens(t), claim_toks), t, b) for t, b in pictures]
    top = max((sc for sc, _t, _b in scored), default=0.0)
    if top <= 0.5:
        return [], 0.0, ""
    best_text = next(t for sc, t, _b in scored if sc == top)
    return [b for sc, _t, b in scored if sc >= 0.4 * top and sc > 1.0], top, best_text


def document_page(
    pages: list[tuple[int, str]],
    pictures: dict[int, list[tuple[str, Box]]],
    claims: list[str],
    hints: list[str] | tuple[str, ...] = (),
) -> int:
    """The one page a citation without a page stands for.

    Chosen for all sentences carrying the number together, never per sentence:
    picking per tapped sentence sent one "1" to pages 2, 1, 5 and 5.
    """
    if not pages:
        return 1
    with_pictures = [
        (n, "\n".join([t, *(pt for pt, _b in pictures.get(n, []))])) for n, t in pages
    ]
    return best_page(with_pictures, " ".join(claims), hints)


def _overlaps(a: Box, b: Box) -> bool:
    return a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]


async def locate(
    doc_id: int,
    page: int | None,
    claim: str,
    token: str,
    hints: list[str] | tuple[str, ...] = (),
    other_claims: list[str] | tuple[str, ...] = (),
) -> CiteTarget | None:
    """Resolve a citation for the user behind ``token``; None without access.

    ``other_claims`` are the other sentences citing the same source with the
    same number; their passages land in ``other_boxes``.
    """
    import asyncio

    from services import doc_image_service

    src = await doc_image_service.fetch_source(doc_id, token)
    if src is None:
        return None
    doc, pdf_bytes = src
    pages = _sidecar_pages(doc_id)
    pictures = _sidecar_pictures(doc_id)
    page_count = doc.page_count or len(pages) or 1
    if not page or not 1 <= page <= page_count:
        page = document_page(pages, pictures, [claim, *other_claims], hints)
    text = dict(pages).get(page, "")
    on_page = pictures.get(page, [])
    passage, score = best_passage(text, claim, hints)
    pic_boxes, pic_score, pic_text = best_pictures(on_page, claim)
    if pic_score > score:
        passage = ""
        if len(pic_text) > 240:
            pic_text = pic_text[:240].rsplit(" ", 1)[0] + " …"
    target = CiteTarget(
        doc_id, doc.title or f"#{doc_id}", page, page_count, passage or pic_text
    )
    # Another sentence's passage counts only on its own merit — no hint
    # fallback, which would mark the search's chunk for every one of them.
    others: list[str] = []
    other_pics: list[Box] = []
    for c in other_claims:
        p, ps = best_passage(text, c)
        boxes, bs, _t = best_pictures(on_page, c)
        if bs > ps:
            other_pics += [b for b in boxes if b not in pic_boxes]
        elif p and p != passage:
            others.append(p)
    if pic_score > score:
        target.boxes = pic_boxes
    target.other_boxes = [b for b in other_pics if b not in target.boxes]
    if pdf_bytes:
        def _read() -> tuple[list[Box], list[Box], list[float]]:
            with doc_image_service.pdfium_lock:
                words = pdf_words(pdf_bytes, page) if passage or others else []
                boxes = align_words(words, passage) if passage else target.boxes
                more = [
                    b for o in others for b in align_words(words, o)
                    if not any(_overlaps(b, x) for x in boxes)
                ]
                return boxes, target.other_boxes + more, page_ratios(pdf_bytes)

        try:
            target.boxes, target.other_boxes, target.ratios = await asyncio.to_thread(_read)
        except Exception as exc:
            _log.warning("citation: text layer of #%s p%s unreadable: %s", doc_id, page, exc)
    if passage and not target.boxes:
        _log.info("citation #%s p%s: passage not on the text layer: %.80r", doc_id, page, passage)
    return target


# ── Wrong page in a marker ───────────────────────────────────────────────────

_BLOCK_END_RE = re.compile(r"\n")
_CLAIM_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def claim_before(text: str, pos: int) -> str:
    """The claim of the marker at ``pos`` — the same rule the browser applies
    (`__citeClaim` in the chat page): the last sentence of its line before the
    marker, with the one before it when that is short."""
    start = text.rfind("\n", 0, pos) + 1
    block = CITE_RE.sub(" ", text[start:pos])
    sents = [x for x in _CLAIM_SPLIT_RE.split(" ".join(block.split())) if x]
    claim = sents.pop() if sents else ""
    if len(claim) < 30 and sents:
        claim = sents.pop() + " " + claim
    return claim[-400:]


def _supports(page_text: str, claim: str) -> float:
    passage, score = best_passage(page_text, claim)
    return score if passage and score > 0 else 0.0


def repair_pages(text: str, pages_of=None) -> str:
    """Point a marker at the page its claim is on, where the model copied the
    wrong one.

    The model copies ``[^32-p3]`` from a tool result and sometimes puts it
    behind a fact from another page of the same document ("290,58 €" is on
    page 4, cited as page 3). The highlight then finds nothing, and the number
    sends the reader to the wrong page. When the claim has no support on the
    cited page and one other page supports it clearly — or several equally,
    then the first — the marker is rewritten, and numbering gives it its own
    number. A marker whose page does support its claim is never touched.
    """
    pages_of = pages_of or _sidecar_pages
    cache: dict[int, list[tuple[int, str]]] = {}
    out, last = [], 0
    for m in CITE_RE.finditer(text or ""):
        if not m.group(2):
            continue
        doc, page = int(m.group(1)), int(m.group(2))
        if doc not in cache:
            try:
                cache[doc] = pages_of(doc)
            except Exception:
                cache[doc] = []
        pages = cache[doc]
        if len(pages) < 2:
            continue
        claim = claim_before(text, m.start())
        if not claim or _supports(dict(pages).get(page, ""), claim) > 0:
            continue
        scored = [(sc, n) for n, t in pages if n != page and (sc := _supports(t, claim)) >= PAGE_FOR_MIN]
        if not scored:
            continue
        top = max(sc for sc, _n in scored)
        better = min(n for sc, n in scored if sc > top - 1.0)
        out.append(text[last:m.start()] + cite_ref(doc, better))
        last = m.end()
    if not out:
        return text
    return "".join(out) + text[last:]
