"""analyze_document's guard-and-retry behaviour, with the vision call stubbed.

No Ollama involved: _extract_once is replaced so the tests exercise only the
retry decision. What matters is that a transient loop costs one extra call and
recovers, a persistent loop is capped rather than stored whole, and healthy
pages never trigger a second call (each one is a full vision inference).
"""

import asyncio

import pytest

from models.extraction import ExtractedContent, PageImage
from services.extraction_guard import MAX_PLAUSIBLE_PAGE_WORDS
from services.vision import OllamaVisionClient


def _content(text: str) -> ExtractedContent:
    return ExtractedContent(
        page=1,
        page_text=text,
        tables=[],
        actions=[],
        page_summary="summary",
        cross_references=[],
        document_type="Invoice",
    )


def _page() -> PageImage:
    return PageImage(page_number=1, total_pages=1, image_bytes=b"fake-jpeg")


@pytest.fixture
def client(monkeypatch):
    c = OllamaVisionClient(base_url="http://vision.invalid", model="test-model")
    # analyze_document builds its prompt from the active profile; that path is
    # covered elsewhere and needs no network.
    return c


def _run(client, page=None):
    return asyncio.run(
        client.analyze_document(page or _page(), "Invoice", "Invoice")
    )


def test_healthy_page_is_returned_after_one_call(client, monkeypatch):
    calls = []

    async def fake(page, prompt, ai_doc_type):
        calls.append(1)
        return _content("A perfectly normal page of extracted text.")

    monkeypatch.setattr(client, "_extract_once", fake)
    result = _run(client)
    assert len(calls) == 1, "healthy pages must not pay for a retry"
    assert result.page_text.startswith("A perfectly normal")


def test_transient_loop_recovers_on_retry(client, monkeypatch):
    """Loops are sampling-dependent — the real doc 124 came back clean."""
    calls = []

    async def fake(page, prompt, ai_doc_type):
        calls.append(1)
        if len(calls) == 1:
            return _content(" ".join(["loop"] * 5000))
        return _content("Clean text on the second attempt.")

    monkeypatch.setattr(client, "_extract_once", fake)
    result = _run(client)
    assert len(calls) == 2
    assert result.page_text == "Clean text on the second attempt."


def test_persistent_loop_is_truncated_not_stored_whole(client, monkeypatch):
    calls = []

    async def fake(page, prompt, ai_doc_type):
        calls.append(1)
        return _content(" ".join(["loop"] * 50000))

    monkeypatch.setattr(client, "_extract_once", fake)
    result = _run(client)
    assert len(calls) == 2, "exactly one retry, then give up"
    assert len(result.page_text.split()) == MAX_PLAUSIBLE_PAGE_WORDS


def test_other_fields_survive_the_guard(client, monkeypatch):
    """Truncation must touch page_text only — tables and actions stay intact."""

    async def fake(page, prompt, ai_doc_type):
        c = _content(" ".join(["loop"] * 50000))
        c.tables = [{"caption": "T", "rows": [{"a": "1"}]}]
        c.actions = [{"description": "pay", "deadline": "2026-01-01"}]
        c.cross_references = [{"type": "Rechnungsnummer", "value": "RE-1"}]
        return c

    monkeypatch.setattr(client, "_extract_once", fake)
    result = _run(client)
    assert result.tables == [{"caption": "T", "rows": [{"a": "1"}]}]
    assert result.actions[0]["description"] == "pay"
    assert result.cross_references[0]["value"] == "RE-1"
    assert result.page_summary == "summary"


def test_empty_extraction_is_not_retried(client, monkeypatch):
    """An empty page is a legitimate outcome (blank scan), not a loop."""
    calls = []

    async def fake(page, prompt, ai_doc_type):
        calls.append(1)
        return _content("")

    monkeypatch.setattr(client, "_extract_once", fake)
    result = _run(client)
    assert len(calls) == 1
    assert result.page_text == ""
