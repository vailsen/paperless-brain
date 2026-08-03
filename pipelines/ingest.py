# pipelines/ingest.py
import io
import re
from typing import Callable

from PIL import Image

# Trivial references: lone § / $ followed by only a number (+ optional single letter).
_TRIVIAL_REF_RE = re.compile(r"^[§$]\s*\d+[a-zA-Z]?\s*$")


def _is_trivial_ref(ref: dict) -> bool:
    return bool(_TRIVIAL_REF_RE.match(ref.get("value", "").strip()))


def _is_specific_value(val: str) -> bool:
    """True when value has at least one non-digit non-space char AND at least one digit.

    Specific values are deduplicated across 'type' labels.
    Pure numbers (e.g. year "2024") are NOT specific — same year can appear under
    different types and must not be collapsed.
    """
    return any(c.isdigit() for c in val) and any(
        not c.isdigit() and not c.isspace() for c in val
    )


from config.extraction_rules import PROMPT_VERSION
from models.extraction import ExtractedContent, JsonSidecar
from services.chroma import ChromaClient
from services.chunking import build_context_prefix, chunk_document
from services.paperless import PaperlessClient
from services.pdf_extractor import PDFExtractor
from services.sidecar_service import SidecarService
from services.thumbnail_service import ThumbnailService
from services.vision import VisionClient


def png_to_jpeg(data: bytes) -> list[bytes]:
    img = Image.open(io.BytesIO(data))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG")
    return [buf.getvalue()]


async def ingest_document(
    doc_id: int,
    paperless: PaperlessClient,
    chroma: ChromaClient,
    vision: VisionClient,
    sidecar_service: SidecarService,
    thumbnail_service: ThumbnailService,
    log: Callable[[str], None] | None = None,
):
    """pipeline for ingesting a paperless document by id into the vector database.

    ``log`` receives per-page progress. A long document is minutes of silence
    otherwise, which is indistinguishable from a hung sync.
    """

    def _log(msg: str) -> None:
        if log:
            log(msg)

    document = await paperless.get_document(doc_id)
    doc_bytes = await paperless.download_document(doc_id)
    thumb_bytes = await thumbnail_service.get_thumbnail(doc_id)

    doc_type = document.document_type

    pdf_extractor = PDFExtractor()
    images = pdf_extractor.extract_pages(doc_bytes)

    extracted_pages: list[ExtractedContent] = []
    ai_doc_type = ""
    prev_table: dict | None = None
    for idx, image in enumerate(images):
        # with open("output.jpg", "wb") as f:
        #     f.write(image.image_bytes)
        _log(f"    page {idx + 1}/{len(images)}…")
        try:
            extracted = await vision.analyze_document(
                page=image,
                document_type=doc_type,
                ai_doc_type=ai_doc_type,
                prev_table=prev_table,
            )
        except Exception as exc:
            print(
                f"[WARN] doc {doc_id} page {image.page_number}: "
                f"vision extraction failed ({type(exc).__name__}: {exc}) — skipping page"
            )
            extracted = ExtractedContent(
                page=image.page_number,
                page_text="",
                tables=[],
                actions=[],
                page_summary="",
                cross_references=[],
                document_type=ai_doc_type,
            )
        if idx == 0:
            ai_doc_type = extracted.document_type
        extracted.tables = [t for t in extracted.tables if t.get("rows")]
        prev_table = extracted.tables[-1] if extracted.tables else None
        extracted_pages.append(extracted)

    # Building data pieces for embedding and json sidecar
    doc_full_text = ""
    all_tables = []
    all_pages = []
    all_actions = []
    all_cross_references = []
    full_summary = ""
    for extr_page in extracted_pages:
        page_num = extr_page.page
        ai_doc_type = extr_page.document_type
        doc_full_text += "\n"
        doc_full_text += extr_page.page_text
        # Merge tables that overflow from the previous page: the model marks the
        # continuation via "continued_from_previous_page"; its rows are appended
        # to the previously collected table instead of forming a new entry.
        # Only the first table of a page can be a continuation.
        for t_idx, table in enumerate(extr_page.tables):
            continued = bool(table.pop("continued_from_previous_page", False))
            if continued and t_idx == 0 and all_tables:
                all_tables[-1]["rows"].extend(table.get("rows", []))
            else:
                all_tables.append({"page_number": page_num, **table})
        page_dict = {
            "page_number": page_num,
            "page_text": extr_page.page_text,
            "page_summary": extr_page.page_summary,
        }
        all_pages.append(page_dict)
        all_actions.extend(extr_page.actions)
        all_cross_references.extend(extr_page.cross_references)
        full_summary += "\n"
        full_summary += extr_page.page_summary

    # Deduplicate actions — the same deadline/action mentioned on several pages
    # (often paraphrased) would otherwise show up multiple times in the
    # dashboard / get_actions.
    from services.action_dedupe import dedupe_actions

    all_actions = dedupe_actions(all_actions)

    # Deduplicate and filter cross-references.
    # 1. Drop trivial bare-paragraph refs like "§1", "$ 3".
    # 2. For specific values (mixed letters/symbols + digits): dedupe by value alone
    #    so that the same ref listed under different type labels is collapsed.
    # 3. For plain numbers (e.g. years): keep (type, value) dedup — same year can
    #    legitimately appear under different types.
    _seen_pair: set = set()
    _seen_val: set = set()
    _unique_refs: list = []
    for _ref in all_cross_references:
        if _is_trivial_ref(_ref):
            continue
        _val = _ref.get("value", "")
        _typ = _ref.get("type", "")
        if _is_specific_value(_val):
            if _val in _seen_val:
                continue
            _seen_val.add(_val)
        else:
            _key = (_typ, _val)
            if _key in _seen_pair:
                continue
            _seen_pair.add(_key)
        _unique_refs.append(_ref)
    all_cross_references = _unique_refs

    # condense page-by-page summaries into one document-level summary
    _log("    summarizing…")
    full_summary_summarized = await vision.summarize_document(full_summary)

    # chunking and embedding
    prefix = build_context_prefix(
        document_type=ai_doc_type,
        correspondent=document.correspondent,
        document_date=document.created,
    )
    page_texts = [page.page_text for page in extracted_pages]
    chunks = chunk_document(page_texts=page_texts, context_prefix=prefix)

    sidecar = JsonSidecar(
        paperless_id=doc_id,
        full_text=doc_full_text,
        full_summary=full_summary,
        full_summary_summarized=full_summary_summarized,
        pages=all_pages,
        tables=all_tables,
        actions=all_actions,
        cross_refs=all_cross_references,
        chunks=len(chunks),
        prompt_version=PROMPT_VERSION,
    )

    # → ready for ChromaDB
    _log(f"    embedding {len(chunks)} chunk(s)…")
    res = await chroma.upsert(
        ids=[f"paperless_{doc_id}_chunk_{c.chunk_index}" for c in chunks],
        documents=[c.text for c in chunks],
        metadatas=[
            {
                "paperless_id": doc_id,
                "chunk_index": c.chunk_index,
                "page_number": c.page_number,
            }
            for c in chunks
        ],
    )
    sidecar_service.save_sidecar(sidecar)
    thumbnail_service.save_thumbnail(thumb_bytes, doc_id)

    from services.clients import cross_ref_index as _idx

    _idx.add_document(doc_id, all_cross_references)

    print(res)
