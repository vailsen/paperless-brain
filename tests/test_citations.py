"""Citations in chat answers: markers, passage choice, PDF boxes, rendering."""

import pytest

from services import citations
from services.citations import (
    align_words,
    best_page,
    best_passage,
    best_pictures,
    cite_ref,
    document_page,
    number_citations,
    page_for,
    split_sentences,
    pdf_words,
    plain_citations,
)

PAGE = """Gebäudeenergiegesetz — Hinweise für Bauherren
Bei Neubauten muss der Wärmebedarf zu mindestens 65 % aus erneuerbaren Energien gedeckt werden. Das gilt ab 2024.
Ausnahmen regelt § 102 GEG.
Kontakt: Stadtwerke Bretten, Telefon 07252 913-0"""


# ── Markers ──────────────────────────────────────────────────────────────────


def test_cite_ref_is_what_the_regex_reads_back():
    for ref in (cite_ref(241, 3), cite_ref(241)):
        m = citations.CITE_RE.fullmatch(ref)
        assert m and m.group(1) == "241"
    assert cite_ref(241, 3) == "[^241-p3]"
    assert cite_ref(241) == "[^241]"


def test_numbers_follow_first_use_and_repeat_for_the_same_source():
    text = "A.[^241-p3] B.[^255][^241-p3] C.[^241-p4]"
    out, sources = number_citations(text, lambda n, d, p: f"<{n}>")
    assert out == "A.<1> B.<2><1> C.<3>"
    assert sources == [(1, 241, 3), (2, 255, 0), (3, 241, 4)]


def test_a_marker_doubled_by_the_model_shows_once():
    out, _ = number_citations("A.[^241-p3][^241-p3]", lambda n, d, p: f"<{n}>")
    assert out == "A.<1>"


@pytest.mark.parametrize(
    "variant", ["[^#241 p.3]", "[^241, S. 3]", "[^241:p3]", "[^241 - page 3]", "[^ 241-p3 ]"]
)
def test_half_remembered_formats_still_count(variant):
    _, sources = number_citations(f"A.{variant}", lambda n, d, p: "")
    assert sources == [(1, 241, 3)]


def test_a_doubled_caret_goes_with_the_marker():
    out, sources = number_citations("343,56 € (66,085 cbm)^[^577-p1]", lambda n, d, p: f"<{n}>")
    assert out == "343,56 € (66,085 cbm)<1>"
    assert sources == [(1, 577, 1)]


def test_a_caret_only_outside_is_a_marker_too():
    # Pandoc's inline-footnote form, written by a model live.
    out, sources = number_citations("auf Lebenszeit.^[435-p1] Und.^[434]", lambda n, d, p: f"<{n}>")
    assert out == "auf Lebenszeit.<1> Und.<2>"
    assert sources == [(1, 435, 1), (2, 434, 0)]


def test_a_caret_bracket_without_a_number_is_left_alone():
    out, sources = number_citations("x^[note] y", lambda n, d, p: "<>")
    assert out == "x^[note] y" and sources == []


def test_footnote_style_text_is_not_a_citation():
    _, sources = number_citations("see [^note] and [^a1]", lambda n, d, p: "")
    assert sources == []


def test_plain_citations_for_pdf():
    out = plain_citations("A.[^241-p3] B.[^255]", "S. {n}")
    assert "A.<sup>1</sup> B.<sup>2</sup>" in out
    assert "<sup>1</sup> #241 · S. 3" in out
    assert out.rstrip().endswith("<sup>2</sup> #255")
    assert plain_citations("no sources") == "no sources"


# ── Passage choice ───────────────────────────────────────────────────────────


def test_passage_is_the_sentence_sharing_the_claims_facts():
    passage, score = best_passage(
        PAGE, "Euer Haus in Bretten muss mindestens 65 % erneuerbare Energie nutzen."
    )
    assert passage == (
        "Bei Neubauten muss der Wärmebedarf zu mindestens 65 % aus erneuerbaren "
        "Energien gedeckt werden."
    )
    assert score > 0


