"""Retrieval score direction.

`relevance_score` is a ChromaDB cosine *distance* — lower is better. It was
rendered verbatim under a "Relevance" heading, so a correctly sorted list read
as "least relevant first". These tests pin the direction so the inversion cannot
come back silently.
"""

from datetime import datetime

from models.paperless_document import PaperlessDocument
from models.result_document import DocumentResult

_NOW = datetime(2026, 1, 1)


def _result(distance):
    doc = PaperlessDocument(
        id=1,
        original_file_name="f.pdf",
        notes=[],
        mime_type="application/pdf",
        title="t",
        tags=[],
        added=_NOW,
        created=_NOW,
        modified=_NOW,
        pdf_url="http://example.invalid/1",
    )
    return DocumentResult(document=doc, relevance_score=distance)


def test_zero_distance_is_full_relevance():
    assert _result(0.0).relevance == 1.0


def test_relevance_falls_as_distance_grows():
    assert _result(0.1).relevance > _result(0.6).relevance


def test_distance_of_one_is_no_relevance():
    assert _result(1.0).relevance == 0.0


def test_relevance_is_clamped_to_unit_range():
    # Chroma cosine distance spans 0..2, so 1 - d can go negative.
    assert _result(1.8).relevance == 0.0
    assert _result(-0.05).relevance == 1.0


def test_no_score_means_no_relevance():
    assert _result(None).relevance is None
    assert not _result(None).is_semantic_result()


def test_ascending_distance_is_descending_relevance():
    """The browser sorts ascending by distance; that must be best-first."""
    distances = [0.05, 0.30, 0.75]
    relevances = [_result(d).relevance for d in distances]
    assert relevances == sorted(relevances, reverse=True)
