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


# ── Duplicates, second step: the same file or the same text ──────────────────

_LETTER = ("Sehr geehrte Damen und Herren, für den Zeitraum vom {d} stellen wir Ihnen folgende "
           "Leistungen in Rechnung. Rechnungsnummer {n}. Betrag {a} EUR. Bitte überweisen Sie den "
           "Betrag innerhalb von 14 Tagen auf das unten genannte Konto. Mit freundlichen Grüßen "
           "Stadtwerke Musterstadt GmbH, Kundennummer 4711-0815.")


def test_monthly_invoices_on_one_template_are_not_duplicates():
    march = _LETTER.format(d="01.03.2026", n="R-2026-0312", a="84,20")
    april = _LETTER.format(d="01.04.2026", n="R-2026-0419", a="91,75")
    assert not ax.same_text(march, april)


def test_a_letter_scanned_twice_is_the_same_text():
    a = _LETTER.format(d="01.03.2026", n="R-2026-0312", a="84,20")
    # OCR of the second scan: a word split differently, one number misread.
    b = a.replace("Leistungen in Rechnung", "Leistungen in  Rech nung").replace("84,20", "84,2O")
    assert ax.same_text(a, b)


def test_no_text_is_no_evidence():
    assert not ax.same_text("", "")
    assert not ax.same_text(_LETTER, "")


def test_confirm_keeps_identical_files_and_same_text_drops_the_rest():
    t1 = _LETTER.format(d="01.03.2026", n="R-1", a="84,20")
    t2 = _LETTER.format(d="01.04.2026", n="R-2", a="91,75")
    pairs = [(1, 2, 0.99), (3, 4, 0.99), (5, 6, 0.98), (7, 8, 0.995)]
    sums = {1: "abc", 2: "abc", 3: "x", 4: "y", 7: "p"}
    texts = {3: t1, 4: t2, 5: t1, 6: t1}
    out = ax.confirm_duplicates(pairs, sums, texts)
    assert [(a, b, k) for a, b, _s, k in out] == [(1, 2, "file"), (5, 6, "text")]
