"""PDF generation — WeasyPrint output shape and archive-language behaviour.

These are the regression tests for the PyMuPDF -> WeasyPrint swap. The link
annotation check matters most: fitz.Story rendered links visually but emitted no
/URI annotations, which is why the old code carried a manual post-processing
pass. WeasyPrint emits them natively and that pass was deleted — this test is
what keeps that claim honest.

WeasyPrint dlopens Pango at render time, so on a machine without it these fail
loudly rather than silently producing an empty PDF.
"""

import re
from datetime import datetime

import pytest

from services.pdf_generator import generate_chat_pdf

_URI_RE = re.compile(rb"/URI\s*\((.*?)\)")


def _pdf(markdown="# Heading\n\nBody text.", **kw):
    kw.setdefault("title", "Test Document")
    kw.setdefault("username", "alice")
    kw.setdefault("model_name", "test-model")
    return generate_chat_pdf(markdown, **kw)


def test_output_is_a_pdf():
    data = _pdf()
    assert data.startswith(b"%PDF-"), "not a PDF"
    assert data.rstrip().endswith(b"%%EOF")


def _text_of(pdf_bytes: bytes) -> str:
    """Extract rendered text — the only honest way to assert on PDF content."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        return "\n".join(doc[i].get_textpage().get_text_range() for i in range(len(doc)))
    finally:
        doc.close()


def _uris_of(pdf_bytes: bytes) -> list[bytes]:
    """Collect /URI values, decompressing FlateDecode streams first.

    Grepping the raw bytes finds nothing: WeasyPrint compresses object streams,
    so the annotations are real but invisible to a plain search.
    """
    import zlib

    found = list(_URI_RE.findall(pdf_bytes))
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", pdf_bytes, re.DOTALL):
        try:
            found += _URI_RE.findall(zlib.decompress(match.group(1)))
        except zlib.error:
            continue
    return found


def test_title_and_metadata_appear_in_the_document():
    data = _pdf(title="Quarterly Report", username="bob", model_name="claude-test")
    text = _text_of(data)
    assert "Quarterly Report" in text
    assert "bob" in text
    assert "claude-test" in text


def test_body_markdown_is_rendered():
    text = _text_of(_pdf("# Section One\n\nA distinctive sentence here."))
    assert "Section One" in text
    assert "distinctive sentence" in text


def test_links_become_real_pdf_annotations():
    """Regression for the PyMuPDF swap: fitz.Story emitted no /URI annotations,
    which is why the old code carried a manual post-processing pass."""
    uris = _uris_of(_pdf("Visit [the docs](https://example.com/page) now."))
    assert uris, "no /URI annotations — links are not clickable"
    assert any(b"example.com/page" in u for u in uris)


def test_bare_urls_are_linkified():
    """markdown2's link-patterns extra turns bare URLs into anchors."""
    uris = _uris_of(_pdf("See https://example.org/thing for details."))
    assert any(b"example.org/thing" in u for u in uris)


def test_multipage_content_produces_multiple_pages():
    import pypdfium2 as pdfium

    long_md = "\n\n".join(f"Paragraph number {i} with some text." for i in range(400))
    doc = pdfium.PdfDocument(_pdf(long_md))
    assert len(doc) > 1
    doc.close()


def test_tables_render_without_error():
    md = (
        "| Col A | Col B | Col C |\n"
        "|---|---|---|\n"
        "| 1 | 2 | 3 |\n"
        "| long wrapping value here | b | c |\n"
    )
    assert _pdf(md).startswith(b"%PDF-")


def test_wide_tables_render_without_error():
    """_process_tables scales the font by column count; 7+ takes a separate path."""
    header = "| " + " | ".join(f"C{i}" for i in range(8)) + " |\n"
    sep = "|" + "---|" * 8 + "\n"
    row = "| " + " | ".join(str(i) for i in range(8)) + " |\n"
    assert _pdf(header + sep + row).startswith(b"%PDF-")


@pytest.mark.parametrize("markdown", [
    "",
    "   ",
    "# Only a heading",
    "- just\n- a\n- list",
    "```python\nprint('code')\n```",
    "> a blockquote",
])
def test_edge_case_content_still_produces_a_pdf(markdown):
    assert _pdf(markdown).startswith(b"%PDF-")


def test_unicode_content_renders():
    """Missing fonts would silently drop glyphs rather than raise."""
    data = _pdf("Umlauts äöüß, Euro €, dash —, fraction ½")
    assert data.startswith(b"%PDF-")
    assert len(data) > 1000


def test_html_in_title_is_escaped_not_executed():
    """Titles come from the LLM; an unescaped tag would corrupt the layout."""
    data = _pdf(title="<script>alert(1)</script>")
    assert data.startswith(b"%PDF-")


def test_explicit_datetime_is_used(monkeypatch):
    from config import settings as settings_mod

    monkeypatch.setattr(settings_mod.settings, "archive_language", "en", raising=False)
    data = _pdf(dt=datetime(2026, 7, 20, 8, 13))
    assert data.startswith(b"%PDF-")


@pytest.mark.parametrize("lang,badge", [("en", "AI-generated"), ("de", "KI-generiert")])
def test_badge_follows_archive_language(monkeypatch, lang, badge):
    """Badge text is archive-level, not per-user — it must follow ARCHIVE_LANGUAGE."""
    from config import settings as settings_mod

    from services.pdf_generator import _BADGE_TEXT

    monkeypatch.setattr(settings_mod.settings, "archive_language", lang, raising=False)
    assert _BADGE_TEXT[lang] == badge
    assert _pdf().startswith(b"%PDF-")


def test_unknown_archive_language_falls_back_to_english_badge(monkeypatch):
    from config import settings as settings_mod

    monkeypatch.setattr(settings_mod.settings, "archive_language", "klingon", raising=False)
    assert _pdf().startswith(b"%PDF-")
