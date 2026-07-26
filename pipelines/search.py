# pipelines/search.py

import asyncio
import re as _re

from models.result_document import DocumentResult
from services.clients import chroma, paperless, sidecar_service
from services.paperless import PaperlessClient

_BOOL_OPS = {"OR", "AND", "NOT", "UND", "ODER"}


def _drop_no_ingest(docs: list) -> list:
    """Filter out docs carrying the no-ingest tag.

    These were deliberately excluded from ingestion (same rule as
    paperless_db_sync), so they have no Chroma chunks and must not surface via
    the metadata-only search path either — otherwise search_exact leaks docs the
    rest of the app treats as non-existent.
    """
    from werkbank.settings_store import get_no_ingest_tag
    tag = get_no_ingest_tag()
    if not tag:
        return docs
    return [d for d in docs if tag not in (d.tags or [])]


def _tag_candidates(query: str) -> list[str]:
    tokens = _re.split(r"[\s,]+", query.strip())
    return [t for t in tokens if len(t) > 2 and t.upper() not in _BOOL_OPS]


async def _tag_search(
    pl: PaperlessClient,
    query: str,
    owner: int | None,
) -> dict[int, object]:
    """Return {paperless_id: doc} for docs whose tags match any keyword in query."""
    try:
        candidates = _tag_candidates(query)
        if not candidates:
            return {}
        tag_map = await pl.get_tag_map()  # {id: name}
        low = {c.lower() for c in candidates}
        matching_ids = [tid for tid, tname in tag_map.items() if tname.lower() in low]
        if not matching_ids:
            return {}
        docs = await pl.list_documents(tag_ids_any=matching_ids, owner=owner)
        return {d.id: d for d in _drop_no_ingest(docs)}
    except Exception:
        return {}


async def search(
    filters: dict,
    semantic_query: str | None = None,
    n_results: int = 20,
    paperless_client: PaperlessClient | None = None,
    where_document: dict | None = None,
    owner: int | None = None,
) -> list[DocumentResult]:
    """Unified search. paperless_client overrides the default (Superuser) client."""
    pl = paperless_client or paperless
    text_query = filters.get("query") or ""
    _filters = dict(filters)
    if owner is not None:
        _filters["owner"] = owner

    if not semantic_query and not where_document:
        docs = _drop_no_ingest(await pl.list_documents(**_filters))
        return [
            DocumentResult(
                document=doc,
                has_actions=sidecar_service.has_actions(doc.id),
                text_query=text_query,
            )
            for doc in docs
        ]

    # ── Semantic / ChromaDB path ──────────────────────────────────────────────
    effective_semantic = semantic_query or text_query
    has_filters = any(v is not None for v in _filters.values())
    chunk_request = n_results * 5

    if has_filters:
        # Fetch Paperless candidates AND tag hits in parallel
        docs, tag_doc_map = await asyncio.gather(
            pl.list_documents(**_filters),
            _tag_search(pl, effective_semantic, owner),
        )
        docs = _drop_no_ingest(docs)
        if not docs and not tag_doc_map:
            return []
        candidate_ids = [doc.id for doc in docs]
        doc_map: dict[int, object] = {doc.id: doc for doc in docs}
        doc_map.update(tag_doc_map)  # tag hits merged early so they can be fetched below

        if candidate_ids:
            hits = await chroma.query(
                query_texts=[effective_semantic],
                n_results=min(chunk_request, len(candidate_ids) * 5),
                where={"paperless_id": {"$in": candidate_ids}},
                where_document=where_document,
            )
        else:
            hits = [[]]
    else:
        # Run ChromaDB query and tag search in parallel
        hits, tag_doc_map = await asyncio.gather(
            chroma.query(
                query_texts=[effective_semantic],
                n_results=chunk_request,
                where_document=where_document,
            ),
            _tag_search(pl, effective_semantic, owner),
        )
        doc_map = {}

    best_score: dict[int, float] = {}
    all_chunks: dict[int, list[str]] = {}
    for hit in hits[0]:
        pid = hit["metadata"]["paperless_id"]
        score = hit["distance"]
        if pid not in best_score or score < best_score[pid]:
            best_score[pid] = score
        all_chunks.setdefault(pid, []).append(hit["document"])

    sorted_pids = sorted(best_score, key=lambda pid: best_score[pid])[:n_results]

    if not has_filters:
        semantic_ids = sorted_pids
        # Also need to fetch tag-only docs not in semantic results
        tag_only_ids = [pid for pid in tag_doc_map if pid not in best_score]
        all_fetch_ids = list(dict.fromkeys(semantic_ids + tag_only_ids))
        if all_fetch_ids:
            fetched = await pl.list_documents(ids=all_fetch_ids)
            doc_map = {doc.id: doc for doc in fetched}
    else:
        tag_only_ids = [pid for pid in tag_doc_map if pid not in best_score]

    results = []
    for pid in sorted_pids:
        doc = doc_map.get(pid)
        if doc is None:
            continue
        results.append(
            DocumentResult(
                document=doc,
                relevance_score=best_score[pid],
                matched_chunks=all_chunks.get(pid, []),
                has_actions=sidecar_service.has_actions(pid),
                text_query=text_query,
            )
        )

    # Append tag-matched docs not found by semantic search
    for pid in tag_only_ids:
        if len(results) >= n_results:
            break
        doc = doc_map.get(pid)
        if doc is None:
            continue
        results.append(
            DocumentResult(
                document=doc,
                relevance_score=0.5,  # synthetic score — tag hit, not semantic
                has_actions=sidecar_service.has_actions(pid),
                text_query=text_query,
            )
        )

    return results
