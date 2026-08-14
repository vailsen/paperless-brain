"""Lossless note text handling.

The load-bearing property is that reading a note and writing it back without
edits produces the same bytes. Without it, debounced autosave rewrites every
note the moment it is opened — dirtying git and forcing a re-embed on the next
sync for a file nobody changed.
"""

import pytest

from vault.note_text import (
    NoteText,
    apply_edits,
    blocks_are_faithful,
    join_note,
    properties,
    raw_blocks,
    split_note,
    try_merge,
)

# ── Round trip ────────────────────────────────────────────────────────────────

ROUND_TRIP_CASES = {
    "plain body": "Just a note.\n",
    "empty file": "",
    "no trailing newline": "One line",
    "frontmatter only": "---\ntitle: X\n---\n",
    "typical note": "---\npbrain_id: abc\ntags:\n- a\n- b\n---\n\n# Heading\n\nText.\n",
    "empty frontmatter": "---\n---\nBody.\n",
    "crlf": "---\r\ntitle: X\r\n---\r\nBody.\r\n",
    "hr in body": "---\ntitle: X\n---\n\nAbove\n\n---\n\nBelow\n",
    "body starts blank": "---\ntitle: X\n---\n\n\nBody.\n",
    "block scalar": "---\nnote: |\n  line one\n  line two\ntitle: X\n---\nBody\n",
    "comments": "---\n# why this exists\ntitle: X\n\n# trailing note\n---\nBody\n",
    "nested map": "---\nmeta:\n  a: 1\n  b: 2\ntitle: X\n---\nBody\n",
    "leading hr no frontmatter": "---\n\nnot frontmatter, never closed\n",
    "document end marker": "---\ntitle: X\n...\nBody\n",
    "indented list style": "---\ntags:\n  - a\n  - b\n---\nBody\n",
    "unicode": "---\ntitle: Mülltütengrößen\n---\nGrüße\n",
}


@pytest.mark.parametrize("text", ROUND_TRIP_CASES.values(), ids=list(ROUND_TRIP_CASES))
def test_split_join_is_byte_identical(text):
    assert join_note(split_note(text)) == text


def test_unterminated_fence_is_body_not_frontmatter():
    note = split_note("---\nnever closed\n")
    assert note.fm_raw is None
    assert properties(note.fm_raw) == {}


def test_empty_frontmatter_parses_to_empty_dict():
    note = split_note("---\n---\nBody.\n")
    assert note.fm_raw == ""
    assert properties(note.fm_raw) == {}


def test_broken_yaml_does_not_raise():
    assert properties("title: [unclosed\n") == {}


# ── Verbatim splicing ─────────────────────────────────────────────────────────

FANCY = (
    "---\n"
    "pbrain_id: a87def7c-39bd-434b-99d1-6c2e7108b808\n"
    "created: 2026-08-10T13:14:47.587338\n"
    "# the leading zeros matter\n"
    "code: 007\n"
    "ratio: 1.50\n"
    "tags:\n"
    "- alt\n"
    "---\n"
    "Body stays put.\n"
)


def test_editing_one_key_leaves_every_other_byte_untouched():
    fm = split_note(FANCY).fm_raw
    out = apply_edits(fm, set_={"tags": ["neu"]})
    assert "created: 2026-08-10T13:14:47.587338\n" in out
    assert "code: 007\n" in out
    assert "ratio: 1.50\n" in out
    assert "# the leading zeros matter\n" in out
    assert "tags:\n- neu\n" in out
    assert "- alt" not in out


def test_no_edits_returns_the_source_unchanged():
    fm = split_note(FANCY).fm_raw
    assert apply_edits(fm) == fm


def test_comment_above_an_edited_key_survives():
    fm = split_note(FANCY).fm_raw
    out = apply_edits(fm, set_={"code": 8})
    assert "# the leading zeros matter\ncode: 8\n" in out


def test_new_key_is_appended_and_key_order_is_preserved():
    fm = split_note(FANCY).fm_raw
    out = apply_edits(fm, set_={"dont_ingest": False})
    keys = list(raw_blocks(out))
    assert keys == ["pbrain_id", "created", "code", "ratio", "tags", "dont_ingest"]
    assert properties(out)["dont_ingest"] is False


def test_removing_the_last_key_yields_no_frontmatter_block():
    assert apply_edits("title: X\n", remove=["title"]) is None


def test_removing_one_of_several_keeps_the_rest():
    out = apply_edits(split_note(FANCY).fm_raw, remove=["ratio"])
    assert "ratio" not in out
    assert "code: 007\n" in out


