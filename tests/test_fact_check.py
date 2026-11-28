"""Hard facts in chat answers, checked against their sources (`services/fact_check.py`)."""

import pytest

from services import fact_check as fc


def _found(fact_text: str, page: str, kind: str | None = None) -> bool:
    f = next((x for x in fc.facts(fact_text, hard_only=False) if x.text == fact_text.strip()), None)
    f = f or fc.Fact(0, len(fact_text), fact_text, kind or "code")
    return bool(fc.find(f, page))


# ── What counts as a hard fact ───────────────────────────────────────────────

def test_hard_facts_and_what_is_left_alone():
    text = ("Der Betrag von 1.240,00 € ist am 14. März 2026 fällig.[^3-p1] Die IBAN lautet "
            "DE89 3704 0044 0532 0130 00, die Rechnung R-2026-0312 (siehe Seite 5, #241). "
            "Im Jahr 2026 gab es 12 Zahlungen. Zins: 4,5 %. "
            "Link [x](https://ex.com/2026/12345) und `code 99999`.")
    got = [(f.text, f.kind) for f in fc.facts(text)]
    assert got == [("1.240,00", "number"), ("14. März 2026", "date"),
                   ("DE89 3704 0044 0532 0130 00", "code"), ("R-2026-0312", "code"), ("4,5", "number")]


def test_two_numbers_side_by_side_stay_two():
    assert [f.text for f in fc.facts("am 14.03.2026 1.240,00 EUR")] == ["14.03.2026", "1.240,00"]


# ── Spaces, separators, case ─────────────────────────────────────────────────

@pytest.mark.parametrize("answer,page", [
    ("DE89 3704 0044 0532 0130 00", "IBAN: de89370400440532013000"),
    ("DE89370400440532013000", "IBAN DE89 3704 0044 0532 0130 00 BIC"),
    ("VR1000631423", "Vertrag VR 1000631423 vom"),
    ("R-2026-0312", "Re.-Nr. R 2026/0312"),
    ("AB-102C", "Kundennummer ab 102c"),
    ("AB-1O2C", "Kundennummer ab 102c"),          # OCR: letter O for zero
])
def test_ids_ignore_spaces_separators_and_case(answer, page):
    assert _found(answer, page)


def test_an_id_is_never_a_piece_of_a_longer_one():
    assert not _found("AB-102", "Kundennummer AB-1029")
    assert not _found("1000631423", "VR 10006314231")


# ── Numbers: dot/comma, thousands, currency ──────────────────────────────────

@pytest.mark.parametrize("answer,page", [
    ("1.240,00", "Betrag EUR 1,240.00"),
    ("1,240.00", "Betrag 1.240,00 €"),
    ("1240", "Betrag 1.240,00 €"),
    ("1.240", "Summe: 1240,00"),
    ("1 240,00", "Summe 1.240,00"),
    ("4,5", "Zinssatz 4.5%"),
    ("12.345.678", "12,345,678 Kunden"),
])
def test_numbers_compare_by_value(answer, page):
    assert _found(answer, page, "number")


def test_a_number_is_not_found_inside_another():
    assert not _found("240", "Betrag 1.240,00", "number")
    assert not _found("1.240,50", "Betrag 1.240,00", "number")


def test_a_long_number_inside_an_id_counts():
    assert _found("4711", "Kd-Nr. 4711-0815", "number")
    assert not _found("4711", "Kd-Nr. 47110815", "number")


# ── Dates ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("answer,page", [
    ("14.03.2026", "fällig am 2026-03-14"),
    ("2026-03-14", "fällig am 14.03.2026"),
    ("14. März 2026", "Datum: 14.3.2026"),
    ("March 14, 2026", "Datum 14.03.26"),
    ("14.03.2026", "due 14 March 2026"),
])
def test_dates_compare_by_day(answer, page):
    assert _found(answer, page, "date")


def test_a_different_day_is_no_match():
    assert not _found("14.03.2026", "fällig am 15.03.2026", "date")


# ── An answer against its sources ────────────────────────────────────────────

PAGES = {
    3: [(1, "Rechnung R-2026-0312 vom 2026-03-01"), (2, "Gesamtbetrag EUR 1,240.00, fällig 14.03.2026")],
    7: [(1, "Kontoauszug 99,90 €")],
}


def _pages(doc):
    return PAGES.get(doc, [])


def test_verify_marks_facts_found_in_cited_sources():
    text = ("Die Rechnung über 1.240,00 € ist am 14. März 2026 fällig.[^3-p1]\n"
            "Abgebucht wurden 99,90 €.[^7-p1] Unbelegt: 555,55 €.")
    got = [(v.text, v.doc, v.page) for v in fc.verify(text, {3, 7}, _pages)]
    # Cited p1, found on p2 of the same document; 555,55 is in no source.
    assert got == [("1.240,00", 3, 2), ("14. März 2026", 3, 2), ("99,90", 7, 1)]


def test_verify_reads_only_documents_the_user_was_shown():
    text = "Die Rechnung über 1.240,00 € ist fällig.[^3-p2]"
    assert fc.verify(text, set(), _pages) == []
    assert fc.verify(text, {7}, _pages) == []


def test_verify_takes_a_table_rows_citation():
    text = "| Posten | Betrag | Quelle |\n|---|---|---|\n| Rechnung | 1.240,00 € | [^3-p2] |"
    assert [(v.text, v.page) for v in fc.verify(text, {3}, _pages)] == [("1.240,00", 2)]


def test_verify_without_any_citation_marks_nothing():
    assert fc.verify("1.240,00 € laut Rechnung.", {3}, _pages) == []


def test_word_spans_find_the_fact_on_the_text_layer():
    words = ["Gesamtbetrag", "EUR", "1,240.00", "fällig", "14.03.2026"]
    assert fc.word_spans("1.240,00", words) == [2]
    assert fc.word_spans("14. März 2026", words) == [4]
    iban = ["IBAN", "DE89", "3704", "0044", "0532", "0130", "00", "BIC"]
    assert fc.word_spans("DE89370400440532013000", iban) == [1, 2, 3, 4, 5, 6]
    assert fc.word_spans("555,55", words) == []
