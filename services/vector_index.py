"""Chroma collections per embedding model, and the "active index" every caller uses.

`services.clients.chroma`, `brain._chroma` and `vault_chroma` are `ActiveCollection`s:
each attribute access resolves the active model's `ChromaClient` for that role.
Switching the active model (`embedding_index.activate`) therefore takes effect
on the next call — no restart, and the dozens of `from services.clients import
chroma` keep working.
"""

from __future__ import annotations

import threading

from services import embedding_registry as reg
from services.chroma import ChromaClient

_lock = threading.Lock()
_clients: dict[tuple[str, str], ChromaClient] = {}


def _persist_dir() -> str:
    from config.settings import settings

    return str(settings.app_path / settings.chroma_path)


def client_for(model: dict, role: str, use: str | None = None) -> ChromaClient:
    """The collection of `model` for `role`, embedding with its current endpoint
    (or `use`, for a one-off comparison — not cached)."""
    name = reg.collection_name(model, role)
    if use is not None:
        return ChromaClient(name, _persist_dir(), reg.embedder(model, use))
    with _lock:
        c = _clients.get((model["id"], role))
        if c is None or c.collection.name != name:
            c = ChromaClient(name, _persist_dir(), reg.embedder(model))
            _clients[(model["id"], role)] = c
        else:
            c.embedder = reg.embedder(model)
        return c


def forget(model_id: str) -> None:
    """Drop cached clients of a model whose collections were deleted."""
    with _lock:
        for key in [k for k in _clients if k[0] == model_id]:
            _clients.pop(key, None)


def active_client(role: str) -> ChromaClient:
    return client_for(reg.active_model(), role)


class ActiveCollection:
    """Stands in for the active model's `ChromaClient` of one role."""

    def __init__(self, role: str):
        object.__setattr__(self, "_role", role)

    def _target(self) -> ChromaClient:
        return active_client(object.__getattribute__(self, "_role"))

    def __getattr__(self, name):
        return getattr(self._target(), name)

    def __setattr__(self, name, value):
        setattr(self._target(), name, value)

    def __repr__(self) -> str:
        return f"<ActiveCollection {object.__getattribute__(self, '_role')}>"