def test_numbers_decide_between_similar_lines():
    page = "Grenzwert Altbau: 15 % erneuerbare Energie\nGrenzwert Neubau: 65 % erneuerbare Energie"
    assert best_passage(page, "Für den Neubau gelten 65 %.")[0] == (
        "Grenzwert Neubau: 65 % erneuerbare Energie"
    )


def test_amounts_match_across_number_formats():
    page = "Zwischensumme: 980,00 EUR\nGesamtbetrag: 1.014,01 EUR"
    assert best_passage(page, "The total is 1014.01 EUR.")[0] == "Gesamtbetrag: 1.014,01 EUR"


def test_an_amount_matches_with_trailing_decimal_zeros():
    # The sidecar keeps the document's "176,000 cbm"; the answer says "176 m³".
    page = "Abrechnungsgebühr Wasser: 26,78 EUR\nZu- u. Abwasser: 914,97 EUR : 176,000 cbm"
    claim = "Euer Wasserverbrauch (Zu- und Abwasser) lag bei 176 m³."
    assert best_passage(page, claim)[0] == "Zu- u. Abwasser: 914,97 EUR : 176,000 cbm"


def test_claim_in_another_language_falls_back_to_the_hint_chunk():
    hint = "Dokument: Merkblatt, Stadt — Ausnahmen regelt § 102 GEG."
    passage, score = best_passage(PAGE, "Exceptions are regulated elsewhere.", [hint])
    assert passage == "Ausnahmen regelt § 102 GEG."
    assert score == 0.0


def test_nothing_in_common_and_no_hint_means_no_highlight():
    assert best_passage(PAGE, "The weather was nice.") == ("", 0.0)
    assert best_passage("", "65 %") == ("", 0.0)


def test_unrelated_lines_are_never_bridged():
    passage, _ = best_passage(PAGE, "Mindestens 65 % erneuerbar, in Bretten.")
    assert "Das gilt ab 2024" not in passage
    assert "Kontakt" not in passage


def test_document_level_citation_picks_the_page():
    pages = [(1, "Deckblatt\nInhaltsverzeichnis"), (2, PAGE), (3, "Anhang")]
    assert best_page(pages, "Mindestens 65 % aus erneuerbaren Energien.") == 2
    assert best_page(pages, "Nothing related.") == 1


def test_a_page_number_is_not_what_a_claim_is_about():
    """Seen live: "Seite 5: Vier Lateralradiogramme …" marked the footer
    "Seite 5 von 5" — the 5 alone, counting triple."""
    page = "Verlauf Gleitwirbel\n09.01.2025 – 2,5 Monate nach 2. OP\nSeite 5 von 5"
    claim = "Seite 5: Vier Lateralradiogramme mit Messwerten (10,92 mm)"
    assert "Seite 5 von 5" not in best_passage(page, claim)[0]


def test_a_number_without_a_page_is_one_page_whichever_sentence_is_tapped():
    pages = [(1, "Deckblatt"), (2, PAGE), (3, "Anhang: Stadtwerke Bretten")]
    claims = ["Mindestens 65 % aus erneuerbaren Energien.", "Die Stadtwerke Bretten."]
    picked = {document_page(pages, {}, [c, *[o for o in claims if o != c]]) for c in claims}
    assert picked == {2}


def test_a_fact_gets_the_page_it_is_on_when_one_page_clearly_wins():
    pages = [(1, "Erstdiagnose 15.11.2022"), (2, "Nach 1. OP 12.04.2024"), (3, "Seite 3 von 3")]
    assert page_for(pages, "Dr. Möldner stellte am 15.11.2022 die Erstdiagnose.") == 1
    assert page_for(pages, "RZ-59211") is None  # nowhere
    assert page_for(pages + [(4, "Erstdiagnose 15.11.2022")], "Erstdiagnose 15.11.2022") is None


def test_a_title_abbreviation_does_not_end_the_sentence():
    assert split_sentences("Für die Dr. Möldner die Diagnose stellte. Danach OP.") == [
        "Für die Dr. Möldner die Diagnose stellte.",
        "Danach OP.",
    ]


