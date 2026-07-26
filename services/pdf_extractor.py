# services/pdf_extractor.py

import io

import pypdfium2 as pdfium

from models.extraction import PageImage


class PDFExtractor:
    """Renders PDF pages as JPEGs for the vision model.

    Resolution is adaptive: each page is rendered so its long side reaches
    TARGET_LONG_SIDE_PX (clamped between min_dpi and max_dpi). Dense scans —
    e.g. A4-landscape spreads holding two document pages side by side — need
    more pixels than a flat 150 DPI, otherwise small digits become unreadable
    for the vision model.
    """

    TARGET_LONG_SIDE_PX = 2600

    def __init__(self, min_dpi: int = 150, max_dpi: int = 300):
        self.min_dpi = min_dpi
        self.max_dpi = max_dpi

    def _scale(self, page: "pdfium.PdfPage") -> float:
        """Render scale in pixels per point (pdfium's baseline is 72 DPI)."""
        width, height = page.get_size()
        long_side_pts = max(width, height) or 1
        dpi = self.TARGET_LONG_SIDE_PX / long_side_pts * 72
        dpi = max(self.min_dpi, min(self.max_dpi, dpi))
        return dpi / 72

    def _render_jpeg(self, page: "pdfium.PdfPage") -> bytes:
        image = page.render(scale=self._scale(page)).to_pil().convert("RGB")
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=85)
        return buf.getvalue()

    def extract_pages(self, pdf_bytes: bytes) -> list[PageImage]:
        doc = pdfium.PdfDocument(pdf_bytes)
        total = len(doc)
        pages = []

        for page_num in range(total):
            page = doc[page_num]
            pages.append(
                PageImage(
                    page_number=page_num + 1,
                    image_bytes=self._render_jpeg(page),
                    total_pages=total,
                )
            )

        doc.close()
        return pages

    def extract_page(self, pdf_bytes: bytes, page_number: int) -> PageImage | None:
        """Extract a single page (1-based). Much faster than extract_pages for large docs."""
        doc = pdfium.PdfDocument(pdf_bytes)
        total = len(doc)
        idx = page_number - 1
        if idx < 0 or idx >= total:
            doc.close()
            return None
        image_bytes = self._render_jpeg(doc[idx])
        doc.close()
        return PageImage(page_number=page_number, total_pages=total, image_bytes=image_bytes)
