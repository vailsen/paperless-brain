# services/doc_image_service.py

"""Pictures of document pages for the chat — a whole page or one cropped picture.

The single gate is the user's own Paperless access: sidecars and the render
cache below have no access control of their own, so every request first asks
Paperless with the *user's* token whether the document is visible to them. Not
visible and not existing look the same from outside (``None`` → 404), so a
request never confirms that someone else's document exists.

Renders are cached on disk under ``data/image_cache/{doc_id}/{modified}/``. The
document's ``modified`` timestamp is part of the path, so a re-scanned or
re-OCRed document gets fresh renders without anyone invalidating anything; the
old directory simply ages out of the size cap.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import threading
from pathlib import Path

from config.settings import settings
from services.page_images import page_images, render_page_jpeg

_log = logging.getLogger(__name__)

CACHE_DIR = settings.app_path / "data" / "image_cache"
CACHE_MAX_BYTES = 500 * 1024 * 1024

# Long side of a whole-page render. Plenty for a chat bubble and a tap-to-zoom;
# the 2600 px ingest render would be 4× the bytes for nothing.
PAGE_LONG_SIDE = 1600
CROP_LONG_SIDE = 1200

# pdfium is not thread-safe; renders run off the event loop but one at a time.
_pdfium_lock = threading.Lock()
# One download per document at a time — a page with three pictures asks for the
# same PDF three times within milliseconds.
_doc_locks: dict[int, asyncio.Lock] = {}


def _stamp(modified) -> str:
    try:
        return str(int(modified.timestamp()))
    except Exception:
        return "0"


def _prune(cache_dir: Path, max_bytes: int) -> None:
    """Drop the least recently used files until the cache fits."""
    files = []
    total = 0
    for root, _dirs, names in os.walk(cache_dir):
        for name in names:
            p = Path(root) / name
            try:
                st = p.stat()
            except OSError:
                continue
            files.append((st.st_atime, st.st_size, p))
            total += st.st_size
    if total <= max_bytes:
        return
    for _atime, size, p in sorted(files):
        try:
            p.unlink()
        except OSError:
            continue
        total -= size
        if total <= max_bytes:
            break


def _touch(path: Path) -> None:
    """Mark a cache hit as recently used (atime is often mounted noatime)."""
    try:
        os.utime(path)
    except OSError:
        pass


def crop_bbox(doc_id: int, page: int, index: int | None) -> list[float] | None:
    """The stored box of picture ``index`` on a page, or None (→ whole page)."""
    if index is None:
        return None
    from services.clients import sidecar_service

    sidecar = sidecar_service.load_sidecar(doc_id) or {}
    for p in sidecar.get("pages") or []:
        if p.get("page_number") == page:
            images = page_images(p)
            if 0 <= index < len(images):
                return images[index].get("bbox") or None
    return None


async def get_image(
    doc_id: int,
    page: int,
    index: int | None,
    token: str,
    cache_dir: Path | None = None,
) -> bytes | None:
    """JPEG of a page or of one picture on it, or None when the user may not see it.

    An index that does not exist (or a picture without a box) falls back to the
    whole page: the user asked to see something on that page, and the page is
    always a correct answer — a 404 would be a broken image in the chat.
    """
    if not token or page < 1:
        return None
    from services.paperless import PaperlessClient

    client = PaperlessClient(settings.paperless_url, token)
    try:
        docs = await client.list_documents(ids=[doc_id])
    except Exception as exc:
        _log.warning("doc-image: access check for #%s failed: %s", doc_id, exc)
        return None
    if not docs:
        return None

    base = (cache_dir or CACHE_DIR) / str(doc_id) / _stamp(docs[0].modified)
    bbox = crop_bbox(doc_id, page, index)
    name = f"{page}_{index}.jpg" if bbox else f"{page}.jpg"
    target = base / name
    if target.is_file():
        _touch(target)
        return target.read_bytes()

    lock = _doc_locks.setdefault(doc_id, asyncio.Lock())
    async with lock:
        if target.is_file():
            return target.read_bytes()
        pdf_path = base / "source.pdf"
        if pdf_path.is_file():
            pdf_bytes = pdf_path.read_bytes()
        else:
            try:
                pdf_bytes = await client.download_document(doc_id)
            except Exception as exc:
                _log.warning("doc-image: download of #%s failed: %s", doc_id, exc)
                return None
            base.mkdir(parents=True, exist_ok=True)
            pdf_path.write_bytes(pdf_bytes)

        def _render() -> bytes | None:
            with _pdfium_lock:
                return render_page_jpeg(
                    pdf_bytes,
                    page,
                    bbox,
                    long_side=CROP_LONG_SIDE if bbox else PAGE_LONG_SIDE,
                )

        try:
            data = await asyncio.to_thread(_render)
        except Exception as exc:
            _log.warning("doc-image: render of #%s p%s failed: %s", doc_id, page, exc)
            return None
        if data is None:
            return None
        base.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
    await asyncio.to_thread(_prune, cache_dir or CACHE_DIR, CACHE_MAX_BYTES)
    return data


def image_url(doc_id: int, page: int, index: int | None = None) -> str:
    url = f"/api/doc-image/{int(doc_id)}/{int(page)}"
    return f"{url}?i={int(index)}" if index is not None else url


# ![alt](/api/doc-image/12/3?i=0) — what show_document_image puts into answers.
DOC_IMAGE_MD_RE = re.compile(
    r"!\[([^\]]*)\]\(/api/doc-image/(\d+)/(\d+)(?:\?i=(\d+))?\)"
)


async def embed_images(markdown: str, token: str) -> str:
    """Inline the chat's document pictures as data URIs, for the PDF export.

    WeasyPrint renders from a string with no base URL, so a relative
    /api/doc-image link would silently become a missing image. Each picture is
    fetched through ``get_image`` — the same access check as the browser — and a
    picture the user may not see is dropped, not leaked into the PDF.
    """
    out = []
    pos = 0
    for m in DOC_IMAGE_MD_RE.finditer(markdown or ""):
        out.append(markdown[pos:m.start()])
        pos = m.end()
        doc_id, page = int(m.group(2)), int(m.group(3))
        index = int(m.group(4)) if m.group(4) is not None else None
        data = await get_image(doc_id, page, index, token)
        if data:
            b64 = base64.b64encode(data).decode("ascii")
            out.append(f"![{m.group(1)}](data:image/jpeg;base64,{b64})")
    out.append((markdown or "")[pos:])
    return "".join(out)