XRAYS = [
    ("Implantatkontrolle, 08.01.2025 Lateralradiogramm der LWS", (0.05, 0.1, 0.4, 0.4)),
    ("Implantatkontrolle, 31.10.2024 Lateralradiogramm der LWS", (0.55, 0.1, 0.4, 0.4)),
    ("Winkelmessung 10,92 mm, 08.01.2025 Lateralradiogramm", (0.05, 0.5, 0.4, 0.4)),
    ("Winkelmessung 10,48 mm, 31.10.2024 Lateralradiogramm", (0.55, 0.5, 0.4, 0.4)),
]


def test_what_a_picture_shows_is_marked_on_the_picture():
    claim = "Vier Lateralradiogramme (31.10.2024 und 08.01.2025) mit Messwerten (10,92 mm bzw. 10,48 mm)"
    boxes, score, text = best_pictures(XRAYS, claim)
    assert len(boxes) == 4  # "four radiographs" is all four
    assert score > best_passage("Verlauf Gleitwirbel\nSeite 5 von 5", claim)[1]
    assert "Winkelmessung" in text


def test_an_unrelated_picture_is_not_marked():
    assert best_pictures(XRAYS, "Die Rechnung über 120 EUR ist bezahlt.") == ([], 0.0, "")


# ── PDF text layer ───────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def pdf_bytes():
    from weasyprint import HTML

    html = (
        "<html><body style='font-size:12pt'>"
        "<h1>Hinweise für Bauherren</h1>"
        "<p>Einleitung ohne Bezug zur Frage, mit etwas Fülltext davor.</p>"
        "<p>Bei Neubauten muss der Wärmebedarf zu mindestens 65 % aus erneuerbaren "
        "Energien gedeckt werden.</p>"
        "<p>Kontakt: Stadtwerke Bretten</p>"
        "<p style='page-break-before:always'>Seite zwei</p>"
        "</body></html>"
    )
    return HTML(string=html).write_pdf()


def test_pdf_words_have_page_fraction_boxes(pdf_bytes):
    words = pdf_words(pdf_bytes, 1)
    assert any(w.text == "Neubauten" for w in words)
    for w in words:
        x, y, bw, bh = w.box
        assert 0 <= x <= 1 and 0 <= y <= 1 and 0 < bw < 1 and 0 < bh < 1
    assert pdf_words(pdf_bytes, 9) == []


def test_passage_is_found_in_the_pdf_and_boxed_per_line(pdf_bytes):
    words = pdf_words(pdf_bytes, 1)
    boxes = align_words(
        words,
        "Bei Neubauten muss der Wärmebedarf zu mindestens 65 % aus erneuerbaren "
        "Energien gedeckt werden.",
    )
    assert boxes
    neubauten = next(w for w in words if w.text == "Neubauten")
    kontakt = next(w for w in words if w.text.startswith("Kontakt"))
    top = min(b[1] for b in boxes)
    bottom = max(b[1] + b[3] for b in boxes)
    assert top <= neubauten.box[1] + 0.001
    assert bottom < kontakt.box[1]  # the next paragraph is not marked


def _w(text: str, y: float) -> citations.Word:
    return citations.Word(text, (0.1, y, 0.1, 0.01))


def test_a_form_value_far_from_its_label_is_still_found():
    # A filled-in form: the value sits at the end of the text stream.
    words = [_w(t, 0.1 + i * 0.01) for i, t in enumerate(
        "Datum: ____ Unterschrift Eigentümer: ____ Bitte immer angeben Datum".split()
        + ["66,6", "22.03.2026", "Valentin"]
    )]
    boxes = align_words(words, "Datum: 22.03.2026")
    assert boxes == [pytest.approx(words[-2].box)]  # the date — "Datum" occurs twice, so not an anchor


def test_anchors_need_a_number_or_two_words():
    words = [_w(t, 0.1 + i * 0.01) for i, t in enumerate(
        "Biomethan steht hier und viel später irgendwo Datum Unterschrift".split()
    )]
    assert align_words(words, "Biomethan ist kein Treffer allein") == []


