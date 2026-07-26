# services/chunking.py

"""
Chunking pipeline for VL-extracted document text.

Splits concatenated page_text into ~200-word chunks with ~30-word overlap,
respecting paragraph boundaries. Each chunk gets a document context prefix
for better retrieval with multilingual-e5-large-instruct.
"""

import re
from datetime import date, datetime  # noqa: F401 — used in type annotations

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


def build_context_prefix(
    document_type: str | None,
    correspondent: str | None,
    document_date: "date | datetime | None",
) -> str:
    """Build a short context string prepended to every chunk.

    Example: "Dokument: Steuerbescheid, Finanzamt Karlsruhe, 15.03.2025 — "

    NOTE: the "Dokument:" wording and the DD.MM.YYYY date are deliberately not
    genericized. This prefix is embedded into every stored chunk, so changing it
    invalidates the existing vectors and would require re-ingesting the whole
    archive. Revisit only alongside a planned re-embed.
    """
    parts = []
    if document_type:
        parts.append(document_type)
    if correspondent:
        parts.append(correspondent)
    if document_date:
        parts.append(document_date.strftime("%d.%m.%Y"))

    if not parts:
        return ""
    return f"Dokument: {', '.join(parts)} — "


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


def chunk_document(
    page_texts: list[str],
    context_prefix: str = "",
    config: ChunkingConfig | None = None,
) -> list[Chunk]:
    """Chunk a document's page texts into retrieval-sized pieces.

    Args:
        page_texts: List of page_text strings, one per page (ordered).
        context_prefix: Prepended to every chunk (from build_context_prefix).
        config: Chunking parameters. Uses defaults if None.

    Returns:
        List of Chunk objects ready for ChromaDB insertion.
    """
    if config is None:
        config = ChunkingConfig()

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

    for para_text, page_num in paragraphs:
        if _word_count(para_text) <= config.max_words:
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
                    segments.append((sentence, page_num))
                else:
                    words = sentence.split()
                    for i in range(0, len(words), config.max_words):
                        segments.append((" ".join(words[i : i + config.max_words]), page_num))

    # --- Step 3: Accumulate segments into chunks with overlap ---

    chunks: list[Chunk] = []
    current_texts: list[str] = []
    current_word_count = 0
    current_page = segments[0][1] if segments else 1
    current_char_start = 0
    char_position = 0
    overlap_prefix = ""

    for segment_text, page_num in segments:
        segment_words = _word_count(segment_text)

        # Would adding this segment exceed max? → flush current chunk
        if current_texts and (current_word_count + segment_words) > config.max_words:
            chunk_content = " ".join(current_texts)

            # Prepend overlap from previous chunk
            if overlap_prefix:
                chunk_content = overlap_prefix + " " + chunk_content

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

            # Compute overlap for next chunk
            overlap_prefix = _get_overlap_text(chunk_content, config.overlap_words)

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
        chunk_content = " ".join(current_texts)
        if overlap_prefix:
            chunk_content = overlap_prefix + " " + chunk_content

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

    return chunks
