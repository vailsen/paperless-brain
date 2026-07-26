"""
ChromaDB Wrapper
A clean, high-level interface for ChromaDB vector store operations.

Install: pip install chromadb
"""

import asyncio
import os
from typing import Any

import chromadb
from chromadb.utils import embedding_functions


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
        embedding_function: str,
        distance_metric: str = "cosine",  # "cosine" | "l2" | "ip"
    ):
        """
        Args:
            collection_name:   Name of the ChromaDB collection.
            persist_directory: Path for persistent storage. None = in-memory.
            embedding_function: A ChromaDB-compatible embedding function.
            distance_metric:   Distance function used for similarity search.
        """

        os.makedirs(persist_directory, exist_ok=True)
        self.client = chromadb.PersistentClient(path=persist_directory)

        self.ef = self._build_embedding_function(embedding_function)
        # e5-instruct models require an instruct QUERY prefix; passages stay raw.
        # Our stored documents are raw (only a semantic context prefix at most),
        # so we only need to prefix the query side — no re-embedding required.
        self._instruct = "e5" in (embedding_function or "").lower() and "instruct" in (embedding_function or "").lower()

        self.collection = self.client.get_or_create_collection(
            name=collection_name,
            embedding_function=self.ef,
            configuration={
                "hnsw": {
                    "space": distance_metric,
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
        await asyncio.to_thread(
            self.collection.add, documents=documents, metadatas=metadatas, ids=ids
        )
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
        if embed_documents is not None:
            embeddings = await asyncio.to_thread(self.ef, embed_documents)
            await asyncio.to_thread(
                self.collection.upsert,
                documents=documents,
                embeddings=embeddings,
                metadatas=metadatas,
                ids=ids,
            )
        else:
            await asyncio.to_thread(
                self.collection.upsert, documents=documents, metadatas=metadatas, ids=ids
            )
        return ids

    async def update(
        self,
        ids: list[str],
        documents: list[str] | None = None,
        metadatas: list[dict] | None = None,
    ) -> None:
        """Update existing documents or their metadata."""
        kwargs: dict[str, Any] = {"ids": ids}
        if documents:
            kwargs["documents"] = documents
        if metadatas:
            kwargs["metadatas"] = metadatas
        await asyncio.to_thread(self.collection.update, **kwargs)

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
    ) -> list[list[dict]]:
        """
        Similarity search.

        Args:
            query_texts:     One or more query strings.
            n_results:       Number of results per query.
            where:           Metadata filter  e.g. {"source": {"$eq": "wiki"}}
            where_document:  Document content filter e.g. {"$contains": "python"}
            include:         Fields to return. Default: documents, metadatas, distances.

        Returns:
            For each query, a list of result dicts with keys:
            id, document, metadata, distance.
        """
        include = include or ["documents", "metadatas", "distances"]
        kwargs: dict[str, Any] = {
            "query_texts": [self._q(t) for t in query_texts],
            "n_results": n_results,
            "include": include,
        }
        if where:
            kwargs["where"] = where
        if where_document:
            kwargs["where_document"] = where_document

        raw = await asyncio.to_thread(self.collection.query, **kwargs)
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
        kwargs: dict[str, Any] = {
            "query_embeddings": [embedding],
            "n_results": n_results,
            "include": include,
        }
        if where:
            kwargs["where"] = where
        raw = await asyncio.to_thread(self.collection.query, **kwargs)
        return self._format_query_results(raw, 1)[0]

    async def get(
        self,
        ids: list[str] | None = None,
        where: dict | None = None,
        limit: int | None = None,
        offset: int | None = None,
        include: list[str] | None = None,
    ) -> list[dict]:
        """
        Fetch documents by ID or metadata filter (no similarity ranking).

        Returns:
            List of dicts with keys: id, document, metadata.
        """
        kwargs: dict[str, Any] = {"include": ["documents", "metadatas"]}
        if ids:
            kwargs["ids"] = ids
        if where:
            kwargs["where"] = where
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
        meta = self.collection.metadata
        await asyncio.to_thread(self.client.delete_collection, name)
        self.collection = await asyncio.to_thread(
            self.client.get_or_create_collection,
            name=name,
            embedding_function=self.ef,
            metadata=meta,
        )

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

    def _q(self, text: str) -> str:
        """Apply the e5-instruct query prefix. Passages stay raw (embedded as-is).

        Without this the query is embedded like a passage → distances become
        indistinct and irrelevant chunks outrank relevant ones (the exact
        multilingual-e5-large-instruct failure mode).
        """
        if self._instruct:
            return (
                "Instruct: Given a search query, retrieve relevant passages that "
                f"answer the query\nQuery: {text}"
            )
        return text

    @staticmethod
    def _build_embedding_function(embedding_function):
        return embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=embedding_function
        )


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
