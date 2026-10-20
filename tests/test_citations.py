"""Citations in chat answers: markers, passage choice, PDF boxes, rendering."""

import pytest

from services import citations
from services.citations import (
    align_words,
    best_page,
    best_passage,
    cite_ref,
    number_citations,
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
    assert "<b>1</b>#241 · p. 3" in html


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

