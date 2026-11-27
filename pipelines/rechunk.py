# pipelines/rechunk.py

"""A document's chunks, built from its sidecar — ingest and rebuild alike.

**No vision model.** The sidecar holds every page's text and picture
descriptions; the chunks are cut from that again and embedded with the active
model. Ingest uses the same functions, so a rebuilt chunk is the chunk a fresh
ingest would write.

**What makes chunks outdated** is their metadata `header` below
`chunking.HEADER_VERSION` (version 1 chunks have none), or a new title: the
title is part of the header, and AI titling renames a document right after its
ingest (`after_rename`).

The context line (type, correspondent, date) of an existing document is kept
as ingest wrote it (`context` metadata, or parsed from a version-1 header):
its type is the vision model's reading, which the sidecar does not store.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from services.chunking import (
    HEADER_VERSION,
    Chunk,
    build_header,
    chunk_document,
    context_from_v1,
)
from services.page_images import embedding_lines

log = logging.getLogger(__name__)


def page_texts(pages: list[dict]) -> list[str]:
    """What is embedded per page: its text, plus `[Image n: …]` lines.

    Picture descriptions go into the embedding only — `page_text` is pushed
    back into Paperless by the text write-back and stays the document's text.
    """
    out = []
    for pg in pages:
        text = pg.get("page_text") or ""
        lines = embedding_lines(pg.get("images") or [])
        out.append(f"{text}\n\n{lines}".strip() if lines else text)
    return out


def _budget():
    from services import embedding_registry as reg
    from services import embeddings as emb

    try:
        return emb.token_budget(reg.active_model()["name"])
    except Exception:  # noqa: BLE001
        log.warning("[rechunk] no token budget", exc_info=True)
        return None, emb.MAX_CHUNK_TOKENS


def build_chunks(pages: list[str], context: str, title: str | None, summary: str | None) -> list[Chunk]:
    """Header + token-fitted chunks. CPU work: call it off the event loop."""
    token_len, window = _budget()
    header = build_header(context, title, summary)
    return chunk_document(pages, context_prefix=header, token_len=token_len, max_tokens=window)


def metadata(doc_id: int, chunk: Chunk, context: str, title: str | None) -> dict:
    return {
        "paperless_id": doc_id,
        "chunk_index": chunk.chunk_index,
        "page_number": chunk.page_number,
        "header": HEADER_VERSION,
        "context": context,
        "title": title or "",
    }


async def write_chunks(chroma, doc_id: int, chunks: list[Chunk], context: str, title: str | None) -> None:
    """Upsert the chunks; drop the document's chunks beyond the new count."""
    if chunks:
        await chroma.upsert(
            ids=[f"paperless_{doc_id}_chunk_{c.chunk_index}" for c in chunks],
            documents=[c.text for c in chunks],
            metadatas=[metadata(doc_id, c, context, title) for c in chunks],
        )
    old = await chroma.get(where={"paperless_id": {"$eq": doc_id}}, include=["metadatas"])
    stale = [x["id"] for x in old if int((x["metadata"] or {}).get("chunk_index", 0)) >= len(chunks)]
    if stale:
        await chroma.delete(ids=stale)


async def _stored(chroma, doc_id: int) -> list[dict]:
    items = await chroma.get(where={"paperless_id": {"$eq": doc_id}})
    return sorted(items, key=lambda x: int((x["metadata"] or {}).get("chunk_index", 0)))


async def rechunk_document(doc_id: int, paperless, chroma, sidecar_service, title: str | None = None) -> int | None:
    """Rebuild one document's chunks from its sidecar. Returns the chunk count,
    None when there is nothing to rebuild from (no sidecar, no stored chunk)."""
    sidecar = sidecar_service.load_sidecar(doc_id)
    stored = await _stored(chroma, doc_id)
    if not sidecar or not stored:
        return None
    meta = stored[0]["metadata"] or {}
    context = meta.get("context")
    if context is None:
        context = context_from_v1(stored[0]["document"])
    if title is None:
        title = (await paperless.get_document(doc_id)).title
    summary = sidecar.get("full_summary_summarized") or sidecar.get("full_summary") or ""
    chunks = await asyncio.to_thread(
        build_chunks, page_texts(sidecar.get("pages") or []), context, title, summary
    )
    if not chunks:
        return None
    await write_chunks(chroma, doc_id, chunks, context, title)
    if sidecar.get("chunks") != len(chunks):
        from models.extraction import JsonSidecar

        sidecar["chunks"] = len(chunks)
        try:
            sidecar_service.save_sidecar(JsonSidecar(**sidecar))
        except Exception:  # noqa: BLE001 — the count is informational
            log.warning("[rechunk] #%s: sidecar chunk count not saved", doc_id, exc_info=True)
    return len(chunks)


async def outdated_ids(chroma) -> list[int]:
    """Documents with a chunk below `HEADER_VERSION`, newest id first."""
    items = await chroma.get(include=["metadatas"])
    out = {
        int(m["paperless_id"])
        for m in (x["metadata"] or {} for x in items)
        if m.get("paperless_id") is not None and int(m.get("header") or 1) < HEADER_VERSION
    }
    return sorted(out, reverse=True)


async def rechunk_all(
    paperless,
    chroma,
    sidecar_service,
    *,
    progress: Callable[[int, int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
    wait: Callable[[], object] | None = None,
) -> dict:
    """Rebuild every outdated document. Resumable: a stop or restart leaves the
    rest outdated, and the next run picks up there."""
    ids = await outdated_ids(chroma)
    titles = {d.id: d.title for d in await paperless.list_documents(ids=ids)} if ids else {}
    done = chunks = failed = 0
    for doc_id in ids:
        if should_stop and should_stop():
            break
        if wait is not None:
            await wait()
        try:
            n = await rechunk_document(doc_id, paperless, chroma, sidecar_service,
                                       title=titles.get(doc_id))
            chunks += n or 0
        except Exception:  # noqa: BLE001 — one document must not end the run
            failed += 1
            log.exception("[rechunk] #%s failed", doc_id)
        done += 1
        if progress:
            progress(done, len(ids), chunks)
    return {"documents": done, "total": len(ids), "chunks": chunks, "failed": failed}


async def after_rename(doc_id: int, new_title: str) -> None:
    """A renamed document's header names the old title: rebuild its chunks."""
    from services.clients import chroma, paperless, sidecar_service

    try:
        await rechunk_document(doc_id, paperless, chroma, sidecar_service, title=new_title)
    except Exception:  # noqa: BLE001 — the rename stands; the next rebuild catches up
        log.warning("[rechunk] #%s after rename failed", doc_id, exc_info=True)