def test_a_passage_not_on_the_page_gets_no_boxes(pdf_bytes):
    words = pdf_words(pdf_bytes, 1)
    assert align_words(words, "Die Kündigungsfrist beträgt drei Monate zum Quartalsende.") == []
    assert align_words([], "anything at all") == []


# ── Rendering in the chat ────────────────────────────────────────────────────


@pytest.fixture
def chat(monkeypatch):
    import app_ui.pages.chat as chat

    monkeypatch.setattr(chat, "get_translator", lambda: (lambda s: s))
    return chat


def test_markers_become_numbered_pills_and_a_source_line(chat):
    html = chat._inject_doc_links("Haus muss 65 % nutzen.[^241-p3] Siehe #241.[^255]")
    assert '<sup class="pb-cite" data-cite="241-3" data-n="1">1</sup>' in html
    assert '<sup class="pb-cite" data-cite="255-0" data-n="2">2</sup>' in html
    assert 'data-doc-id="241"' in html  # the plain #241 is still a document link
    assert '<div class="pb-cite-list">' in html
    assert '<b>1</b>p. 3</span>' in html
    assert 'class="pb-cite-item is-bare" data-cite="255-0" data-n="2"><b>2</b></span>' in html


def test_the_source_box_groups_pages_under_the_documents_title(chat):
    html = chat._inject_doc_links(
        "A.[^577-p2] B.[^577-p1] C.[^577-p2] D.[^9]", titles={577: "Nebenkosten <2025>"}
    )
    box = html.split('<div class="pb-cite-list">')[1]
    assert box.count('class="pb-cite-doc"') == 2
    assert "Nebenkosten &lt;2025&gt;" in box  # escaped
    assert box.index('data-cite="577-2"') < box.index('data-cite="577-1"')
    assert '<span class="pb-cite-title" data-doc="9"></span>' in box  # filled in later


def test_a_hash_inside_a_marker_is_not_turned_into_a_link(chat):
    html = chat._inject_doc_links("Fakt.[^#241-p3]")
    assert "data-doc-id" not in html.split('<div class="pb-cite-list">')[0]
    assert 'data-cite="241-3"' in html


def test_half_streamed_marker_is_hidden(chat):
    assert "[^24" not in chat._inject_doc_links("Fakt.[^24")


def test_answer_without_citations_is_unchanged(chat):
    assert "pb-cite" not in chat._inject_doc_links("Nur Text mit #12.")


# ── Tool results carry the references ────────────────────────────────────────


def test_search_hit_snippet_carries_its_page_reference():
    from datetime import datetime, timezone

    from models.paperless_document import PaperlessDocument
    from models.result_document import DocumentResult
    from services.chat_service import _hit_snippet

    now = datetime.now(timezone.utc)
    doc = PaperlessDocument(
        id=241, original_file_name="f.pdf", notes=[], mime_type="application/pdf",
        title="GEG", tags=[], added=now, created=now, modified=now,
        pdf_url="http://example.invalid/241",
    )
    hit = DocumentResult(document=doc, matched_chunks=["Mindestens 65 %"], matched_pages=[3])
    assert _hit_snippet(hit, 150) == "…Mindestens 65 %… [^241-p3]"
    assert _hit_snippet(DocumentResult(document=doc), 150) == "cite as [^241]"


def test_prompt_asks_for_references_per_sentence():
    from config.chat_prompts import _BLOCKS

    block = _BLOCKS["documents"]
    assert "[^241-p3]" in block and "After EVERY sentence" in block



# ── Weak evidence, tables, wrong pages (examples of 2026-11-01) ──────────────


