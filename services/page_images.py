# services/page_images.py

"""Where the pictures on a page are — boxes from the PDF and from the vision model.

A page's ``images`` list (sidecar ``pages[i]["images"]``) holds one entry per
photo, illustration, diagram or chart:

    {"bbox": [x, y, w, h], "description": "...", "belongs_to": "...", "source": "pdf"|"model"}

``bbox`` is in fractions of the page (0–1, top-left origin), so a crop can be
rendered at any resolution later and a re-render never invalidates it.

Two sources, one rule: **a box is a claim until code has checked it.**

- ``"pdf"`` — the exact rectangle of an image object embedded in a born-digital
  PDF. It only ever *refines* a picture the model reported; the PDF cannot tell a
  photo from a letterhead logo (both are image objects), the model can.
- ``"model"`` — the box the vision model returned, normalized 0–1000. Every one
  goes through ``validate_box`` before it is stored.

A scanned page is one image object covering the whole page. That is the signal
that the PDF knows nothing about the pictures on it and the model's boxes are
all there is.
"""

from __future__ import annotations

import io

import pypdfium2 as pdfium

# Model boxes are normalized to this grid (the Qwen-VL grounding convention).
MODEL_GRID = 1000

# A box smaller than this share of the page is a dot, not a picture; one larger
# than MAX is the page itself (the model "boxed" a full-page scan). MIN is low on
# purpose: an invoice's product thumbnail is ~0.08 % of an A4 page, and the
# calibration model boxed it correctly — the prompt keeps icons out, not this.
MIN_AREA = 0.0005
MAX_AREA = 0.98

# One embedded image covering this much of the page: a scan, not a picture.
SCAN_COVERAGE = 0.85

# PDF image objects smaller than this are bullets, rules and tracking pixels.
PDF_MIN_AREA = 0.0005

# Producers often store one picture as several strips. Image objects whose
# edges are this close (fraction of the page) are merged into one picture.
PDF_MERGE_GAP = 0.01

# A model box and a PDF box are the same picture above this overlap.
MATCH_IOU = 0.3

Box = tuple[float, float, float, float]  # x1, y1, x2, y2 as page fractions


# ── Model boxes ───────────────────────────────────────────────────────────────


def validate_box(raw, img_w: int = 0, img_h: int = 0) -> Box | None:
    """A model box → page fractions, or None when it cannot be a picture.

    ``raw`` is ``[x1, y1, x2, y2]`` on the 0–1000 grid. Some models answer in
    pixels of the image they were shown instead; a coordinate above the grid
    gives that away, and the render size converts it back. Without a render
    size such a box is dropped rather than guessed.
    """
    try:
        x1, y1, x2, y2 = (float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    if max(x1, y1, x2, y2) > MODEL_GRID:
        if not (img_w and img_h):
            return None
        x1, x2 = x1 / img_w, x2 / img_w
        y1, y2 = y1 / img_h, y2 / img_h
    else:
        x1, y1, x2, y2 = (v / MODEL_GRID for v in (x1, y1, x2, y2))
    x1, y1, x2, y2 = (min(1.0, max(0.0, v)) for v in (x1, y1, x2, y2))
    if x2 <= x1 or y2 <= y1:
        return None
    area = (x2 - x1) * (y2 - y1)
    if area < MIN_AREA or area > MAX_AREA:
        return None
    return (x1, y1, x2, y2)


def iou(a: Box, b: Box) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


# ── PDF boxes ─────────────────────────────────────────────────────────────────


def _touching(a: Box, b: Box, gap: float) -> bool:
    return not (
        a[2] + gap < b[0] or b[2] + gap < a[0] or a[3] + gap < b[1] or b[3] + gap < a[1]
    )


def _merge_touching(boxes: list[Box], gap: float = PDF_MERGE_GAP) -> list[Box]:
    merged = list(boxes)
    changed = True
    while changed:
        changed = False
        for i in range(len(merged)):
            for j in range(i + 1, len(merged)):
                if _touching(merged[i], merged[j], gap):
                    a, b = merged[i], merged[j]
                    merged[i] = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
                    del merged[j]
                    changed = True
                    break
            if changed:
                break
    return merged


def pdf_page_boxes(page: "pdfium.PdfPage") -> list[Box]:
    """Embedded pictures of a born-digital page as page fractions.

    Empty for a scan (one image covering the page) — there the PDF knows no
    more than the pixels do.
    """
    width, height = page.get_size()
    if not width or not height:
        return []
    boxes: list[Box] = []
    for obj in page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE], max_depth=3):
        try:
            left, bottom, right, top = obj.get_bounds()
        except Exception:
            continue
        # PDF space has its origin bottom-left; stored boxes are top-left.
        box = (
            min(1.0, max(0.0, left / width)),
            min(1.0, max(0.0, 1 - top / height)),
            min(1.0, max(0.0, right / width)),
            min(1.0, max(0.0, 1 - bottom / height)),
        )
        area = (box[2] - box[0]) * (box[3] - box[1])
        if area >= SCAN_COVERAGE:
            return []
        if area >= PDF_MIN_AREA:
            boxes.append(box)
    return _merge_touching(boxes)


