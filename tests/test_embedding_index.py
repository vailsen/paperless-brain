"""Embedding models: registry, building an index from the stored chunks,
activation, deletion, and the RAM rule for local models."""

import asyncio
import hashlib
import sys
import types

import pytest

from services import embedding_index as ei
from services import embedding_registry as reg
from services import embeddings as emb
from services import vector_index


class FakeEmbedder:
    kind = "local"

    def __init__(self, tag: str):
        self.tag = tag
        self.seen: list[str] = []

    def _vec(self, text: str) -> list[float]:
        h = hashlib.sha256(f"{self.tag}|{text}".encode()).digest()
        return [b / 255 + 0.01 for b in h[:8]]

    def passages(self, texts):
        self.seen.extend(texts)
        return [self._vec(t) for t in texts]

    def queries(self, texts):
        return [self._vec(t) for t in texts]

    def warm(self):
        return 0.0


@pytest.fixture
def registry(monkeypatch):
    from werkbank.repository import init_db

    init_db()
    from werkbank import settings_store

    settings_store.set_value(reg.KEY, "")
    reg.invalidate()
    vector_index._clients.clear()
    fakes: dict[str, FakeEmbedder] = {}

    def fake_embedder(m, use=None):
        key = f"{m['id']}|{use or m.get('use')}"
        return fakes.setdefault(key, FakeEmbedder(m["id"]))

    monkeypatch.setattr(reg, "embedder", fake_embedder)
    yield fakes
    # Drop every collection this test made.
    for m in reg.models():
        for role in reg.ROLES:
            c = vector_index.client_for(m, role)
            try:
                c.client.delete_collection(c.collection.name)
            except Exception:
                pass
    vector_index._clients.clear()
    settings_store.set_value(reg.KEY, "")
    reg.invalidate()


def test_seed_keeps_the_existing_collections(registry):
    m = reg.active_model()
    assert m["name"] == "intfloat/multilingual-e5-large-instruct"
    assert m["index"] == "ready"
    assert [reg.collection_name(m, r) for r in reg.ROLES] == ["test_documents", "brain", "vault"]
    assert m["query_prompt"].startswith("Instruct:")


def test_a_new_model_gets_its_own_collections(registry):
    q = reg.add_model("Qwen/Qwen3-Embedding-0.6B")
    assert reg.collection_name(q, "documents") == "test_documents__qwen3-embedding-0.6b"
    assert q["query_prompt"] == emb.QWEN3_QUERY
    with pytest.raises(ValueError):
        reg.add_model("Qwen/Qwen3-Embedding-0.6B")
    with pytest.raises(ValueError):
        reg.remove_model(reg.active_id())
    reg.update(q["id"], index="ready")
    with pytest.raises(ValueError):
        reg.remove_model(q["id"])


def test_switching_an_index_to_remote_needs_a_parity_check(registry):
    q = reg.add_model("Qwen/Qwen3-Embedding-0.6B", remote_url="http://gpu:11434",
                      remote_model="qwen3-embedding:0.6b")
    assert q["remote_url"] == "http://gpu:11434/v1"
    assert reg.can_switch_use(q, "remote") == (True, "")  # nothing built yet
    reg.update(q["id"], index="ready")
    q = reg.get(q["id"])
    assert reg.can_switch_use(q, "remote") == (False, "parity")
    reg.update(q["id"], parity={"endpoint": "http://gpu:11434/v1|qwen3-embedding:0.6b",
                                "mean": 0.995})
    assert reg.can_switch_use(reg.get(q["id"]), "remote") == (True, "")
    # A changed endpoint voids the check.
    reg.edit_model(q["id"], query_prompt=q["query_prompt"], remote_url="http://other:11434",
                   remote_model="qwen3-embedding:0.6b", api_key=None)
    assert reg.can_switch_use(reg.get(q["id"]), "remote") == (False, "parity")


def _fill_active():
    a = reg.active_model()

    async def go():
        docs = vector_index.client_for(a, "documents")
        await docs.upsert(ids=["paperless_1_chunk_0", "paperless_2_chunk_0"],
                          documents=["Dokument: Rechnung — Laptop 1299 €", "Mietvertrag Kündigung"],
                          metadatas=[{"paperless_id": 1, "chunk_index": 0, "page_number": 1},
                                     {"paperless_id": 2, "chunk_index": 0, "page_number": 1}])
        vault = vector_index.client_for(a, "vault")
        await vault.upsert(ids=["n1:0"], documents=["Bremsbeläge wechseln"],
                           metadatas=[{"path": "To-Dos/Auto.md", "heading_path": "Bremsen",
                                       "user": "u", "pbrain_id": "n1"}])
        brain = vector_index.client_for(a, "brain")
        await brain.upsert(ids=["f1"], documents=["Drehmomentschlüssel VANPO"],
                           metadatas=[{"path": "PaperlessBrain Memory/Werkzeug.md", "user": "u"}])

    asyncio.run(go())
    return a


