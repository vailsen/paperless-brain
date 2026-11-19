"""ChromaClient ranks by comparing every vector (exact), not by HNSW."""

import asyncio

import numpy as np
import pytest

from services import chroma as chroma_mod
from services.chroma import ChromaClient

DIM = 16


class FakeEmbedder:
    """Text "v:<seed>" → a fixed random vector; anything else → hashed seed."""

    def _vec(self, text: str) -> list[float]:
        seed = int(text.split(":", 1)[1]) if text.startswith("v:") else abs(hash(text)) % 10_000
        return np.random.default_rng(seed).normal(size=DIM).tolist()

    def passages(self, texts):
        return [self._vec(t) for t in texts]

    def queries(self, texts):
        return [self._vec(t) for t in texts]


@pytest.fixture
def client(tmp_path):
    c = ChromaClient("exact_test", str(tmp_path), FakeEmbedder())
    texts = [f"v:{i}" for i in range(200)]
    asyncio.run(c.upsert(
        documents=texts,
        ids=[f"id{i:03d}" for i in range(200)],
        metadatas=[{"paperless_id": i % 20, "chunk_index": i} for i in range(200)],
    ))
    return c


def _reference(c: ChromaClient, query: str, allowed=None, n=10):
    raw = c.collection.get(include=["embeddings"])
    q = np.asarray(FakeEmbedder()._vec(query))
    q = q / np.linalg.norm(q)
    scored = []
    for i, e in zip(raw["ids"], raw["embeddings"]):
        if allowed is not None and i not in allowed:
            continue
        e = np.asarray(e) / np.linalg.norm(e)
        scored.append((1 - float(e @ q), i))
    return [i for _, i in sorted(scored)[:n]]


def test_query_is_exact_nearest_neighbours(client):
    hits = asyncio.run(client.query(["v:5000"], n_results=10))[0]
    assert [h["id"] for h in hits] == _reference(client, "v:5000")
    assert all(h["document"] and h["metadata"] for h in hits)
    assert hits == sorted(hits, key=lambda h: h["distance"])


def test_distances_match_chromas_own(client):
    """Same distance scale as HNSW: callers' thresholds keep their meaning."""
    vec = FakeEmbedder()._vec("v:4242")
    hnsw = client.collection.query(query_embeddings=[vec], n_results=5,
                                   include=["distances"])
    ours = asyncio.run(client.query(["v:4242"], n_results=5))[0]
    assert [h["id"] for h in ours] == hnsw["ids"][0]
    assert [h["distance"] for h in ours] == pytest.approx(hnsw["distances"][0], abs=1e-5)


def test_metadata_filter_is_applied_before_ranking(client):
    where = {"paperless_id": {"$in": [3, 7]}}
    hits = asyncio.run(client.query(["v:777"], n_results=50, where=where))[0]
    allowed = {f"id{i:03d}" for i in range(200) if i % 20 in (3, 7)}
    assert len(hits) == len(allowed) == 20
    assert [h["id"] for h in hits] == _reference(client, "v:777", allowed, n=50)


def test_document_filter_is_applied(client):
    hits = asyncio.run(client.query(["v:1"], n_results=10,
                                    where_document={"$contains": "v:19"}))[0]
    assert {h["id"] for h in hits} == {"id019", "id190", "id191", "id192", "id193",
                                       "id194", "id195", "id196", "id197", "id198"}


def test_no_match_returns_nothing(client):
    hits = asyncio.run(client.query(["v:1"], n_results=5, where={"paperless_id": -1}))[0]
    assert hits == []


def test_write_through_another_client_is_seen(client, tmp_path):
    asyncio.run(client.query(["v:9"], n_results=1))  # loads the matrix
    other = ChromaClient("exact_test", str(tmp_path), FakeEmbedder())
    asyncio.run(other.upsert(documents=["v:9"], ids=["zzz"], metadatas=[{"paperless_id": 99}]))
    hits = asyncio.run(client.query(["v:9"], n_results=2))[0]
    assert {h["id"] for h in hits} == {"zzz", "id009"}
    asyncio.run(other.delete(ids=["zzz"]))
    assert asyncio.run(client.query(["v:9"], n_results=1))[0][0]["id"] == "id009"


def test_query_by_embedding_uses_the_same_path(client):
    vec = FakeEmbedder()._vec("v:31337")
    hits = asyncio.run(client.query_by_embedding(vec, n_results=7))
    assert [h["id"] for h in hits] == _reference(client, "v:31337", n=7)


def test_large_collections_fall_back_to_hnsw(client, monkeypatch):
    monkeypatch.setattr(chroma_mod, "EXACT_MAX", 10)
    called = []
    real = client.collection.query
    monkeypatch.setattr(client.collection, "query", lambda **kw: called.append(1) or real(**kw))
    asyncio.run(client.query(["v:1"], n_results=3))
    assert called