def pdf_boxes_per_page(pdf_bytes: bytes) -> list[list[Box]]:
    """``pdf_page_boxes`` for every page; an unreadable PDF yields no boxes."""
    try:
        doc = pdfium.PdfDocument(pdf_bytes)
    except Exception:
        return []
    try:
        return [pdf_page_boxes(doc[i]) for i in range(len(doc))]
    finally:
        doc.close()


# ── Combining ─────────────────────────────────────────────────────────────────


def build_page_images(
    model_images: list, pdf_boxes: list[Box], img_w: int = 0, img_h: int = 0
) -> list[dict]:
    """The sidecar ``images`` list for one page.

    Every picture the model reported is kept — its description is what makes it
    findable, box or not. A model box that overlaps a PDF image object is
    replaced by that object's exact box; an invalid model box leaves the
    picture without one (the chat then shows the whole page).
    """
    out: list[dict] = []
    used_pdf: set[int] = set()
    seen: set[tuple] = set()
    for item in model_images or []:
        if not isinstance(item, dict):
            continue
        description = str(item.get("description") or "").strip()
        if not description:
            continue
        box = validate_box(item.get("box"), img_w, img_h)
        source = "model"
        if box is not None and pdf_boxes:
            best, best_iou = None, MATCH_IOU
            for idx, pbox in enumerate(pdf_boxes):
                if idx in used_pdf:
                    continue
                score = iou(box, pbox)
                if score > best_iou:
                    best, best_iou = idx, score
            if best is not None:
                used_pdf.add(best)
                box, source = pdf_boxes[best], "pdf"
        key = (description, box)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "bbox": (
                [round(box[0], 4), round(box[1], 4), round(box[2] - box[0], 4), round(box[3] - box[1], 4)]
                if box is not None else None
            ),
            "description": description,
            "belongs_to": str(item.get("belongs_to") or "").strip(),
            "source": source if box is not None else "none",
        })
    return out


def page_images(page: dict) -> list[dict]:
    """The pictures of a sidecar page. Old sidecars have no key."""
    images = page.get("images") if isinstance(page, dict) else None
    return [i for i in (images or []) if isinstance(i, dict) and i.get("description")]


def embedding_lines(images: list[dict]) -> str:
    """One line per picture, appended to a page's text for embedding only.

    The marker is fixed English on purpose: the model translated the old inline
    marker, which is what made it unparseable. These lines never reach
    ``page_text`` / ``full_text`` — that text is written back into Paperless.
    """
    lines = []
    for n, img in enumerate(images):
        line = f"[Image {n}: {img['description']}"
        if img.get("belongs_to"):
            line += f" — {img['belongs_to']}"
        lines.append(line + "]")
    return "\n".join(lines)


def describe_for_model(images: list[dict], page_number: int) -> str:
    """The pictures of a page as tool output, with the index the chat tool takes."""
    if not images:
        return ""
    parts = []
    for n, img in enumerate(images):
        text = f"[{n}] {img['description']}"
        if img.get("belongs_to"):
            text += f" ({img['belongs_to']})"
        parts.append(text)
    return f"Images on page {page_number}: " + "; ".join(parts)


# ── Rendering ────────────────────────────────────────────────────────────────


def render_page_jpeg(
    pdf_bytes: bytes,
    page_number: int,
    bbox: list[float] | None = None,
    long_side: int = 1600,
    pad: float = 0.02,
    quality: int = 85,
) -> bytes | None:
    """A page (or the ``bbox`` part of it) as JPEG, long side ≈ ``long_side`` px.

    A crop is rendered at the scale that makes *the crop* that size, not the
    page — a small product photo would otherwise come out as a thumbnail.
    Returns None for a page that does not exist.
    """
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        if not 1 <= page_number <= len(doc):
            return None
        page = doc[page_number - 1]
        width, height = page.get_size()
        if bbox:
            x, y, w, h = bbox
            x1, y1 = max(0.0, x - pad), max(0.0, y - pad)
            x2, y2 = min(1.0, x + w + pad), min(1.0, y + h + pad)
            crop_w, crop_h = (x2 - x1) * width, (y2 - y1) * height
            # Never more than 4× the page's 72-dpi size: past that a scan only
            # gets blurrier and the file larger.
            scale = min(4.0, long_side / max(crop_w, crop_h, 1))
            image = page.render(
                scale=scale,
                crop=(x1 * width, (1 - y2) * height, (1 - x2) * width, y1 * height),
            ).to_pil()
        else:
            scale = long_side / max(width, height, 1)
            image = page.render(scale=scale).to_pil()
        buf = io.BytesIO()
        image.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
        return buf.getvalue()
    finally:
        doc.close()


def page_count(pdf_bytes: bytes) -> int:
    doc = pdfium.PdfDocument(pdf_bytes)
    try:
        return len(doc)
    finally:
        doc.close()
