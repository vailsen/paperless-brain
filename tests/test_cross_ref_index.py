"""Cross-reference index — normalisation, matching and incremental updates.

The index is what makes `get_related_documents` work: it links an invoice
number on one document to the same number on another. Two things must hold —
label variants of the same reference type collapse together, and trivial values
(bare years, short numbers) never match, or every document sharing "2023" would
appear related to every other.
"""

import json

import pytest

from services.cross_ref_index import (
    CrossRefIndex,
    _is_trivial,
    _normalise,
    _normalise_type,
)


def _sidecar(tmp_dir, doc_id: int, refs: list[dict]) -> None:
    (tmp_dir / f"{doc_id}.json").write_text(
        json.dumps({"paperless_id": doc_id, "cross_refs": refs})
    )


# ── normalisation ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["2023", "1999", "42", "7", "abc", "   x  "])
def test_trivial_values_are_rejected(value):
    """Bare years and short strings would relate every document to every other."""
    assert _is_trivial(value)


@pytest.mark.parametrize("value", ["RE-2023-0815", "12345", "AZ 4 K 1234/23"])
def test_substantive_values_are_kept(value):
    assert not _is_trivial(value)


def test_normalise_is_case_and_whitespace_insensitive():
    assert _normalise("  RE-2023-0815  ") == _normalise("re-2023-0815")
    assert _normalise("AZ   4 K") == "az 4 k"


@pytest.mark.parametrize(
    "a,b",
    [
        ("Rechnungs-Nr.", "Rechnungsnummer"),
        ("RECHNUNGSNR", "rechnungsnummer"),
        ("IdNummer", "Identifikationsnummer"),
        ("Gläubiger-ID", "Gläubigeridentifikationsnummer"),
    ],
)
def test_type_label_variants_collapse(a, b):
    """Same reference type written differently must still match."""
    assert _normalise_type(a) == _normalise_type(b)


def test_distinct_types_do_not_collapse():
    assert _normalise_type("Rechnungsnummer") != _normalise_type("Kundennummer")


# ── build & query ────────────────────────────────────────────────────────────


def test_build_ignores_unreadable_and_foreign_files(tmp_sidecar_dir):
    _sidecar(tmp_sidecar_dir, 1, [{"type": "Rechnungsnummer", "value": "RE-1"}])
    (tmp_sidecar_dir / "index.json").write_text("{}")
    (tmp_sidecar_dir / "broken.json").write_text("{not json")
    (tmp_sidecar_dir / "notes.txt").write_text("ignored")

    idx = CrossRefIndex()
    idx.build(str(tmp_sidecar_dir))  # must not raise
    assert idx.find_by_value("RE-1") == {1}


def test_documents_sharing_a_reference_are_related(tmp_sidecar_dir):
    _sidecar(tmp_sidecar_dir, 1, [{"type": "Rechnungsnummer", "value": "RE-2023-0815"}])
    _sidecar(tmp_sidecar_dir, 2, [{"type": "Rechnungs-Nr.", "value": "re-2023-0815"}])
    idx = CrossRefIndex()
    idx.build(str(tmp_sidecar_dir))

    related = idx.get_related(1)
    assert len(related) == 1
    assert related[0]["matching_ids"] == [2], "label variant + case must still match"
    assert idx.has_related(1)


def test_same_value_under_a_different_type_does_not_match(tmp_sidecar_dir):
    """A customer number equal to an invoice number is a coincidence, not a link."""
    _sidecar(tmp_sidecar_dir, 1, [{"type": "Rechnungsnummer", "value": "12345"}])
    _sidecar(tmp_sidecar_dir, 2, [{"type": "Kundennummer", "value": "12345"}])
    idx = CrossRefIndex()
    idx.build(str(tmp_sidecar_dir))
    assert idx.get_related(1)[0]["matching_ids"] == []


def test_trivial_values_are_listed_but_never_match(tmp_sidecar_dir):
    _sidecar(tmp_sidecar_dir, 1, [{"type": "Jahr", "value": "2023"}])
    _sidecar(tmp_sidecar_dir, 2, [{"type": "Jahr", "value": "2023"}])
    idx = CrossRefIndex()
    idx.build(str(tmp_sidecar_dir))

    related = idx.get_related(1)
    assert related[0]["value"] == "2023", "raw ref still shown in the dialog"
    assert related[0]["matching_ids"] == []
    assert not idx.has_related(1)
    assert idx.find_by_value("2023") == set()


def test_a_document_never_relates_to_itself(tmp_sidecar_dir):
    _sidecar(tmp_sidecar_dir, 1, [
        {"type": "Rechnungsnummer", "value": "RE-1"},
        {"type": "Rechnungsnummer", "value": "RE-1"},
    ])
    idx = CrossRefIndex()
    idx.build(str(tmp_sidecar_dir))
    assert all(r["matching_ids"] == [] for r in idx.get_related(1))


def test_unknown_document_returns_empty(tmp_sidecar_dir):
    idx = CrossRefIndex()
    idx.build(str(tmp_sidecar_dir))
    assert idx.get_related(999) == []
    assert not idx.has_related(999)


def test_find_by_value_handles_blank_input(tmp_sidecar_dir):
    idx = CrossRefIndex()
    idx.build(str(tmp_sidecar_dir))
    assert idx.find_by_value("") == set()
    assert idx.find_by_value("   ") == set()


# ── incremental updates ──────────────────────────────────────────────────────


def test_add_document_links_to_existing(tmp_sidecar_dir):
    _sidecar(tmp_sidecar_dir, 1, [{"type": "Rechnungsnummer", "value": "RE-9"}])
    idx = CrossRefIndex()
    idx.build(str(tmp_sidecar_dir))

    idx.add_document(2, [{"type": "Rechnungsnummer", "value": "RE-9"}])
    assert idx.get_related(1)[0]["matching_ids"] == [2]
    assert idx.find_by_value("RE-9") == {1, 2}


def test_add_document_replaces_previous_entries(tmp_sidecar_dir):
    """Re-ingesting a document must not leave its old references behind."""
    idx = CrossRefIndex()
    idx.build(str(tmp_sidecar_dir))
    idx.add_document(1, [{"type": "Rechnungsnummer", "value": "OLD-1"}])
    idx.add_document(1, [{"type": "Rechnungsnummer", "value": "NEW-1"}])

    assert idx.find_by_value("OLD-1") == set()
    assert idx.find_by_value("NEW-1") == {1}


def test_remove_document_clears_its_references(tmp_sidecar_dir):
    idx = CrossRefIndex()
    idx.build(str(tmp_sidecar_dir))
    idx.add_document(1, [{"type": "Rechnungsnummer", "value": "RE-7"}])
    idx.add_document(2, [{"type": "Rechnungsnummer", "value": "RE-7"}])

    idx.remove_document(1)
    assert idx.find_by_value("RE-7") == {2}
    assert idx.get_related(2)[0]["matching_ids"] == []


def test_remove_unknown_document_is_a_noop(tmp_sidecar_dir):
    idx = CrossRefIndex()
    idx.build(str(tmp_sidecar_dir))
    idx.remove_document(12345)  # must not raise
