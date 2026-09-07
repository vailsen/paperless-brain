# pipelines/similar.py
"""Nearest-neighbour search for *documents* rather than for a query string.

`pipelines.search` ranks chunks against an embedded question. This ranks chunks
against the chunks of a document the user already has in front of them — "more
like this one", which is the question you cannot phrase as a query because you
would have to guess what makes the document distinctive in the first place.

Two details carry the result:

- **The source document's own vectors are the query.** Re-embedding its summary
  would compare a paraphrase to the archive; the stored chunk embeddings are the
  same vectors every other document was indexed with, so distances are
  comparable with those of `search`.
- **A neighbour is scored by its single best chunk, not by an average.** A
  fifteen-page contract and a one-page invoice about the same matter overlap in
  one passage; averaging would bury that under fourteen unrelated pages.
"""

import asyncio

from models.result_document import DocumentResult
from pipelines.search import _drop_no_ingest
from services.clients import chroma, paperless, sidecar_service
from services.paperless import PaperlessClient

# Chunks of the source document used as query vectors. A long document would
# otherwise fire one Chroma query per page for a result the first few pages
# already decide.
MAX_QUERY_CHUNKS = 8


async def find_similar_documents(
    document_id: int,
    n_results: int = 10,
    paperless_client: PaperlessClient | None = None,
) -> list[DocumentResult]:
    """Return documents whose embeddings sit closest to `document_id`'s.

    Empty when the document has no chunks in Chroma (never ingested, or carries
    the no-ingest tag) — the caller must say so rather than report "nothing
    similar found", which is a different claim.
    """
    pl = paperless_client or paperless

    own = await chroma.get(
        where={"paperless_id": {"$eq": document_id}},
        include=["embeddings", "metadatas"],
    )
    embeddings = [c["embedding"] for c in own if c.get("embedding") is not None]
    if not embeddings:
        return []

    # Neighbours are asked for per query chunk; ask for more than we return
    # because several of them will be the source document's own chunks.
    per_query = min(100, n_results * 5)
    hit_lists = await asyncio.gather(
        *(
            chroma.query_by_embedding(emb, n_results=per_query)
            for emb in embeddings[:MAX_QUERY_CHUNKS]
        ),
        return_exceptions=True,
    )

    best_score: dict[int, float] = {}
    best_chunk: dict[int, str] = {}
    for hits in hit_lists:
        if isinstance(hits, Exception):
            continue
        for hit in hits:
            pid = (hit.get("metadata") or {}).get("paperless_id")
            if pid is None or pid == document_id:
                continue
            dist = hit.get("distance")
            if dist is None:
                continue
            if pid not in best_score or dist < best_score[pid]:
                best_score[pid] = dist
                best_chunk[pid] = hit.get("document") or ""

    if not best_score:
        return []

    ranked = sorted(best_score, key=lambda pid: best_score[pid])[:n_results]
    try:
        docs = _drop_no_ingest(await pl.list_documents(ids=ranked))
    except Exception:
        return []
    doc_map = {d.id: d for d in docs}

    results: list[DocumentResult] = []
    for pid in ranked:
        doc = doc_map.get(pid)
        if doc is None:
            continue
        results.append(
            DocumentResult(
                document=doc,
                relevance_score=best_score[pid],
                matched_chunks=[best_chunk[pid]] if best_chunk.get(pid) else [],
                has_actions=sidecar_service.has_actions(pid),
            )
        )
    return results
