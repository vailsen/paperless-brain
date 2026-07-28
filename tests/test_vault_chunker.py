"""Vault markdown chunking — heading structure and the size cap.

The cap is the load-bearing part: multilingual-e5-large-instruct truncates at a
512-token window, so any chunk that exceeds the word budget is silently cut when
embedded and its tail never becomes searchable. A single 2000-word paragraph
used to produce exactly one 2000-word chunk.
"""

from vault.chunker import chunk_vault_file

MAX = 400


def _words(text: str) -> int:
    return len(text.split())


def test_empty_body_yields_nothing():
    assert chunk_vault_file("") == []
    assert chunk_vault_file("   \n\n  ") == []


def test_small_section_is_one_chunk():
    chunks = chunk_vault_file("# Notes\n\nSomething short.\n")
    assert len(chunks) == 1
    assert chunks[0].text == "Something short."
    assert chunks[0].heading_path == "Notes"


def test_heading_path_tracks_the_hierarchy():
    md = (
        "# Architecture\n\nintro\n\n"
        "## Sync\n\nabout sync\n\n"
        "### Triggers\n\nabout triggers\n\n"
        "## Storage\n\nabout storage\n"
    )
    paths = [c.heading_path for c in chunk_vault_file(md)]
    assert paths == [
        "Architecture",
        "Architecture > Sync",
        "Architecture > Sync > Triggers",
        "Architecture > Storage",
    ]


def test_text_before_the_first_heading_has_an_empty_path():
    chunks = chunk_vault_file("loose intro line\n\n# Later\n\nbody\n")
    assert chunks[0].heading_path == ""
    assert chunks[0].text == "loose intro line"


def test_chunk_index_is_global_across_sections():
    md = "# A\n\nalpha\n\n# B\n\nbeta\n\n# C\n\ngamma\n"
    assert [c.chunk_index for c in chunk_vault_file(md)] == [0, 1, 2]


# ── The cap ───────────────────────────────────────────────────────────────────


def test_single_oversized_paragraph_is_split():
    """The regression: one unbroken wall of text was one oversized chunk."""
    md = "# A\n\n" + " ".join(["word"] * 2000) + "\n"
    chunks = chunk_vault_file(md, max_words=MAX)
    assert len(chunks) == 5
    assert all(_words(c.text) <= MAX for c in chunks)


def test_no_chunk_ever_exceeds_the_budget():
    md = (
        "# Long\n\n"
        + " ".join(["alpha"] * 1500)
        + "\n\n"
        + "\n\n".join(" ".join(["beta"] * 250) for _ in range(4))
        + "\n\n## Short\n\ntail\n"
    )
    assert all(_words(c.text) <= MAX for c in chunk_vault_file(md, max_words=MAX))


def test_oversized_paragraph_splits_on_line_boundaries():
    """Markdown paragraphs are line-structured — a table row must survive whole."""
    rows = ["| cell a{0} | cell b{0} | cell c{0} |".format(i) for i in range(300)]
    chunks = chunk_vault_file("# T\n\n" + "\n".join(rows) + "\n", max_words=MAX)
    assert len(chunks) > 1
    emitted = [line for c in chunks for line in c.text.split("\n")]
    assert emitted == rows  # every row intact, in order, none lost or duplicated


def test_a_single_line_longer_than_the_budget_is_cut_on_words():
    """Last resort: one line with no internal structure still has to fit."""
    md = "# A\n\n" + " ".join(["x"] * 900) + "\n"
    chunks = chunk_vault_file(md, max_words=MAX)
    assert [_words(c.text) for c in chunks] == [400, 400, 100]


def test_splitting_loses_no_words():
    body = " ".join(f"w{i}" for i in range(1300))
    chunks = chunk_vault_file("# A\n\n" + body + "\n", max_words=MAX)
    assert " ".join(c.text for c in chunks).split() == body.split()


def test_paragraph_grouping_is_unchanged_below_the_cap():
    """Paragraphs that fit are still packed together, not split per paragraph."""
    md = "# A\n\n" + "\n\n".join(" ".join(["w"] * 120) for _ in range(5)) + "\n"
    chunks = chunk_vault_file(md, max_words=MAX)
    assert [_words(c.text) for c in chunks] == [360, 240]
    assert "\n\n" in chunks[0].text  # paragraph breaks preserved
