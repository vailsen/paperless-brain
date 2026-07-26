"""Chunking pipeline — pure text logic, no embedding involved."""

from datetime import datetime

import pytest

from services.chunking import (
    ChunkingConfig,
    build_context_prefix,
    chunk_document,
)

# ── context prefix ───────────────────────────────────────────────────────────


def test_prefix_is_empty_when_nothing_is_known():
    """No metadata must yield no prefix, not a dangling 'Dokument: — '."""
    assert build_context_prefix(None, None, None) == ""


def test_prefix_includes_only_the_known_parts():
    assert build_context_prefix("Invoice", None, None) == "Dokument: Invoice — "


def test_prefix_formats_the_date():
    prefix = build_context_prefix("Invoice", "Acme Ltd", datetime(2026, 7, 20))
    assert prefix == "Dokument: Invoice, Acme Ltd, 20.07.2026 — "


# ── chunking ─────────────────────────────────────────────────────────────────


def test_no_pages_yields_no_chunks():
    assert chunk_document([]) == []


def test_blank_pages_yield_no_chunks():
    assert chunk_document(["", "   ", "\n\n"]) == []


def test_single_short_page_is_one_chunk():
    chunks = chunk_document(["A short paragraph of text."])
    assert len(chunks) == 1
    assert chunks[0].chunk_index == 0
    assert chunks[0].page_number == 1


def test_context_prefix_is_prepended_to_every_chunk():
    pages = ["word " * 300, "word " * 300]
    chunks = chunk_document(pages, context_prefix="Dokument: Invoice — ")
    assert len(chunks) > 1
    assert all(c.text.startswith("Dokument: Invoice — ") for c in chunks)


def test_chunks_respect_the_max_word_budget():
    """Oversized chunks defeat the retrieval window they were sized for.

    Emitted chunks carry the overlap prefix on top of the content budget, so
    the bound is max_words + overlap_words (no context_prefix here).
    """
    config = ChunkingConfig(target_words=50, max_words=60, overlap_words=10)
    chunks = chunk_document(["word " * 500], config=config)
    assert chunks
    bound = config.max_words + config.overlap_words
    assert all(c.word_count <= bound for c in chunks), [c.word_count for c in chunks]


def test_unpunctuated_text_is_still_split():
    """Regression: text with no [.!?] boundaries used to become ONE chunk.

    Sentence splitting keys on punctuation + capital letter. OCR'd scans,
    address blocks and table dumps often have neither, so the whole page stayed
    a single segment and the e5 512-token window silently truncated the tail —
    that content never reached the search index.
    """
    config = ChunkingConfig(target_words=50, max_words=60, overlap_words=10)
    chunks = chunk_document(["word " * 2000], config=config)
    assert len(chunks) > 1
    bound = config.max_words + config.overlap_words
    assert max(c.word_count for c in chunks) <= bound


def test_unpunctuated_text_loses_no_words():
    """Splitting must partition the text, not drop the remainder."""
    words = [f"w{i}" for i in range(500)]
    chunks = chunk_document(
        [" ".join(words)], config=ChunkingConfig(max_words=60, overlap_words=0)
    )
    joined = " ".join(c.text for c in chunks).split()
    assert set(words) <= set(joined), "words lost during hard-cap splitting"


def test_chunk_indexes_are_sequential():
    config = ChunkingConfig(target_words=50, max_words=60, overlap_words=10)
    chunks = chunk_document(["word " * 400], config=config)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_page_numbers_are_one_based_and_non_decreasing():
    config = ChunkingConfig(target_words=40, max_words=50, overlap_words=5)
    pages = ["alpha " * 100, "beta " * 100, "gamma " * 100]
    chunks = chunk_document(pages, config=config)
    numbers = [c.page_number for c in chunks]
    assert min(numbers) >= 1
    assert numbers == sorted(numbers)
    assert max(numbers) <= len(pages)


def test_paragraph_content_survives_chunking():
    """Every source paragraph must appear somewhere in the output."""
    pages = ["First paragraph here.\n\nSecond distinct paragraph.\n\nThird one."]
    joined = " ".join(c.text for c in chunk_document(pages))
    for fragment in ("First paragraph", "Second distinct", "Third one"):
        assert fragment in joined


@pytest.mark.parametrize("overlap", [0, 10, 30])
def test_overlap_setting_does_not_break_chunking(overlap):
    config = ChunkingConfig(target_words=50, max_words=60, overlap_words=overlap)
    chunks = chunk_document(["word " * 300], config=config)
    assert chunks
    assert all(c.word_count > 0 for c in chunks)
