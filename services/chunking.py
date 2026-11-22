# services/chunking.py

"""
Chunking pipeline for VL-extracted document text.

Splits concatenated page_text into ~200-word chunks with ~30-word overlap,
respecting paragraph boundaries. Each chunk gets a document context prefix
for better retrieval with multilingual-e5-large-instruct.
"""

import re
from datetime import date, datetime  # noqa: F401 — used in type annotations
from typing import Callable

from pydantic import BaseModel


class Chunk(BaseModel):
    """Single chunk ready for ChromaDB insertion."""

    text: str  # context prefix + chunk content
    chunk_index: int
    page_number: int  # page where this chunk starts
    char_start: int  # position in full_text (for debugging)
    char_end: int
    word_count: int


class ChunkingConfig(BaseModel):
    """Tunable chunking parameters."""

    target_words: int = 200
    max_words: int = 250
    overlap_words: int = 30


HEADER_VERSION = 2
"""Chunks whose metadata `header` is below this get rebuilt (`pipelines.rechunk`).

1: "Dokument: <type>, <correspondent>, <date> — ".
2: + the document title and the first sentence of its summary. Measured
   (`eval/harness/headers.py`): long documents MRR 0.69 → 0.88.
"""

_SEP = " — "


def _clean(text: str | None) -> str:
    # The separator ends a header field; it must not occur inside one, or
    # `strip_context_prefix` cuts in the wrong place.
    return " ".join((text or "").replace("_", " ").split()).replace(_SEP, " - ")


def first_sentence(text: str | None, limit: int = 200) -> str:
    """The first sentence of a summary (at least 20 chars), cut at `limit`."""
    text = " ".join((text or "").split())
    m = re.match(r"(.{20,}?[.!?])\s", text + " ")
    s = m.group(1) if m else text
    return s if len(s) <= limit else s[:limit].rsplit(" ", 1)[0] + " …"


def context_line(
    document_type: str | None,
    correspondent: str | None,
    document_date: "date | datetime | None",
) -> str:
    """"<type>, <correspondent>, <DD.MM.YYYY>" — the known parts only."""
    parts = []
    if document_type:
        parts.append(_clean(document_type))
    if correspondent:
        parts.append(_clean(correspondent))
    if document_date:
        parts.append(document_date.strftime("%d.%m.%Y"))
    return ", ".join(parts)


def build_header(context: str, title: str | None = None, summary: str | None = None) -> str:
    """The header from a `context_line`, the title and the document summary."""
    head = " | ".join(x for x in (_clean(title), _clean(context)) if x)
    if not head:
        return ""
    out = f"Dokument: {head}{_SEP}"
    sentence = _clean(first_sentence(summary)) if summary else ""
    if sentence:
        out += f"Inhalt: {sentence}{_SEP}"
    return out


def build_context_prefix(
    document_type: str | None,
    correspondent: str | None,
    document_date: "date | datetime | None",
    title: str | None = None,
    summary: str | None = None,
) -> str:
    """Build the context header prepended to every chunk of a document.

    Example: "Dokument: Bescheid 2024 | Steuerbescheid, Finanzamt Karlsruhe,
    15.03.2025 — Inhalt: Einkommensteuerbescheid für 2024 mit Erstattung. — "

    Every vector of a long document carries what the document is: a chunk from
    page 7 of a contract does not say "Mietvertrag" itself.

    NOTE: the "Dokument:" / "Inhalt:" wording and the DD.MM.YYYY date are
    deliberately not genericized. The header is embedded into every stored
    chunk, so changing it invalidates the existing vectors — revisit only with
    a `HEADER_VERSION` step (which rebuilds the chunks from the sidecars).
    """
    return build_header(context_line(document_type, correspondent, document_date), title, summary)


def context_from_v1(text: str | None) -> str:
    """The context line of a version-1 chunk ("Dokument: <context> — …")."""
    text = text or ""
    if text.startswith("Dokument: "):
        idx = text.find(_SEP)
        if idx != -1:
            return text[len("Dokument: "):idx]
    return ""


def strip_context_prefix(text: str | None) -> str:
    """The chunk without its context header — what the document itself says.

    For display and for passage matching: the header's title and summary
    sentence are not the document's words, and a citation must not mark them.
    """
    text = text or ""
    if text.startswith("Dokument: "):
        idx = text.find(_SEP)
        if idx != -1:
            text = text[idx + len(_SEP):]
            if text.startswith("Inhalt: "):
                idx = text.find(_SEP)
                if idx != -1:
                    text = text[idx + len(_SEP):]
    return text


def _split_into_sentences(text: str) -> list[str]:
    """Split text into sentences. Handles German abbreviations reasonably."""
    # Split on sentence-ending punctuation followed by space + uppercase,
    # or followed by newline. Keep the delimiter with the preceding sentence.
    parts = re.split(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ])", text)
    return [p.strip() for p in parts if p.strip()]


def _word_count(text: str) -> int:
    return len(text.split())


def _get_overlap_text(text: str, overlap_words: int) -> str:
    """Extract the last N words from text as overlap for the next chunk."""
    words = text.split()
    if len(words) <= overlap_words:
        return text
    return " ".join(words[-overlap_words:])


