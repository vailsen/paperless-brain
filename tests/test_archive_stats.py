"""Dashboard figures (`services/archive_stats.py`)."""

from datetime import date, datetime

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


def test_vectors_only_for_visible_documents():
    _docs, chunks = _clusters()
    ids, _m = ax.doc_vectors(chunks, {1, 2, 3})
    assert ids == [1, 2, 3]


def test_duplicates():
    docs, chunks = _clusters()
    # 37 is a copy of 1.
    docs.append(_doc(37, TODAY, corr="Allianz", typ="Policy"))
    chunks.append({"metadata": {"paperless_id": 37}, "embedding": chunks[0]["embedding"]})
    ids, mat = ax.doc_vectors(chunks, {d.id for d in docs})
    assert any({a, b} == {1, 37} for a, b, _s in ax.duplicates(docs, ids, mat, threshold=0.995))
