"""Text → vectors, for every collection. The only place an embedding model runs.

Chroma no longer embeds anything: every collection is opened without an
embedding function and `ChromaClient` hands it the vectors computed here. That
is what lets one model run on the server's CPU or behind a remote endpoint, and
what keeps Chroma from loading a model of its own next to ours.

**As few local models in RAM as the work needs.** A sentence-transformers
model is 2–3 GB in fp32. Loading a model drops every other one except the
pinned (`set_pinned`: the active model, and one whose index is being built on
this CPU while the active one keeps serving search). The admin's comparison
loads *exclusively* — one model at a time, the other cleared — and the active
model is loaded again afterwards.

**The query side carries the model's instruction, passages are embedded as
they are.** e5-instruct and Qwen3-Embedding both want an instruction before a
query and nothing before a passage; the prompt is per model
(`embedding_registry`), seeded by `default_query_prompt`.
"""

from __future__ import annotations

import gc
import logging
import threading
import time
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

E5_INSTRUCT_QUERY = (
    "Instruct: Given a search query, retrieve relevant passages that answer the query\nQuery: "
)
# Qwen3-Embedding's own `query` prompt (config_sentence_transformers.json).
QWEN3_QUERY = (
    "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:"
)

REMOTE_BATCH = 32
REMOTE_TIMEOUT_S = 120.0


class EmbeddingError(RuntimeError):
    """The model could not be loaded or the endpoint did not answer."""


def default_query_prompt(model_name: str) -> str:
    n = (model_name or "").lower()
    if "e5" in n and "instruct" in n:
        return E5_INSTRUCT_QUERY
    if "qwen" in n and "embed" in n:
        return QWEN3_QUERY
    return ""


# ── Local models (sentence-transformers, CPU) ─────────────────────────────────

_slot_lock = threading.Lock()
_models: dict[str, object] = {}   # name → loaded SentenceTransformer
_load_s: dict[str, float] = {}
_pinned: set[str] = set()         # the active model; one being built locally


def set_pinned(names: set[str]) -> None:
    """Models that stay loaded while another one is loaded (`embedding_registry`
    keeps this to the active model, plus a model whose index is being built
    locally — that build and the live search need both)."""
    _pinned.clear()
    _pinned.update(n for n in names if n)


def _free() -> None:
    gc.collect()
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass


def _load(name: str, exclusive: bool = False):
    """The model `name`, loaded. Every other loaded model that is not pinned is
    dropped first — with `exclusive`, pinned ones too (the comparison: one model
    in RAM at a time, timings without a neighbour)."""
    with _slot_lock:
        drop = [n for n in _models if n != name and (exclusive or n not in _pinned)]
        for n in drop:
            log.info("[embeddings] unloading %s", n)
            _models.pop(n, None)
        if drop:
            _free()
        if name in _models:
            return _models[name]
        from sentence_transformers import SentenceTransformer

        t0 = time.perf_counter()
        try:
            model = SentenceTransformer(name, device="cpu")
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingError(f"could not load {name}: {exc}") from exc
        _models[name] = model
        _load_s[name] = time.perf_counter() - t0
        log.info("[embeddings] loaded %s in %.1f s", name, _load_s[name])
        return model


def loaded_models() -> list[str]:
    """Local models in RAM right now."""
    return list(_models)


def drop_unpinned() -> None:
    """Free every loaded model that is neither active nor being built."""
    with _slot_lock:
        drop = [n for n in _models if n not in _pinned]
        for n in drop:
            log.info("[embeddings] unloading %s", n)
            _models.pop(n, None)
        if drop:
            _free()


def unload(name: str | None = None) -> None:
    """Drop `name` (every model when None) from RAM."""
    with _slot_lock:
        for n in [name] if name else list(_models):
            _models.pop(n, None)
        _free()


class LocalEmbedder:
    kind = "local"

    def __init__(self, name: str, query_prompt: str = "", exclusive: bool = False):
        self.name = name
        self.query_prompt = query_prompt
        self.exclusive = exclusive

    @property
    def key(self) -> str:
        return f"local:{self.name}"

    def _encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        model = _load(self.name, self.exclusive)
        vecs = model.encode(list(texts), batch_size=16, normalize_embeddings=True,
                            convert_to_numpy=True, show_progress_bar=False)
        return [v.tolist() for v in vecs]

    def passages(self, texts: list[str]) -> list[list[float]]:
        return self._encode(texts)

    def queries(self, texts: list[str]) -> list[list[float]]:
        return self._encode([self.query_prompt + t for t in texts])

    def warm(self) -> float:
        """Load the model now; seconds it took (0 when it was loaded already)."""
        if self.name in _models and not (
                self.exclusive and any(n != self.name for n in _models)):
            return 0.0
        had = self.name in _models
        _load(self.name, self.exclusive)
        return 0.0 if had else _load_s.get(self.name, 0.0)


# ── Remote endpoints (OpenAI-compatible /v1/embeddings) ───────────────────────


def normalize_base_url(url: str) -> str:
    """`http://host:11434` → `http://host:11434/v1`; a pasted `/embeddings` goes."""
    url = (url or "").strip().rstrip("/")
    if url.endswith("/embeddings"):
        url = url[: -len("/embeddings")]
    if url and urlparse(url).path in ("", "/"):
        url += "/v1"
    return url


class RemoteEmbedder:
    kind = "remote"

    def __init__(self, base_url: str, model: str, api_key: str = "", query_prompt: str = ""):
        self.base_url = normalize_base_url(base_url)
        self.model = model
        self.api_key = api_key
        self.query_prompt = query_prompt

    @property
    def key(self) -> str:
        return f"remote:{self.base_url}|{self.model}"

    def _post(self, texts: list[str]) -> list[list[float]]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            r = httpx.post(f"{self.base_url}/embeddings", headers=headers,
                           json={"model": self.model, "input": texts}, timeout=REMOTE_TIMEOUT_S)
            r.raise_for_status()
            data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingError(f"{self.base_url}: {exc}") from exc
        if len(data) != len(texts):
            raise EmbeddingError(f"{self.base_url}: {len(data)} vectors for {len(texts)} texts")
        try:
            from services.ollama_watchdog import touch
            from services.wol import wakes

            if wakes(self.base_url):
                touch()  # the host the watchdog sleeps is in use
        except Exception:  # noqa: BLE001
            pass
        return [d["embedding"] for d in data]

    def _encode(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), REMOTE_BATCH):
            out.extend(self._post(list(texts[i:i + REMOTE_BATCH])))
        return out

    def passages(self, texts: list[str]) -> list[list[float]]:
        return self._encode(texts)

    def queries(self, texts: list[str]) -> list[list[float]]:
        return self._encode([self.query_prompt + t for t in texts])

    def warm(self) -> float:
        return 0.0


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0
