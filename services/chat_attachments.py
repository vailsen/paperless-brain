"""Files the user uploads into a chat — a PDF or a picture that is not in
Paperless, to ask about it.

- **Nothing goes to Paperless, Chroma or the vault.** The file is read once,
  in memory; what stays is its text, inside the user's message.
- **The text lives in the stored message**, as a `<pb-attachment>` block after
  the user's words. The history keeps only `role` + `content`, and the model
  must still see the file on the next question — and an unchanged message
  keeps the model's prompt cache valid, where a block that came and went would
  not. The page shows a chip instead of the block (`split`).
- **A PDF's own text layer first**; the vision model (the ingestion model,
  `services.vision.build_vision_client`) reads only pages without one — scans
  — and pictures. The same extraction prompt as ingest, so tables, stamps and
  handwriting are read the way the archive's documents are.
- **No reference numbers.** Citations open pages of Paperless documents
  (`[^241-p3]`); an upload has no id, so the block tells the model to name the
  page in words.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass

_log = logging.getLogger("chat_attachments")

MAX_BYTES = 25 * 1024 * 1024
#: Pages read per file. A longer PDF is cut, and the block says so.
MAX_PAGES = 30
#: Characters of one file that go into the conversation (~15k tokens).
MAX_CHARS = 60_000
#: A PDF page with fewer non-space characters than this is a scan: vision.
MIN_TEXT_LAYER_CHARS = 80
#: Long side of a picture sent to the vision model (as PDFExtractor renders).
IMAGE_LONG_SIDE_PX = 2600

ACCEPT = "application/pdf,image/*,.pdf,.jpg,.jpeg,.png,.webp,.gif,.bmp,.tif,.tiff"

NOTE = (
    "[A file the user uploaded into this chat. It is NOT in the Paperless archive and has no "
    "reference numbers: cite it in words (\"the uploaded file, page 2\"), never with [^…].]"
)

_BLOCK_RE = re.compile(
    r'\n*<pb-attachment name="([^"]*)" pages="(\d+)"[^>]*>\n.*?\n</pb-attachment>', re.S)


class AttachmentError(Exception):
    """Shown to the user as is."""


@dataclass
class Attachment:
    name: str
    pages: int
    text: str
    truncated: bool = False
    vision_pages: int = 0


# ── Message format ────────────────────────────────────────────────────────────

def _safe_name(name: str) -> str:
    name = re.sub(r'[\s"<>]+', " ", name or "").strip()
    return name[:120] or "file"


def to_block(att: Attachment) -> str:
    body = att.text.replace("</pb-attachment>", "</pb-attachment >")
    if att.truncated:
        body += "\n[… the rest of the file was not read.]"
    return (f'<pb-attachment name="{_safe_name(att.name)}" pages="{att.pages}">\n'
            f"{NOTE}\n{body}\n</pb-attachment>")


def compose(text: str, attachments: list[Attachment]) -> str:
    """The stored user message: the user's words first (conversation titles
    are cut from the start), then one block per file."""
    blocks = "\n\n".join(to_block(a) for a in attachments)
    return f"{text.strip()}\n\n{blocks}" if blocks else text.strip()


def split(content: str) -> tuple[str, list[dict]]:
    """(the user's own words, [{name, pages}]) — what the page shows."""
    files = [{"name": m.group(1), "pages": int(m.group(2))}
             for m in _BLOCK_RE.finditer(content or "")]
    return _BLOCK_RE.sub("", content or "").strip(), files


def strip_for_title(content: str, keep_attachment_text: bool = False) -> str:
    """The user's words, with each file as "[file: name]" — for titles, the
    tool history heading, exports. `keep_attachment_text` keeps the blocks
    (a draft written from the conversation may need the file's content)."""
    if keep_attachment_text:
        return content or ""
    text, files = split(content)
    marks = " ".join(f"[{f['name']}]" for f in files)
    return f"{text} {marks}".strip() if marks else text


# ── Reading ───────────────────────────────────────────────────────────────────

def _pdf_pages(data: bytes, _) -> tuple[int, list[str]]:
    import pypdfium2 as pdfium

    try:
        doc = pdfium.PdfDocument(data)
    except Exception as exc:  # pdfium raises its own error type
        raise AttachmentError(_("not a readable PDF")) from exc
    try:
        total = len(doc)
        texts = []
        for i in range(min(total, MAX_PAGES)):
            tp = doc[i].get_textpage()
            texts.append(tp.get_text_range() or "")
            tp.close()
        return total, texts
    finally:
        doc.close()


def _image_jpeg(data: bytes, _) -> bytes:
    from PIL import Image, ImageOps

    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception as exc:
        raise AttachmentError(_("not a picture this server can read")) from exc
    scale = IMAGE_LONG_SIDE_PX / max(img.size)
    if scale < 1:
        img = img.resize((round(img.width * scale), round(img.height * scale)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _vision_text(result) -> str:
    """Page text plus the pictures' descriptions, as the embedding gets them."""
    parts = [(result.page_text or "").strip()]
    for i, im in enumerate(result.images or []):
        desc = (im.get("description") or "").strip() if isinstance(im, dict) else ""
        if desc:
            parts.append(f"[Image {i + 1}: {desc}]")
    return "\n".join(p for p in parts if p)


def is_pdf(data: bytes, filename: str, content_type: str) -> bool:
    return data[:5] == b"%PDF-" or content_type == "application/pdf" or filename.lower().endswith(".pdf")


async def extract(
    data: bytes,
    filename: str,
    content_type: str = "",
    *,
    username: str = "",
    token: str = "",
    on_progress: Callable[[int, int], None] | None = None,
    _=lambda s: s,
) -> Attachment:
    """Read one uploaded file. Raises AttachmentError with a short reason."""
    if not data:
        raise AttachmentError(_("the file is empty"))
    if len(data) > MAX_BYTES:
        raise AttachmentError(_("larger than {n} MB").format(n=MAX_BYTES // (1024 * 1024)))

    from models.extraction import PageImage

    is_image = False
    if is_pdf(data, filename, content_type):
        total, texts = await asyncio.to_thread(_pdf_pages, data, _)
        if total == 0:
            raise AttachmentError(_("the PDF has no pages"))
        scans = [i for i, t in enumerate(texts) if len(re.sub(r"\s", "", t)) < MIN_TEXT_LAYER_CHARS]
    elif (content_type or "").startswith("image/") or re.search(
            r"\.(jpe?g|png|webp|gif|bmp|tiff?)$", filename.lower()):
        total, texts, scans, is_image = 1, [""], [0], True
    else:
        raise AttachmentError(_("only PDFs and pictures can be attached"))

    vision = None
    if scans:
        from services.pdf_extractor import PDFExtractor
        from services.vision import build_vision_client

        vision = build_vision_client(username, token)
        jpeg = await asyncio.to_thread(_image_jpeg, data, _) if is_image else None
        for n, i in enumerate(scans):
            if on_progress:
                on_progress(n, len(scans))
            if jpeg is not None:
                page = PageImage(page_number=1, total_pages=1, image_bytes=jpeg)
            else:
                page = await asyncio.to_thread(PDFExtractor().extract_page, data, i + 1)
                if page is None:
                    continue
            try:
                result = await vision.analyze_document(page, "", "")
            except Exception as exc:
                _log.warning("vision failed on an uploaded page", exc_info=True)
                raise AttachmentError(
                    _("the vision model could not read it ({err})").format(err=exc)) from exc
            texts[i] = _vision_text(result)
        if on_progress:
            on_progress(len(scans), len(scans))

    pages = [f"--- Page {i + 1} ---\n{t.strip()}" for i, t in enumerate(texts)] if total > 1 \
        else [texts[0].strip()]
    text = "\n\n".join(pages).strip()
    truncated = total > len(texts)
    if len(text) > MAX_CHARS:
        text, truncated = text[:MAX_CHARS], True
    if not text:
        raise AttachmentError(_("no text could be read from it"))
    return Attachment(name=filename or "file", pages=total, text=text,
                      truncated=truncated, vision_pages=len(scans))
