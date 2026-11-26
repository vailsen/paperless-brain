"""
ChromaDB Wrapper
A clean, high-level interface for ChromaDB vector store operations.

Install: pip install chromadb
"""

import asyncio
import os
import threading
from typing import Any, Awaitable

import chromadb
import numpy as np

# Up to this many vectors a query compares against every one of them (a matrix
# product, a few ms for the archive's ~2 600 chunks) instead of walking Chroma's
# HNSW graph. HNSW is approximate and, in Chroma 1.5.9, not even deterministic
# across processes: the tail of a 100-chunk request differed between two runs
# of the same query. Exact search is the same answer every time.
EXACT_MAX = 50_000

# Per collection (persist dir, name): a write counter, and the vectors loaded
# for exact search with the counter and count they were loaded at. Module-level
# because several ChromaClient objects open the same collection (an index build
# writes through its own client).
_matrix_lock = threading.Lock()
_writes: dict[tuple[str, str], int] = {}
_matrices: dict[tuple[str, str], tuple[int, int, list[str], np.ndarray]] = {}


class ChromaClient:
    """
    A wrapper around ChromaDB for simplified vector store operations.

    Supports:
    - In-memory and persistent storage
    - Custom or built-in embedding functions
    - CRUD on documents
    - Similarity search with optional metadata filtering
    """

    def __init__(
        self,
        collection_name: str,
        persist_directory: str,
        embedder,
        distance_metric: str = "cosine",  # "cosine" | "l2" | "ip"
    ):
        """
        Args:
            collection_name:   Name of the ChromaDB collection.
            persist_directory: Path for persistent storage.
            embedder:          `services.embeddings` Local/RemoteEmbedder. Chroma
                               itself embeds nothing (see that module).
            distance_metric:   Distance function used for similarity search.
        """

        os.makedirs(persist_directory, exist_ok=True)
        self._persist = os.path.abspath(persist_directory)
        self.client = chromadb.PersistentClient(path=persist_directory)
        self.embedder = embedder
        self._space = distance_metric
        self.collection = self._open(collection_name)

    def _open(self, name: str):
        # embedding_function=None: Chroma must not build the function persisted
        # in an old collection's configuration (it would load a second copy of
        # the model). Text without vectors would fall back to Chroma's default
        # model, so every call below passes vectors.
        return self.client.get_or_create_collection(
            name=name,
            embedding_function=None,
            configuration={
                "hnsw": {
                    "space": self._space,
                    "ef_construction": 100,
                    "max_neighbors": 16,
                }
            },
        )

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def add(
        self,
        documents: list[str],
        ids: list[str],
        metadatas: list[dict] | None = None,
    ) -> list[str]:
        """
        Add documents to the collection.

        Args:
            documents: Plain-text documents to embed and store.
            metadatas: Optional per-document metadata dicts.
            ids:       Optional explicit IDs. Auto-generated (UUID4) if omitted.

        Returns:
            List of IDs assigned to the documents.
        """
        metadatas = metadatas or [{} for _ in documents]
        # Embedding inference (SentenceTransformer) runs inside the collection
        # call and is CPU-heavy — off the event loop, or every session freezes.
        embeddings = await asyncio.to_thread(self.embedder.passages, documents)
        await asyncio.to_thread(
            self.collection.add, documents=documents, embeddings=embeddings,
            metadatas=metadatas, ids=ids,
        )
        self._touched()
        return ids

    async def upsert(
        self,
        documents: list[str],
        ids: list[str],
        metadatas: list[dict] | None = None,
        embed_documents: list[str] | None = None,
    ) -> list[str]:
        """Add or update documents by ID.

        When ``embed_documents`` is given, those texts are embedded while
        ``documents`` is what gets stored and returned as the snippet. This lets
        a vault chunk embed its title/heading context (so title-only queries
        match) without polluting the displayed snippet. Passages are embedded
        raw — consistent with the e5-instruct "query prefix only" rule — so the
        embedding function is called directly on ``embed_documents``.
        """
        metadatas = metadatas or [{} for _ in documents]
        embeddings = await asyncio.to_thread(
            self.embedder.passages,
            embed_documents if embed_documents is not None else documents,
        )
        await asyncio.to_thread(
            self.collection.upsert,
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
            ids=ids,
        )
        self._touched()
        return ids

    async def upsert_vectors(
        self,
        ids: list[str],
        embeddings: list[list[float]],
        documents: list[str],
        metadatas: list[dict],
    ) -> None:
        """Store vectors computed elsewhere (index build, `embedding_index`)."""
        await asyncio.to_thread(
            self.collection.upsert, ids=ids, embeddings=embeddings,
            documents=documents, metadatas=metadatas,
        )
        self._touched()

    async def update(
        self,
        ids: list[str],
        documents: list[str] | None = None,
        metadatas: list[dict] | None = None,
        embed_documents: list[str] | None = None,
    ) -> None:
        """Update existing documents or their metadata.

        ``embed_documents`` mirrors ``upsert``: the stored text stays the clean
        document while the vector is computed from the context-prefixed variant.
        Without it an edited note would silently lose the header its neighbours
        still carry, and be embedded slightly differently from its own reindexed
        version.
        """
        kwargs: dict[str, Any] = {"ids": ids}
        if documents:
            kwargs["documents"] = documents
        if metadatas:
            kwargs["metadatas"] = metadatas
        if embed_documents is not None or documents:
            kwargs["embeddings"] = await asyncio.to_thread(
                self.embedder.passages,
                embed_documents if embed_documents is not None else documents,
            )
        await asyncio.to_thread(self.collection.update, **kwargs)
        self._touched()

    async def delete(
        self, ids: list[str] | None = None, where: dict | None = None
    ) -> None:
        """
        Delete documents by ID or metadata filter.

        Args:
            ids:   List of document IDs to delete.
            where: Metadata filter dict (ChromaDB `where` syntax).
        """
        kwargs: dict[str, Any] = {}
        if ids:
            kwargs["ids"] = ids
        if where:
            kwargs["where"] = where
        await asyncio.to_thread(self.collection.delete, **kwargs)
        self._touched()

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def query(
        self,
        query_texts: list[str],
        n_results: int = 5,
        where: dict | None = None,
        where_document: dict | None = None,
        include: list[str] | None = None,
        where_pending: Awaitable[dict | None] | None = None,
    ) -> list[list[dict]]:
        """
        Similarity search.

        Args:
            query_texts:     One or more query strings.
            n_results:       Number of results per query.
            where:           Metadata filter  e.g. {"source": {"$eq": "wiki"}}
            where_document:  Document content filter e.g. {"$contains": "python"}
            where_pending:   Awaitable of a further metadata filter, resolved
                             concurrently with the query embedding.
            include:         Fields to return. Default: documents, metadatas, distances.

        Returns:
            For each query, a list of result dicts with keys:
            id, document, metadata, distance.
        """
        include = include or ["documents", "metadatas", "distances"]
        embedding = asyncio.to_thread(self.embedder.queries, list(query_texts))
        if where_pending is None:
            vectors = await embedding
        else:
            # A filter that needs its own round trip (the reader's visible ids)
            # is fetched while the query is embedded, not after.
            vectors, extra = await asyncio.gather(embedding, where_pending)
            if extra:
                where = {"$and": [where, extra]} if where else extra
        raw = await asyncio.to_thread(
            self._search, vectors, n_results, where, where_document, include
        )
        return self._format_query_results(raw, len(query_texts))

    async def query_by_embedding(
        self,
        embedding: list[float],
        n_results: int = 10,
        where: dict | None = None,
        include: list[str] | None = None,
    ) -> list[dict]:
        """Nearest-neighbour search using an existing embedding vector."""
        include = include or ["documents", "metadatas", "distances"]
        raw = await asyncio.to_thread(self._search, [embedding], n_results, where, None, include)
        return self._format_query_results(raw, 1)[0]

    async def get(
        self,
        ids: list[str] | None = None,
        where: dict | None = None,
        limit: int | None = None,
        offset: int | None = None,
        include: list[str] | None = None,
        where_document: dict | None = None,
    ) -> list[dict]:
        """
        Fetch documents by ID, metadata filter and/or text filter
        (``where_document``, e.g. ``{"$regex": …}``) — no similarity ranking.

        Returns:
            List of dicts with keys: id, document, metadata.
        """
        kwargs: dict[str, Any] = {"include": ["documents", "metadatas"]}
        if ids:
            kwargs["ids"] = ids
        if where:
            kwargs["where"] = where
        if where_document:
            kwargs["where_document"] = where_document
        if limit is not None:
            kwargs["limit"] = limit
        if offset is not None:
            kwargs["offset"] = offset
        if include is not None:
            kwargs["include"] = include

        raw = await asyncio.to_thread(self.collection.get, **kwargs)
        ids = raw.get("ids") or []
        _docs = raw.get("documents"); documents = _docs if _docs is not None else [None] * len(ids)
        _meta = raw.get("metadatas"); metadatas = _meta if _meta is not None else [None] * len(ids)
        _embs = raw.get("embeddings"); embeddings = _embs if _embs is not None else [None] * len(ids)
        return [
            {"id": i, "document": d, "metadata": m, "embedding": e}
            for i, d, m, e in zip(ids, documents, metadatas, embeddings)
        ]

    async def count(self) -> int:
        """Return the total number of documents in the collection."""
        return await asyncio.to_thread(self.collection.count)

    async def peek(self, limit: int = 5) -> list[dict]:
        """Return the first `limit` documents (useful for debugging)."""
        raw = await asyncio.to_thread(self.collection.peek, limit=limit)
        return [
            {"id": i, "document": d, "metadata": m}
            for i, d, m in zip(raw["ids"], raw["documents"], raw["metadatas"])
        ]

    # ------------------------------------------------------------------
    # Collection management
    # ------------------------------------------------------------------

    async def reset(self) -> None:
        """Delete and recreate the collection (clears all data)."""
        name = self.collection.name
        await asyncio.to_thread(self.client.delete_collection, name)
        self.collection = await asyncio.to_thread(self._open, name)
        self._touched()

    # ------------------------------------------------------------------
    # Exact search
    # ------------------------------------------------------------------

    def _key(self) -> tuple[str, str]:
        return (self._persist, self.collection.name)

    def _touched(self) -> None:
        """Every write through any client invalidates the loaded vectors."""
        key = self._key()
        with _matrix_lock:
            _writes[key] = _writes.get(key, 0) + 1

    def _matrix(self, count: int) -> tuple[list[str], np.ndarray]:
        """All ids (sorted) and their unit vectors, loaded once per write."""
        key = self._key()
        with _matrix_lock:
            gen = _writes.get(key, 0)
            cached = _matrices.get(key)
            if cached and cached[0] == gen and cached[1] == count:
                return cached[2], cached[3]
        raw = self.collection.get(include=["embeddings"])
        order = sorted(range(len(raw["ids"])), key=lambda i: raw["ids"][i])
        ids = [raw["ids"][i] for i in order]
        emb = raw["embeddings"]
        mat = np.asarray(emb, dtype=np.float32)[order] if len(ids) else np.zeros((0, 0), np.float32)
        if len(ids):
            norms = np.linalg.norm(mat, axis=1, keepdims=True)
            mat = mat / np.where(norms == 0, 1, norms)
        with _matrix_lock:
            # A write that landed while loading leaves the counter ahead: the
            # next query loads again instead of trusting this copy.
            _matrices[key] = (gen, len(ids), ids, mat)
        return ids, mat

    def _search(
        self,
        vectors: list,
        n_results: int,
        where: dict | None,
        where_document: dict | None,
        include: list[str],
    ) -> dict:
        """Nearest chunks, in the shape of `collection.query`'s answer.

        Exact (every vector compared) for cosine collections up to `EXACT_MAX`;
        Chroma's HNSW otherwise. Filters are resolved by Chroma (its SQLite
        index), the ranking by the matrix product. Equal distances keep id
        order, so the answer never depends on the process.
        """
        count = self.collection.count()
        if self._space != "cosine" or count > EXACT_MAX:
            kwargs: dict[str, Any] = {
                "query_embeddings": vectors, "n_results": n_results, "include": include,
            }
            if where:
                kwargs["where"] = where
            if where_document:
                kwargs["where_document"] = where_document
            return self.collection.query(**kwargs)

        ids, mat = self._matrix(count)
        if where or where_document:
            kwargs = {"include": []}
            if where:
                kwargs["where"] = where
            if where_document:
                kwargs["where_document"] = where_document
            allowed = set(self.collection.get(**kwargs)["ids"])
            rows = np.fromiter((i for i, x in enumerate(ids) if x in allowed), dtype=np.int64)
        else:
            rows = np.arange(len(ids), dtype=np.int64)

        out: dict[str, list] = {"ids": [], "distances": [], "documents": [], "metadatas": []}
        want_docs = "documents" in include
        want_meta = "metadatas" in include
        for vec in vectors:
            q = np.asarray(vec, dtype=np.float32)
            nq = float(np.linalg.norm(q))
            if not len(rows) or nq == 0:
                top_ids, dist = [], []
            else:
                sims = mat[rows] @ (q / nq)
                k = min(n_results, len(rows))
                # Stable sort over id-ordered rows: ties resolve by id.
                best = np.argsort(-sims, kind="stable")[:k]
                top_ids = [ids[rows[i]] for i in best]
                dist = [float(1.0 - sims[i]) for i in best]
            out["ids"].append(top_ids)
            out["distances"].append(dist)
            if (want_docs or want_meta) and top_ids:
                got = self.collection.get(
                    ids=top_ids,
                    include=[x for x in ("documents", "metadatas") if x in include],
                )
                pos = {x: i for i, x in enumerate(got["ids"])}
                docs = got.get("documents") or [None] * len(got["ids"])
                metas = got.get("metadatas") or [None] * len(got["ids"])
                out["documents"].append([docs[pos[x]] if x in pos else None for x in top_ids])
                out["metadatas"].append([metas[pos[x]] if x in pos else None for x in top_ids])
            else:
                out["documents"].append([None] * len(top_ids))
                out["metadatas"].append([None] * len(top_ids))
        return out

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_query_results(raw: dict, n_queries: int) -> list[list[dict]]:
        results = []
        for i in range(n_queries):
            hits = []
            ids = raw.get("ids", [[]])[i]
            docs = raw.get("documents", [[]])[i]
            metas = raw.get("metadatas", [[]])[i]
            distances = raw.get("distances", [[]])[i]
            for doc_id, doc, meta, dist in zip(ids, docs, metas, distances):
                hits.append(
                    {"id": doc_id, "document": doc, "metadata": meta, "distance": dist}
                )
            results.append(hits)
        return results


# ---------------------------------------------------------------------------
# Quick demo
# ---------------------------------------------------------------------------

# if __name__ == "__main__":
#     db = ChromaClient("demo", persist_directory=None)

#     ids = db.add(
#         documents=[
#             "Python is a high-level programming language.",
#             "ChromaDB is an open-source vector database.",
#             "Embeddings represent text as dense numeric vectors.",
#             "FastAPI is a modern web framework for Python.",
#             "Vector search finds semantically similar documents.",
#         ],
#         metadatas=[
#             {"topic": "python"},
#             {"topic": "database"},
#             {"topic": "ml"},
#             {"topic": "python"},
#             {"topic": "database"},
#         ],
#     )
#     print(f"Inserted {db.count()} documents\n")

#     results = db.query(["What is a vector database?"], n_results=3)
#     print("Top 3 results for 'What is a vector database?'")
#     for hit in results[0]:
#         print(f"  [{hit['distance']:.4f}] {hit['document']}")

#     print("\nFiltered to topic=python:")
#     filtered = db.query(
#         ["programming language"], n_results=2, where={"topic": {"$eq": "python"}}
#     )
#     for hit in filtered[0]:
#         print(f"  [{hit['distance']:.4f}] {hit['document']}")