def test_build_copies_the_stored_text_and_embeds_it_again(registry):
    a = _fill_active()
    q = reg.add_model("Qwen/Qwen3-Embedding-0.6B")

    async def go():
        src = vector_index.client_for(a, "vault")
        dst = vector_index.client_for(q, "vault")
        fake = reg.embedder(q)
        r = await ei.sync_role(src, dst, "vault", fake)
        assert r == {"source": 1, "embedded": 1, "deleted": 0}
        # The vault header is rebuilt from path and heading, as the writers do.
        assert fake.seen == ["[To-Dos] Auto › Bremsen\n\nBremsbeläge wechseln"]
        got = await dst.get(ids=["n1:0"])
        assert got[0]["document"] == "Bremsbeläge wechseln"
        assert got[0]["metadata"]["path"] == "To-Dos/Auto.md"
        # Nothing changed: nothing embedded.
        assert (await ei.sync_role(src, dst, "vault", fake))["embedded"] == 0
        # Changed text is embedded again; a deleted entry goes.
        await src.upsert(ids=["n1:0"], documents=["Bremsbeläge vorne wechseln"],
                         metadatas=[{"path": "To-Dos/Auto.md", "heading_path": "Bremsen",
                                     "user": "u", "pbrain_id": "n1"}])
        assert (await ei.sync_role(src, dst, "vault", fake))["embedded"] == 1
        await src.delete(ids=["n1:0"])
        assert (await ei.sync_role(src, dst, "vault", fake))["deleted"] == 1
        assert await dst.count() == 0

    asyncio.run(go())


def test_brain_text_is_embedded_without_the_memory_folder(registry):
    assert ei.embed_text_for("brain", "Drehmomentschlüssel VANPO",
                             {"path": "PaperlessBrain Memory/Werkzeug.md"}) == (
        "Werkzeug\n\nDrehmomentschlüssel VANPO")
    assert ei.embed_text_for("documents", "Dokument: x", {"path": "a"}) == "Dokument: x"


def test_activate_catches_up_then_switches(registry):
    a = _fill_active()
    q = reg.add_model("Qwen/Qwen3-Embedding-0.6B")
    from services.clients import chroma

    async def go():
        ei.start(q["id"], "build")
        await ei._job.task
        assert reg.get(q["id"])["index"] == "ready"
        assert chroma.collection.name == "test_documents"  # build alone switches nothing
        # A document arriving while the new index sat ready is caught up on activation.
        await vector_index.client_for(a, "documents").upsert(
            ids=["paperless_3_chunk_0"], documents=["Steuerbescheid"],
            metadatas=[{"paperless_id": 3, "chunk_index": 0, "page_number": 1}])
        with pytest.raises(ValueError):
            await ei.delete_index(a["id"])  # the active one, never
        ei.start(q["id"], "activate")
        await ei._job.task
        assert reg.active_id() == q["id"]
        assert chroma.collection.name == "test_documents__qwen3-embedding-0.6b"
        assert await chroma.count() == 3
        # The old index stays as it was, dormant.
        assert reg.get(a["id"])["index"] == "ready"
        await ei.delete_index(a["id"])
        assert reg.get(a["id"])["index"] == "none"
        with pytest.raises(ValueError):
            await ei.delete_index(q["id"])

    asyncio.run(go())


def test_ranking_and_overlap():
    hits = [
        {"distance": 0.30, "metadata": {"paperless_id": 5, "page_number": 2}, "document": "b"},
        {"distance": 0.10, "metadata": {"paperless_id": 5, "page_number": 1}, "document": "a"},
        {"distance": 0.20, "metadata": {"paperless_id": 7, "page_number": 1}, "document": "c"},
    ]
    docs = ei.rank_documents(hits, 10)
    assert [(d["id"], d["page"]) for d in docs] == [(5, 1), (7, 1)]
    assert ei.overlap(docs, [{"id": 7}]) == 0.5


def test_a_local_model_load_drops_others_except_pinned(monkeypatch):
    loads = []

    class ST:
        def __init__(self, name, device=None):
            loads.append(name)

    monkeypatch.setitem(sys.modules, "sentence_transformers",
                        types.SimpleNamespace(SentenceTransformer=ST))
    emb.unload()
    emb.set_pinned({"active"})
    emb._load("active")
    emb._load("b")
    assert set(emb.loaded_models()) == {"active", "b"}
    emb._load("c")  # b was not pinned
    assert set(emb.loaded_models()) == {"active", "c"}
    emb._load("b", exclusive=True)  # the comparison: one model alone
    assert emb.loaded_models() == ["b"]
    emb.unload()
    emb.set_pinned(set())


def test_remote_base_url_is_normalized():
    assert emb.normalize_base_url("http://gpu:11434") == "http://gpu:11434/v1"
    assert emb.normalize_base_url("http://gpu:11434/v1/embeddings") == "http://gpu:11434/v1"
    assert emb.normalize_base_url("https://api.x.com/v1/") == "https://api.x.com/v1"


def test_after_a_comparison_only_the_active_model_stays(monkeypatch):
    class ST:
        def __init__(self, name, device=None):
            pass

    monkeypatch.setitem(sys.modules, "sentence_transformers",
                        types.SimpleNamespace(SentenceTransformer=ST))
    emb.unload()
    emb.set_pinned({"active"})
    emb._load("candidate", exclusive=True)
    assert emb.loaded_models() == ["candidate"]
    emb.drop_unpinned()
    emb._load("active")
    assert emb.loaded_models() == ["active"]
    emb.unload()
    emb.set_pinned(set())
