"""Picture boxes: what the PDF and the vision model claim, and what code keeps.

The invariant behind every test here: a box is a claim until code has checked
it. The model's 0–1000 grid boxes are clamped and filtered, a box in pixels is
converted only when the render size is known, and an embedded PDF image only
ever refines a picture the model reported — it never adds one, because the PDF
cannot tell a photo from a letterhead logo.

The PDFs are built with WeasyPrint from generated PNGs, so the expected boxes
are known exactly.
"""

import base64
import io
import json

import pypdfium2 as pdfium
import pytest
from PIL import Image
from weasyprint import HTML

from services import page_images as P
from services.sidecar_service import SidecarService


def _png(w: int = 60, h: int = 40, color=(200, 30, 30)) -> str:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _pdf(body: str) -> bytes:
    """A4 PDF without page margins, so CSS millimetres map to page fractions."""
    html = f"""<html><head><style>
        @page {{ size: 210mm 297mm; margin: 0 }}
        body {{ margin: 0 }}
        img {{ position: absolute; display: block }}
    </style></head><body>{body}</body></html>"""
    return HTML(string=html).write_pdf()


def _boxes(pdf: bytes, page: int = 0):
    doc = pdfium.PdfDocument(pdf)
    try:
        return P.pdf_page_boxes(doc[page])
    finally:
        doc.close()


# ── validate_box ─────────────────────────────────────────────────────────────


def test_grid_box_becomes_page_fractions():
    assert P.validate_box([100, 200, 600, 700]) == pytest.approx((0.1, 0.2, 0.6, 0.7))


def test_box_is_clamped_to_the_page():
    assert P.validate_box([-50, 100, 1000, 1000]) == pytest.approx((0.0, 0.1, 1.0, 1.0))


@pytest.mark.parametrize(
    "raw",
    [
        [600, 200, 100, 700],  # inverted x
        [100, 700, 600, 200],  # inverted y
        [100, 100, 100, 500],  # zero width
        [500, 500, 510, 505],  # a dot
        [0, 0, 1000, 1000],  # the whole page — a scan, not a picture
        [1, 2, 3],  # not a box
        "box",
        None,
        ["a", "b", "c", "d"],
    ],
)
def test_impossible_boxes_are_dropped(raw):
    assert P.validate_box(raw) is None


def test_pixel_box_is_converted_with_the_render_size():
    """Some models answer in pixels of the image they saw. A coordinate past the
    grid gives that away; the render size converts it back."""
    box = P.validate_box([184, 260, 1103, 1560], img_w=1838, img_h=2600)
    assert box == pytest.approx((0.1, 0.1, 0.6, 0.6), abs=1e-3)


def test_pixel_box_without_render_size_is_dropped_not_guessed():
    assert P.validate_box([184, 260, 1103, 1560]) is None


def test_small_product_thumbnail_survives():
    """Calibration on the live archive: an invoice's product picture is ~0.08 %
    of the page and the model boxed it correctly. It must not be filtered out."""
    assert P.validate_box([118, 552, 148, 578]) is not None


# ── PDF boxes ────────────────────────────────────────────────────────────────


def test_born_digital_pdf_yields_one_box_per_picture():
    pdf = _pdf(
        f'<img src="{_png()}" style="left:21mm; top:29.7mm; width:63mm; height:59.4mm">'
        f'<img src="{_png()}" style="left:126mm; top:148.5mm; width:42mm; height:89.1mm">'
    )
    boxes = sorted(_boxes(pdf))
    assert len(boxes) == 2
    assert boxes[0] == pytest.approx((0.1, 0.1, 0.4, 0.3), abs=0.01)
    assert boxes[1] == pytest.approx((0.6, 0.5, 0.8, 0.8), abs=0.01)


def test_full_page_image_is_a_scan():
    pdf = _pdf(f'<img src="{_png(210, 297)}" style="left:0; top:0; width:210mm; height:297mm">')
    assert _boxes(pdf) == []


def test_picture_stored_as_strips_is_one_box():
    """Seen on the live archive: a map stored as two stacked image objects."""
    pdf = _pdf(
        f'<img src="{_png()}" style="left:21mm; top:100mm; width:150mm; height:40mm">'
        f'<img src="{_png()}" style="left:21mm; top:140mm; width:150mm; height:40mm">'
    )
    boxes = _boxes(pdf)
    assert len(boxes) == 1
    assert boxes[0][1] == pytest.approx(100 / 297, abs=0.01)
    assert boxes[0][3] == pytest.approx(180 / 297, abs=0.01)


def test_unreadable_pdf_yields_no_boxes():
    assert P.pdf_boxes_per_page(b"not a pdf") == []


# ── build_page_images ────────────────────────────────────────────────────────


def test_model_box_is_replaced_by_the_overlapping_pdf_box():
    pdf_box = (0.2, 0.15, 0.77, 0.9)
    out = P.build_page_images(
        [{"description": "X-ray", "belongs_to": "Spine", "box": [198, 152, 772, 905]}],
        [pdf_box],
    )
    assert out == [{
        "bbox": [0.2, 0.15, 0.57, 0.75],
        "description": "X-ray",
        "belongs_to": "Spine",
        "source": "pdf",
    }]


