"""Pictures in chat: the tool, the access gate, the cache, the route and ingest.

The rule these pin: sidecars and the render cache have no access control, so
every way to a picture asks Paperless with the *user's* token first — and a
document the user cannot see looks exactly like one that does not exist.
"""

import asyncio
import base64
import io
from dataclasses import dataclass
from datetime import datetime

import pytest

from models.paperless_document import PaperlessDocument
from PIL import Image
from weasyprint import HTML

from app_ui import doc_image_routes
from app_ui.chat_images import MAX_IMAGES_PER_ANSWER, with_images
from models.extraction import ExtractedContent
from services import chat_service, doc_image_service
from services.chat_service import ImageDisplayEvent


def _run(coro):
    return asyncio.run(coro)


def _png() -> str:
    buf = io.BytesIO()
    Image.new("RGB", (60, 40), (30, 120, 200)).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _pdf(pages: int = 2) -> bytes:
    body = "".join(
        f'<div style="page-break-after:always"><p>Page {n}</p>'
        f'<img src="{_png()}" style="width:80mm;height:50mm"></div>'
        for n in range(1, pages + 1)
    )
    return HTML(string=f"<html><body>{body}</body></html>").write_pdf()


SIDECAR = {
    "paperless_id": 7,
    "pages": [
        {"page_number": 1, "page_text": "Intro", "page_summary": ""},
        {
            "page_number": 2,
            "page_text": "Lasagne al forno",
            "page_summary": "",
            "images": [
                {"bbox": [0.1, 0.1, 0.4, 0.3], "description": "Lasagne in a baking dish",
                 "belongs_to": "Recipe: Lasagne al forno", "source": "model"},
                {"bbox": None, "description": "Tomatoes", "belongs_to": "", "source": "none"},
            ],
        },
    ],
}


def _Doc(doc_id: int) -> PaperlessDocument:
    when = datetime(2026, 10, 1, 12, 0)
    return PaperlessDocument(
        id=doc_id, title="Kochmagazin", original_file_name="k.pdf", notes=[],
        mime_type="application/pdf", tags=[], added=when, created=when, modified=when,
        page_count=2, pdf_url=f"http://example.invalid/{doc_id}",
    )


class _FakePaperless:
    """A token-scoped client: sees only `visible`, counts downloads."""

    downloads = 0

    def __init__(self, visible=(7,), pdf: bytes | None = None):
        self.visible = set(visible)
        self.pdf = pdf

    async def list_documents(self, ids=None, **_):
        return [_Doc(i) for i in (ids or []) if i in self.visible]

    async def download_document(self, doc_id):
        type(self).downloads += 1
        return self.pdf


@pytest.fixture
def sidecar(monkeypatch):
    from services import clients

    monkeypatch.setattr(
        clients.sidecar_service, "load_sidecar", lambda i: SIDECAR if i == 7 else None
    )
    monkeypatch.setattr(
        chat_service.sidecar_service, "load_sidecar", lambda i: SIDECAR if i == 7 else None
    )


# ── the chat tool ────────────────────────────────────────────────────────────


@pytest.fixture
def user(monkeypatch, sidecar):
    monkeypatch.setattr(chat_service, "_user_paperless", lambda: _FakePaperless())


def _show(**inputs):
    return _run(chat_service._execute_tool_dispatch("show_document_image", inputs))


def test_whole_page_emits_an_event_and_tells_the_model(user):
    text, docs, events = _show(document_id=7, page_number=2)
    assert events == [ImageDisplayEvent(doc_id=7, page=2, index=None, caption="Kochmagazin – 2")]
    assert "Shown to the user" in text
    # The document joins the results panel, at the page shown.
    assert [(d.document.id, d.matched_pages) for d in docs] == [(7, [2])]


def test_picture_index_uses_what_it_belongs_to_as_caption(user):
    _text, _docs, events = _show(document_id=7, page_number=2, image_index=0)
    assert events[0].index == 0
    assert events[0].caption == "Recipe: Lasagne al forno"


def test_picture_without_box_still_shows_and_says_so(user):
    text, _docs, events = _show(document_id=7, page_number=2, image_index=1)
    assert events and "whole page" in text


def test_foreign_document_shows_nothing(monkeypatch, sidecar):
    monkeypatch.setattr(chat_service, "_user_paperless", lambda: _FakePaperless(visible=()))
    text, _docs, events = _show(document_id=7, page_number=1)
    assert events == [] and "no access" in text


def test_missing_page_names_the_range(user):
    text, _docs, events = _show(document_id=7, page_number=9)
    assert events == [] and "pages 1–2" in text