def test_nested_map_is_spliced_not_flattened():
    fm = "meta:\n  a: 1\n  b: 2\ntitle: X\n"
    out = apply_edits(fm, set_={"title": "Y"})
    assert "meta:\n  a: 1\n  b: 2\n" in out
    assert properties(out) == {"meta": {"a": 1, "b": 2}, "title": "Y"}


def test_block_scalar_is_preserved_verbatim():
    fm = "note: |\n  line one\n  line two\ntitle: X\n"
    out = apply_edits(fm, set_={"title": "Y"})
    assert "note: |\n  line one\n  line two\n" in out


def test_faithfulness_detects_yaml_the_scanner_cannot_splice():
    assert blocks_are_faithful("a: 1\nb: 2\n")
    assert not blocks_are_faithful("{a: 1, b: 2}\n")


def test_unsplicable_yaml_falls_back_to_a_full_redump():
    out = apply_edits("{a: 1, b: 2}\n", set_={"c": 3})
    assert properties(out) == {"a": 1, "b": 2, "c": 3}


# ── Three-way merge ───────────────────────────────────────────────────────────

BASE = "---\ntitle: X\n---\nHello\n"


def test_sync_injecting_pbrain_id_while_the_user_types_merges_silently():
    ours = "---\ntitle: X\n---\nHello world\n"
    theirs = "---\npbrain_id: abc\ntitle: X\n---\nHello\n"
    merged = try_merge(BASE, ours, theirs)
    assert merged is not None
    assert properties(split_note(merged).fm_raw)["pbrain_id"] == "abc"
    assert split_note(merged).body == "Hello world\n"


def test_disjoint_property_edits_merge():
    ours = "---\ntitle: X\ntags:\n- a\n---\nHello\n"
    theirs = "---\ntitle: X\ndont_ingest: true\n---\nHello\n"
    merged = try_merge(BASE, ours, theirs)
    props = properties(split_note(merged).fm_raw)
    assert props["tags"] == ["a"] and props["dont_ingest"] is True


def test_body_only_edit_against_property_only_edit_merges():
    ours = "---\ntitle: X\n---\nRewritten\n"
    theirs = "---\ntitle: X\ndont_ingest: false\n---\nHello\n"
    merged = try_merge(BASE, ours, theirs)
    assert split_note(merged).body == "Rewritten\n"
    assert properties(split_note(merged).fm_raw)["dont_ingest"] is False


def test_property_removal_on_our_side_is_carried_over():
    base = "---\ntitle: X\ntmp: 1\n---\nHello\n"
    ours = "---\ntitle: X\n---\nHello\n"
    theirs = "---\ntitle: X\ntmp: 1\npbrain_id: abc\n---\nHello\n"
    merged = try_merge(base, ours, theirs)
    props = properties(split_note(merged).fm_raw)
    assert "tmp" not in props and props["pbrain_id"] == "abc"


def test_conflicting_body_edits_do_not_merge():
    assert try_merge(BASE, "---\ntitle: X\n---\nMine\n", "---\ntitle: X\n---\nTheirs\n") is None


def test_conflicting_property_edits_do_not_merge():
    ours = "---\ntitle: Mine\n---\nHello\n"
    theirs = "---\ntitle: Theirs\n---\nHello\n"
    assert try_merge(BASE, ours, theirs) is None


def test_identical_edits_on_both_sides_are_not_a_conflict():
    same = "---\ntitle: Y\n---\nHello\n"
    assert try_merge(BASE, same, same) == same


def test_merge_onto_a_note_that_gained_its_first_frontmatter():
    base = "Hello\n"
    ours = "Hello world\n"
    theirs = "---\npbrain_id: abc\n---\nHello\n"
    merged = try_merge(base, ours, theirs)
    assert merged == "---\npbrain_id: abc\n---\nHello world\n"


def test_merge_preserves_untouched_scalar_formatting():
    base = FANCY
    ours = FANCY.replace("Body stays put.", "Body edited.")
    theirs = FANCY.replace("tags:\n- alt\n", "tags:\n- alt\ndont_ingest: false\n")
    merged = try_merge(base, ours, theirs)
    assert "code: 007\n" in merged
    assert "created: 2026-08-10T13:14:47.587338\n" in merged
    assert split_note(merged).body == "Body edited.\n"


def test_note_text_carries_crlf_fences_through_an_edit():
    note = split_note("---\r\ntitle: X\r\n---\r\nBody\r\n")
    edited = join_note(NoteText(
        fm_raw=apply_edits(note.fm_raw, set_={"title": "Y"}),
        body=note.body,
        open_fence=note.open_fence,
        close_fence=note.close_fence,
    ))
    assert edited.startswith("---\r\n")
    assert edited.endswith("---\r\nBody\r\n")