def test_pdf_image_without_a_model_picture_is_not_added():
    """A letterhead logo is an image object too — only the model knows it is
    not a picture."""
    assert P.build_page_images([], [(0.06, 0.05, 0.4, 0.15)]) == []


def test_model_box_far_from_any_pdf_box_stays_a_model_box():
    out = P.build_page_images(
        [{"description": "Chart", "box": [100, 100, 300, 300]}], [(0.6, 0.6, 0.9, 0.9)]
    )
    assert out[0]["source"] == "model"
    assert out[0]["bbox"] == pytest.approx([0.1, 0.1, 0.2, 0.2])


def test_picture_with_an_invalid_box_keeps_its_description():
    """The description is what makes it findable; the chat shows the page."""
    out = P.build_page_images([{"description": "Lasagne", "box": [5, 5, 1, 1]}], [])
    assert out == [{"bbox": None, "description": "Lasagne", "belongs_to": "", "source": "none"}]


def test_entries_without_description_and_duplicates_are_dropped():
    item = {"description": "Photo", "box": [100, 100, 500, 500]}
    out = P.build_page_images([item, dict(item), {"description": " ", "box": [1, 1, 9, 9]}, "x"], [])
    assert len(out) == 1


def test_one_pdf_box_is_not_given_to_two_pictures():
    pdf_box = (0.1, 0.1, 0.5, 0.5)
    out = P.build_page_images(
        [
            {"description": "A", "box": [100, 100, 500, 500]},
            {"description": "B", "box": [110, 110, 490, 490]},
        ],
        [pdf_box],
    )
    assert [o["source"] for o in out] == ["pdf", "model"]


# ── text helpers ─────────────────────────────────────────────────────────────


def test_embedding_lines_use_a_fixed_english_marker():
    lines = P.embedding_lines([
        {"description": "Lasagne im Ofen", "belongs_to": "Rezept: Lasagne"},
        {"description": "Tomaten", "belongs_to": ""},
    ])
    assert lines == "[Image 0: Lasagne im Ofen — Rezept: Lasagne]\n[Image 1: Tomaten]"


def test_tool_listing_carries_the_index():
    text = P.describe_for_model([{"description": "Lasagne", "belongs_to": "Recipe"}], 12)
    assert text == "Images on page 12: [0] Lasagne (Recipe)"


def test_old_sidecar_page_has_no_pictures():
    assert P.page_images({"page_number": 1, "page_text": "x"}) == []
    assert P.page_images({"images": None}) == []
    assert P.page_images({"images": [{"bbox": None}]}) == []


# ── rendering ────────────────────────────────────────────────────────────────


def _jpeg_size(data: bytes) -> tuple[int, int]:
    img = Image.open(io.BytesIO(data))
    assert img.format == "JPEG"
    return img.size


def test_page_render_has_the_requested_long_side():
    pdf = _pdf("<p>x</p>")
    w, h = _jpeg_size(P.render_page_jpeg(pdf, 1, long_side=800))
    assert max(w, h) == pytest.approx(800, abs=2)


def test_crop_has_the_shape_of_the_box():
    pdf = _pdf("<p>x</p>")
    w, h = _jpeg_size(P.render_page_jpeg(pdf, 1, [0.1, 0.1, 0.6, 0.2], long_side=600, pad=0))
    # 0.6 × 210 mm wide, 0.2 × 297 mm high
    assert w / h == pytest.approx((0.6 * 210) / (0.2 * 297), rel=0.05)


def test_missing_page_renders_nothing():
    pdf = _pdf("<p>x</p>")
    assert P.render_page_jpeg(pdf, 2) is None
    assert P.render_page_jpeg(pdf, 0) is None


# ── re-ingest order ──────────────────────────────────────────────────────────


def _write_sidecar(d, pid: int, version: str, page_text: str = "", images=None):
    page = {"page_number": 1, "page_text": page_text, "page_summary": ""}
    if images is not None:
        page["images"] = images
    (d / f"{pid}.json").write_text(json.dumps({
        "paperless_id": pid, "prompt_version": version, "pages": [page],
    }))


def test_outdated_documents_with_pictures_come_first_then_newest(tmp_sidecar_dir):
    _write_sidecar(tmp_sidecar_dir, 10, "0.2")
    _write_sidecar(tmp_sidecar_dir, 30, "0.2")
    _write_sidecar(tmp_sidecar_dir, 5, "0.2", "Text\n\n[Bildbeschreibung: Röntgenbild]")
    _write_sidecar(tmp_sidecar_dir, 7, "0.2", "[Image description: chart]")
    _write_sidecar(tmp_sidecar_dir, 99, "0.3")
    (tmp_sidecar_dir / "index.json").write_text("{}")
    svc = SidecarService(extraction_sidecar_path=str(tmp_sidecar_dir))
    assert svc.outdated_ids("0.3") == [7, 5, 30, 10]
