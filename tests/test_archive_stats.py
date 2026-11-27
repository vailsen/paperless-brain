"""Dashboard figures (`services/archive_stats.py`)."""

from datetime import date, datetime, timedelta

import numpy as np

from models.paperless_document import PaperlessDocument
from services import archive_stats as ax

TODAY = date(2026, 11, 27)


def _doc(i, created, corr="Stadtwerke", typ="Invoice", tags=(), pages=1):
    return PaperlessDocument.model_construct(
        id=i, title=f"d{i}", correspondent=corr, document_type=typ, tags=list(tags), page_count=pages,
        created=datetime.combine(created, datetime.min.time()), added=datetime.combine(created, datetime.min.time()))


def test_over_time_counts_months():
    docs = [_doc(1, date(2026, 11, 2)), _doc(2, date(2026, 11, 20)), _doc(3, date(2026, 9, 1), typ="Letter")]
    r = ax.over_time(docs, months=3, today=TODAY)
    assert r == {"months": ["2026-09", "2026-10", "2026-11"], "counts": [1, 0, 2]}
    assert ax.over_time(docs, months=3, today=TODAY, doc_type="Letter")["counts"] == [1, 0, 0]


def test_a_yearly_bill_that_did_not_come_is_expected():
    docs = [_doc(i, date(2022 + i, 2, 14)) for i in range(4)]  # Feb 2022 … Feb 2025
    out = ax.expected_missing(docs, today=TODAY)
    assert len(out) == 1 and out[0].cadence == "yearly"
    assert out[0].last == date(2025, 2, 14) and out[0].expected == date(2026, 2, 14)
    assert ax.expected_missing(docs, today=TODAY, dismissed={"Stadtwerke|Invoice"}) == []


def test_a_series_on_time_or_long_ended_is_not_listed():
    on_time = [_doc(i, date(2026, 11, 20) - timedelta(days=30 * i)) for i in range(5)]
    assert ax.expected_missing(on_time, today=TODAY) == []
    ended = [_doc(i, date(2020, 1, 15) + timedelta(days=30 * i)) for i in range(6)]
    assert ax.expected_missing(ended, today=TODAY) == []


def test_irregular_letters_have_no_rhythm():
    days = [0, 9, 70, 75, 200, 340]
    docs = [_doc(i, date(2025, 1, 1) + timedelta(days=d)) for i, d in enumerate(days)]
    assert ax.expected_missing(docs, today=TODAY) == []


def test_health():
    docs = [_doc(1, TODAY, corr=None), _doc(2, TODAY, typ=None, tags=["x"]), _doc(3, TODAY, tags=["y"])]
    assert ax.health(docs) == {"no_correspondent": 1, "no_type": 1, "no_tags": 1}


def _clusters():
    rng = np.random.default_rng(1)
    docs, chunks = [], []
    centers = {"Allianz": np.eye(8)[0], "Dr. Weber": np.eye(8)[3], "Telekom": np.eye(8)[6]}
    i = 0
    for corr, c in centers.items():
        typ = {"Allianz": "Policy", "Dr. Weber": "Letter", "Telekom": "Invoice"}[corr]
        for _ in range(12):
            i += 1
            docs.append(_doc(i, TODAY, corr=corr, typ=typ))
            for _k in range(2):
                chunks.append({"metadata": {"paperless_id": i}, "embedding": (c + rng.normal(0, .05, 8)).tolist()})
    return docs, chunks


def test_topics_are_labelled_from_their_documents():
    docs, chunks = _clusters()
    ids, mat = ax.doc_vectors(chunks, {d.id for d in docs})
    tops = ax.topics(docs, ids, mat)
    assert sorted(t.label for t in tops) == ["Allianz · Policy", "Dr. Weber · Letter", "Telekom · Invoice"]
    assert all(len(t.ids) == 12 for t in tops)


def test_vectors_only_for_visible_documents():
    docs, chunks = _clusters()
    ids, _m = ax.doc_vectors(chunks, {1, 2, 3})
    assert ids == [1, 2, 3]


def test_duplicates_and_outliers():
    docs, chunks = _clusters()
    # 37 is a copy of 1; 38 sits among the Telekom invoices but is typed Policy.
    docs.append(_doc(37, TODAY, corr="Allianz", typ="Policy"))
    chunks.append({"metadata": {"paperless_id": 37}, "embedding": chunks[0]["embedding"]})
    docs.append(_doc(38, TODAY, corr="Telekom", typ="Policy"))
    chunks.append({"metadata": {"paperless_id": 38}, "embedding": (np.eye(8)[6]).tolist()})
    ids, mat = ax.doc_vectors(chunks, {d.id for d in docs})
    assert any({a, b} == {1, 37} for a, b, _s in ax.duplicates(docs, ids, mat, threshold=0.995))
    outs = ax.outliers(docs, ids, mat)
    assert any(o.doc_id == 38 and o.field == "document_type" and o.neighbours_say == "Invoice" for o in outs)