def _fit_words(text: str, budget: int, token_len: Callable[[str], int]) -> list[str]:
    """`text` cut at word boundaries into pieces of at most `budget` tokens."""
    pieces: list[str] = []
    words = text.split()
    start = 0
    while start < len(words):
        # Grow by halving steps: tokenising the candidate is exact, and a
        # binary search keeps it to a few calls per piece.
        lo, hi = start + 1, len(words)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if token_len(" ".join(words[start:mid])) <= budget:
                lo = mid
            else:
                hi = mid - 1
        pieces.append(" ".join(words[start:lo]))
        start = lo
    return pieces


def chunk_document(
    page_texts: list[str],
    context_prefix: str = "",
    config: ChunkingConfig | None = None,
    token_len: Callable[[str], int] | None = None,
    max_tokens: int = 512,
) -> list[Chunk]:
    """Chunk a document's page texts into retrieval-sized pieces.

    Args:
        page_texts: List of page_text strings, one per page (ordered).
        context_prefix: Prepended to every chunk (from build_context_prefix).
        config: Chunking parameters. Uses defaults if None.
        token_len: The embedding model's token count of a text (special
            tokens included). With it, no chunk — header and overlap included
            — exceeds `max_tokens`: the model truncates the rest, and that text
            would never reach the index. German needs ~2 tokens a word, so a
            word budget alone left 41 % of the chunks cut off.
        max_tokens: The model's window.

    Returns:
        List of Chunk objects ready for ChromaDB insertion.
    """
    if config is None:
        config = ChunkingConfig()

    def fits(text: str) -> bool:
        return token_len is None or token_len(context_prefix + text) <= max_tokens

    # --- Step 1: Build a list of paragraphs with page tracking ---

    paragraphs: list[tuple[str, int]] = []  # (paragraph_text, page_number)

    for page_num, page_text in enumerate(page_texts, 1):
        # Split on double newlines (paragraph breaks) or single newlines
        # followed by a blank line
        raw_paragraphs = re.split(r"\n\s*\n", page_text.strip())
        for para in raw_paragraphs:
            cleaned = para.strip()
            if cleaned:
                paragraphs.append((cleaned, page_num))

    if not paragraphs:
        return []

    # --- Step 2: Break oversized paragraphs into sentences ---

    segments: list[tuple[str, int]] = []  # (text, page_number)
    # Room for one segment next to the header (and the overlap, which is
    # dropped when even that does not fit).
    seg_budget = None
    if token_len is not None:
        seg_budget = max(32, max_tokens - token_len(context_prefix))

    def add_segment(text: str, page_num: int) -> None:
        if seg_budget is not None and token_len(text) > seg_budget:
            for piece in _fit_words(text, seg_budget, token_len):
                segments.append((piece, page_num))
        else:
            segments.append((text, page_num))

    for para_text, page_num in paragraphs:
        if _word_count(para_text) <= config.max_words and fits(para_text):
            segments.append((para_text, page_num))
        else:
            # Paragraph too long — split into sentences
            sentences = _split_into_sentences(para_text)
            for sentence in sentences:
                # A "sentence" can still exceed the budget: sentence splitting
                # keys on [.!?] + capital, which OCR'd scans, address blocks,
                # table dumps and itemised lists often lack entirely. Without
                # this hard cap such text stays one giant segment, and the
                # embedding model (512-token window) silently truncates it —
                # the tail of the document never reaches the index.
                if _word_count(sentence) <= config.max_words:
                    add_segment(sentence, page_num)
                else:
                    words = sentence.split()
                    for i in range(0, len(words), config.max_words):
                        add_segment(" ".join(words[i : i + config.max_words]), page_num)

    # --- Step 3: Accumulate segments into chunks with overlap ---

    chunks: list[Chunk] = []
    current_texts: list[str] = []
    current_word_count = 0
    current_page = segments[0][1] if segments else 1
    current_char_start = 0
    char_position = 0
    overlap_prefix = ""

    def content(texts: list[str]) -> str:
        body = " ".join(texts)
        return overlap_prefix + " " + body if overlap_prefix else body

    def flush() -> None:
        nonlocal overlap_prefix
        chunk_content = content(current_texts)
        if not fits(chunk_content):
            # The overlap is a courtesy to the reader of the next chunk; the
            # chunk's own text must fit.
            chunk_content = " ".join(current_texts)
        full_text = context_prefix + chunk_content
        chunks.append(
            Chunk(
                text=full_text,
                chunk_index=len(chunks),
                page_number=current_page,
                char_start=current_char_start,
                char_end=char_position,
                word_count=_word_count(full_text),
            )
        )
        overlap_prefix = _get_overlap_text(chunk_content, config.overlap_words)

    for segment_text, page_num in segments:
        segment_words = _word_count(segment_text)

        # Would adding this segment exceed max? → flush current chunk
        if current_texts and (
            (current_word_count + segment_words) > config.max_words
            or not fits(content([*current_texts, segment_text]))
        ):
            flush()
            # Reset accumulator
            current_texts = []
            current_word_count = 0
            current_char_start = char_position
            current_page = page_num

        # Add segment to current chunk
        current_texts.append(segment_text)
        current_word_count += segment_words
        char_position += len(segment_text) + 1  # +1 for the join space

        # Track the page where this chunk starts
        if len(current_texts) == 1:
            current_page = page_num

    # --- Step 4: Flush remaining text as final chunk ---

    if current_texts:
        flush()

    return chunks