def test_missing_index_lists_the_available_pictures(user):
    text, _docs, events = _show(document_id=7, page_number=2, image_index=5)
    assert events == []
    assert "[0] Lasagne in a baking dish" in text and "[1] Tomatoes" in text


def test_index_on_a_page_without_pictures_points_to_the_whole_page(user):
    text, _docs, events = _show(document_id=7, page_number=1, image_index=0)
    assert events == [] and "without image_index" in text


def test_tool_is_display_only_and_kept_from_werkbank():
    from werkbank.v2.registry import load_defaults

    tool = next(t for t in chat_service.TOOL_DEFINITIONS if t["name"] == "show_document_image")
    assert "do not get to see the image" in tool["description"]
    assert "show_document_image" in load_defaults().forbidden_tools


def test_page_text_and_details_list_the_pictures_with_index(user):
    text, _ = _run(chat_service._tool_get_document_page_text({"document_id": 7, "page_number": 2}))
    assert "Images on page 2: [0] Lasagne in a baking dish (Recipe: Lasagne al forno)" in text
    details, _ = _run(chat_service._tool_get_document_details({"document_id": 7}))
    assert "Images on page 2: [0]" in details
    assert "page ?" not in details


def test_reading_one_document_puts_it_into_the_results(user):
    _t, hits = _run(chat_service._tool_get_document_page_text({"document_id": 7, "page_number": 2}))
    assert [(h.document.id, h.matched_pages) for h in hits] == [(7, [2])]
    _t, hits = _run(chat_service._tool_get_document_details({"document_id": 7}))
    assert [h.document.id for h in hits] == [7]
    _t, hits = _run(chat_service._tool_get_document_details({"document_id": 8}))
    assert hits == []  # no access, no result
    assert "get_document_table" in chat_service._DOC_READ_TOOLS


def test_search_hit_lists_pictures_with_page_and_index():
    line = chat_service._search_hit_pictures(SIDECAR)
    assert line.startswith("📷 2 picture(s): p.2 [0] Recipe: Lasagne al forno; p.2 [1] Tomatoes")


# ── the image service ────────────────────────────────────────────────────────


@pytest.fixture
def paperless(monkeypatch, sidecar):
    from services import paperless as paperless_mod

    pdf = _pdf()
    _FakePaperless.downloads = 0
    monkeypatch.setattr(
        paperless_mod, "PaperlessClient", lambda url, token: _FakePaperless(pdf=pdf)
    )
    return pdf


def _jpeg(data):
    img = Image.open(io.BytesIO(data))
    assert img.format == "JPEG"
    return img.size


def test_no_token_gets_nothing(paperless, tmp_path):
    assert _run(doc_image_service.get_image(7, 1, None, "", cache_dir=tmp_path)) is None


def test_foreign_document_gets_nothing(paperless, tmp_path):
    assert _run(doc_image_service.get_image(8, 1, None, "tok", cache_dir=tmp_path)) is None


def test_page_is_rendered_once_and_then_served_from_cache(paperless, tmp_path):
    first = _run(doc_image_service.get_image(7, 1, None, "tok", cache_dir=tmp_path))
    second = _run(doc_image_service.get_image(7, 1, None, "tok", cache_dir=tmp_path))
    assert first == second
    assert max(_jpeg(first)) == pytest.approx(doc_image_service.PAGE_LONG_SIDE, abs=2)
    assert _FakePaperless.downloads == 1


def test_crop_differs_from_page_and_bad_index_falls_back_to_page(paperless, tmp_path):
    page = _run(doc_image_service.get_image(7, 2, None, "tok", cache_dir=tmp_path))
    crop = _run(doc_image_service.get_image(7, 2, 0, "tok", cache_dir=tmp_path))
    fallback = _run(doc_image_service.get_image(7, 2, 9, "tok", cache_dir=tmp_path))
    no_box = _run(doc_image_service.get_image(7, 2, 1, "tok", cache_dir=tmp_path))
    assert crop != page
    assert fallback == page == no_box
    assert _FakePaperless.downloads == 1


def test_missing_page_gets_nothing(paperless, tmp_path):
    assert _run(doc_image_service.get_image(7, 9, None, "tok", cache_dir=tmp_path)) is None


def test_cache_is_pruned_to_its_cap(tmp_path):
    for n in range(5):
        (tmp_path / f"{n}.jpg").write_bytes(b"x" * 100)
    doc_image_service._prune(tmp_path, 250)
    assert sum(p.stat().st_size for p in tmp_path.iterdir()) <= 250


# ── the route ────────────────────────────────────────────────────────────────


def test_route_without_session_is_404(monkeypatch):
    monkeypatch.setattr(doc_image_routes, "get_session_token", lambda: "")
    assert _run(doc_image_routes.doc_image(7, 1)).status_code == 404


