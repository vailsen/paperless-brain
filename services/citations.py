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
# the format: [^#241 p.3], [^241, S. 3], [^241:p3].
CITE_RE = re.compile(
    r"\[\^\s*#?(\d{1,7})(?:\s*[-,:]?\s*(?:p|S|page|Seite)\.?\s*(\d{1,4}))?\s*\]",
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
    out, sources = number_citations(text, lambda n, _d, _p: f"<sup>{n}</sup>")
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

_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)*|[^\W\d_]+", re.UNICODE)


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
        out.append(_norm_number(tok) if tok[0].isdigit() else _fold(tok))
    return out


def content_tokens(text: str) -> set[str]:
    return {
        t for t in tokens(text)
        if t[0].isdigit() or (len(t) >= 3 and t not in _STOPWORDS)
    }


def _weight(tok: str) -> float:
    return 3.0 if tok[0].isdigit() else 1.0


def _stem(tok: str) -> str:
    # German inflects heavily ("erneuerbaren" / "erneuerbare"); a shared
    # prefix of six letters is close enough for picking a passage.
    return tok if tok[0].isdigit() or len(tok) < 7 else tok[:6]


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
        elif t[0].isdigit():
            if any(s[0].isdigit() and _same_amount(t, s) for s in seg_tokens):
                score += 0.7 * _weight(t)
        elif _stem(t) in seg_stems or (
            # German compounds: "Wärme" is in "Wärmebedarfs".
            len(t) >= 4 and not t[0].isdigit() and any(s.startswith(t) for s in seg_tokens)
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
    if best[1] > 0.5:
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
    picked = sorted({flat[k][1] for k in range(s, e + 1)})
    return _line_boxes([words[i].box for i in picked])


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
        if counts.get(t) == 1 and (len(t) >= 2 if t[0].isdigit() else len(t) >= 6)
    }
    if not any(t[0].isdigit() for t in anchors) and len(anchors) < 2:
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
    page_count = doc.page_count or len(pages) or 1
    if not page or not 1 <= page <= page_count:
        page = best_page(pages, claim, hints) if pages else 1
    text = dict(pages).get(page, "")
    passage, _score = best_passage(text, claim, hints)
    target = CiteTarget(doc_id, doc.title or f"#{doc_id}", page, page_count, passage)
    # Another sentence's passage counts only on its own merit — no hint
    # fallback, which would mark the search's chunk for every one of them.
    others = [p for c in other_claims if (p := best_passage(text, c)[0]) and p != passage]
    if pdf_bytes:
        def _read() -> tuple[list[Box], list[Box], list[float]]:
            with doc_image_service.pdfium_lock:
                words = pdf_words(pdf_bytes, page) if passage or others else []
                boxes = align_words(words, passage) if passage else []
                more = [
                    b for o in others for b in align_words(words, o)
                    if not any(_overlaps(b, x) for x in boxes)
                ]
                return boxes, more, page_ratios(pdf_bytes)

        try:
            target.boxes, target.other_boxes, target.ratios = await asyncio.to_thread(_read)
        except Exception as exc:
            _log.warning("citation: text layer of #%s p%s unreadable: %s", doc_id, page, exc)
    if passage and not target.boxes:
        _log.info("citation #%s p%s: passage not on the text layer: %.80r", doc_id, page, passage)
    return target
