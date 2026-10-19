"""Reading hints: a correction left on re-ingest reaches every page prompt and
survives later re-ingests, which delete the sidecar first."""

import asyncio

from models.extraction import ExtractedContent, PageImage
from services import ingest_hints
from services.vision import OllamaVisionClient


def test_hint_round_trip_and_clear(tmp_path):
    path = tmp_path / "hints.json"
    assert ingest_hints.get_hint(362, path) == ""
    ingest_hints.set_hint(362, "  210/80, not 210/180  ", user="vp", path=path)
    assert ingest_hints.get_hint(362, path) == "210/80, not 210/180"
    ingest_hints.set_hint(362, "", path=path)
    assert ingest_hints.get_hint(362, path) == ""


def test_hint_is_capped(tmp_path):
    path = tmp_path / "hints.json"
    stored = ingest_hints.set_hint(1, "x" * 5000, path=path)
    assert len(stored) == ingest_hints.MAX_CHARS


def _prompt_for(hint: str, monkeypatch) -> str:
    client = OllamaVisionClient(base_url="http://vision.invalid", model="m")
    seen = []

    async def fake(page, prompt, ai_doc_type):
        seen.append(prompt)
        return ExtractedContent(page=1, page_text="Normal page text here.", tables=[],
                                actions=[], page_summary="s", cross_references=[],
                                document_type="x")

    monkeypatch.setattr(client, "_extract_once", fake)
    page = PageImage(page_number=1, total_pages=1, image_bytes=b"x")
    asyncio.run(client.analyze_document(page, "x", "x", hint=hint))
    return seen[0]


def test_hint_goes_into_the_page_prompt(monkeypatch):
    assert "210/80, not 210/180" in _prompt_for("210/80, not 210/180", monkeypatch)


def test_no_hint_leaves_the_prompt_unchanged(monkeypatch):
    assert "Reading hint" not in _prompt_for("", monkeypatch)
    assert "Reading hint" not in _prompt_for("   ", monkeypatch)


def test_ingest_reads_the_stored_hint(tmp_path, tmp_sidecar_dir, monkeypatch):
    """A sync re-ingest passes no hint; the stored one must still be used."""
    from pipelines.ingest import ingest_document
    from services.sidecar_service import SidecarService
    from tests.test_doc_images import _Chroma, _IngestPaperless, _pdf, _Thumbs, _Vision

    path = tmp_path / "hints.json"
    ingest_hints.set_hint(42, "Read the date as 2024", path=path)
    monkeypatch.setattr(ingest_hints, "default_path", lambda: path)

    seen = []

    class _HintVision(_Vision):
        async def analyze_document(self, page, document_type, ai_doc_type, prev_table=None, hint=""):
            seen.append(hint)
            return await super().analyze_document(page, document_type, ai_doc_type, prev_table)

    sidecars = SidecarService(extraction_sidecar_path=str(tmp_sidecar_dir))
    asyncio.run(ingest_document(42, _IngestPaperless(_pdf(2)), _Chroma(), _HintVision(),
                                sidecars, _Thumbs()))
    assert seen == ["Read the date as 2024"] * 2