def test_a_lone_digit_in_a_barcode_is_no_support():
    page = ("02 2FF7 4F60 35 700C 2C91\nDV 03.23 1,00 Deutsche Post\n"
            "Sie haben mit uns vereinbart, dass wir Ihren Eigenbeitrag für jedes Kalenderjahr "
            "um vier Prozent des Vorjahresbeitrags erhöhen.")
    passage, _ = best_passage(page, "Im Vertrag steht, dass der Eigenbeitrag jährlich um 4 % "
                                    "des Vorjahresbeitrags steigt")
    assert passage.startswith("Sie haben mit uns vereinbart")
    assert "4f60" in citations.tokens("4F60") and "4" not in citations.tokens("2FF7 4F60")
    assert citations.tokens("700C") == ["700", "c"]   # a number with its unit still splits


def test_a_year_alone_is_no_support():
    page = "Einkommensteuererklärung\nWISO steuer:Web 2019, Buhl, Hannover gedruckt mit ERiC-Print 35.4.2/ESt_2018"
    assert best_passage(page, "Your tax id is 12 345 678 901, as in your 2018 tax return") == ("", 0.0)


def test_an_id_matches_grouped_or_not():
    page = "Pichlmaier, Valentin\nIdNr.: 12 345 678 901\nEinkommensteuererklärung 2018"
    for claim in ("Your tax id is 12345678901", "Your tax id is 12 345 678 901"):
        assert "IdNr" in best_passage(page, claim)[0]


def test_an_amount_matches_a_form_with_separate_cent_field():
    page = ("Summe der insgesamt gutgeschriebenen Zulagen Euro: 1683 Cent: 26\n"
            "Summe der insgesamt geleisteten Altersvorsorgebeiträge Euro: 10847 Cent: 52")
    passage, _ = best_passage(page, "Bisher eingezahlt: 10.847,52 € eigene Beiträge")
    assert "10847" in passage


def test_a_table_amount_marks_its_row_not_a_sentence_with_the_label():
    # The text layer of a table: labels first, values after — then a paragraph
    # repeating the label.
    words = [citations.Word(t, (0.1 + 0.05 * i, 0.30, 0.04, 0.01)) for i, t in enumerate(
        "Schlussüberschuss Sockelbeteili un an den Bewertun sreserven".split())]
    words += [citations.Word(t, (0.1, 0.27, 0.04, 0.01)) for t in ("0,00", "Euro")]
    words += [citations.Word("37,24", (0.8, 0.30, 0.04, 0.01)), citations.Word("Euro", (0.85, 0.30, 0.04, 0.01))]
    words += [citations.Word(t, (0.1 + 0.05 * i, 0.50, 0.04, 0.01)) for i, t in enumerate(
        "erhalten Sie eine Sockelbeteiligung an den Bewertungsreserven. Sie kann".split())]
    boxes = align_words(words, "Sockelbeteiligung an den Bewertungsreserven: + 37,24 Euro")
    assert boxes and all(abs(b[1] - 0.30) < 0.005 for b in boxes)


PAGES_32 = [
    (1, "Ihr Eigenbeitrag steigt jährlich."),
    (3, "Gebildetes Kapital zum 31.12.2022: = 12.997,23 Euro"),
    (4, "Garantierte Rente: 290,58 Euro"),
    (6, "Garantierte Rente 290,58 Euro, mögliche Rente 298,61 Euro"),
]


def test_a_marker_on_the_wrong_page_is_pointed_at_the_right_one():
    text = ("- **Vermögen:** 12.997,23 € [^32-p3]\n"
            "- **Erwartete Rente 2055:** 290,58 € garantiert, maximal 298,61 € [^32-p3]")
    out = citations.repair_pages(text, lambda _d: PAGES_32)
    assert "12.997,23 € [^32-p3]" in out               # supported where cited: untouched
    assert "298,61 € [^32-p6]" in out                  # the page with both amounts


def test_repair_leaves_unclear_and_unsupported_markers_alone():
    text = "Das Wetter war schön [^32-p3]. Ganz allgemein [^32]."
    assert citations.repair_pages(text, lambda _d: PAGES_32) == text


def test_claim_before_matches_the_browsers_rule():
    text = "Erste Aussage hier. Kurz [^1-p1] und weiter"
    assert citations.claim_before(text, text.index("[^1")) == "Erste Aussage hier. Kurz"