def test_route_serves_jpeg_privately(monkeypatch):
    monkeypatch.setattr(doc_image_routes, "get_session_token", lambda: "tok")

    async def _fake(doc_id, page, index, token):
        return b"\xff\xd8jpeg" if (doc_id, token) == (7, "tok") else None

    monkeypatch.setattr(doc_image_service, "get_image", _fake)
    ok = _run(doc_image_routes.doc_image(7, 1))
    assert ok.status_code == 200 and ok.media_type == "image/jpeg"
    assert ok.headers["cache-control"].startswith("private")
    assert _run(doc_image_routes.doc_image(8, 1)).status_code == 404


# ── chat answer and PDF export ───────────────────────────────────────────────


def _ev(doc=7, page=2, index=0, caption="Recipe: Lasagne #12 [x]"):
    return ImageDisplayEvent(doc_id=doc, page=page, index=index, caption=caption)


def test_answer_gets_one_markdown_image_per_picture():
    out = with_images("Here you go.", [_ev()], lambda s: s)
    assert out.startswith("Here you go.\n\n![Recipe: Lasagne 12 x](/api/doc-image/7/2?i=0)")
    assert "#7, page 2" in out  # becomes a document link in the chat


def test_answer_does_not_repeat_a_picture_already_in_the_text():
    once = with_images("A", [_ev()], lambda s: s)
    assert with_images(once, [_ev()], lambda s: s) == once


def test_answer_picture_count_is_capped():
    events = [_ev(page=p) for p in range(1, 20)]
    out = with_images("", events, lambda s: s)
    assert out.count("![") == MAX_IMAGES_PER_ANSWER


def test_pdf_export_inlines_visible_pictures_and_drops_foreign_ones(monkeypatch):
    async def _fake(doc_id, page, index, token):
        return b"JPEG" if doc_id == 7 else None

    monkeypatch.setattr(doc_image_service, "get_image", _fake)
    md = "Text\n\n![a](/api/doc-image/7/2?i=0)\n\n![b](/api/doc-image/8/1)"
    out = _run(doc_image_service.embed_images(md, "tok"))
    assert "![a](data:image/jpeg;base64,SlBFRw==)" in out
    assert "/api/doc-image/8" not in out and "![b]" not in out


def test_pdf_generator_survives_an_unresolved_picture_link():
    from services.pdf_generator import generate_chat_pdf

    pdf = generate_chat_pdf("Hello\n\n![x](/api/doc-image/7/2)", "T", "u", "m")
    assert pdf.startswith(b"%PDF")


# ── ingest: pictures are embedded, never written into the page text ──────────


class _Vision:
    async def analyze_document(self, page, document_type, ai_doc_type, prev_table=None, hint=""):
        return ExtractedContent(
            page=page.page_number,
            page_text=f"Rezepttext Seite {page.page_number}",
            tables=[], actions=[], page_summary="s", cross_references=[],
            document_type="Magazin",
            images=[{"description": "Lasagne in der Form", "belongs_to": "Rezept: Lasagne",
                     "box": [100, 100, 500, 400]}],
        )

    async def summarize_document(self, full_summary):
        return "summary"


class _Chroma:
    def __init__(self):
        self.documents = []

    async def upsert(self, ids, documents, metadatas):
        self.documents = documents


class _Thumbs:
    async def get_thumbnail(self, doc_id):
        return b""

    def save_thumbnail(self, data, doc_id):
        pass


class _IngestPaperless:
    def __init__(self, pdf):
        self.pdf = pdf

    async def get_document(self, doc_id):
        @dataclass
        class D:
            document_type: str = "Magazin"
            correspondent: str = ""
            created: datetime = datetime(2026, 1, 1)

        return D()

    async def download_document(self, doc_id):
        return self.pdf


def test_ingest_stores_pictures_and_embeds_their_descriptions_only(tmp_sidecar_dir):
    from pipelines.ingest import ingest_document
    from services.sidecar_service import SidecarService

    chroma = _Chroma()
    sidecars = SidecarService(extraction_sidecar_path=str(tmp_sidecar_dir))
    _run(ingest_document(
        42, _IngestPaperless(_pdf(1)), chroma, _Vision(), sidecars, _Thumbs(),
    ))
    sc = sidecars.load_sidecar(42)
    page = sc["pages"][0]
    assert page["images"][0]["description"] == "Lasagne in der Form"
    assert page["images"][0]["source"] in ("pdf", "model")
    assert "[Image" not in page["page_text"] and "[Image" not in sc["full_text"]
    assert any("[Image 0: Lasagne in der Form — Rezept: Lasagne]" in d for d in chroma.documents)
