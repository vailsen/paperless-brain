"""The admin's embedding models and which one's index the app searches.

Stored in `werkbank_settings` (key `embedding_registry`, global, not per user):

    {"active": "<model id>",
     "models": [{
        "id", "name",            # name = Hugging Face id, run by sentence-transformers
        "query_prompt",          # put before every query (passages stay raw)
        "remote_url", "remote_model", "remote_key",   # optional OpenAI-compatible endpoint
        "use": "local" | "remote",                    # which of the two embeds
        "suffix",                # collection names: <base> or <base>__<suffix>
        "index": "none" | "building" | "paused" | "ready",
        "progress": {...}, "synced_at": iso, "parity": {...},
        "thresholds": {"archive_hint": float, "fact_duplicate": float, "brain_hint": float},
     }]}

**One model, one index, local or remote.** Local and remote are two ways to
run the same model; the index belongs to the model. A remote endpoint serves
a converted copy (Ollama's GGUF), so switching an index between the two needs
a passed parity check (`embedding_index.parity`) first.

**Exactly one model is active** and its index is what every search reads. The
first read seeds the registry from `EMBEDDING_MODEL` with the collections that
already exist (`suffix` ""), so an existing install changes nothing. After
that, `EMBEDDING_MODEL` is ignored.

Only `embedding_index` changes `active` and `index` — activating goes through
a catch-up, deleting never touches the active index.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import threading
from datetime import datetime, timezone

from services import embeddings as emb

log = logging.getLogger(__name__)

KEY = "embedding_registry"
ROLES = ("documents", "brain", "vault")
_BASE = {"brain": "brain", "vault": "vault"}  # documents: CHROMA_COLLECTION

# Distances tuned on e5-large-instruct; another model spreads distances
# differently, so each model carries its own (set from the comparison page).
DEFAULT_THRESHOLDS = {
    "archive_hint": 0.15,    # web_search: the archive covers this topic
    "fact_duplicate": 0.15,  # remember_fact: "a very similar fact exists"
}
THRESHOLD_KEYS = ("archive_hint", "fact_duplicate", "brain_hint")

PARITY_MIN_MEAN = 0.99  # cosine local↔remote on the same texts

_lock = threading.Lock()
_cache: dict | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _store():
    from werkbank import settings_store

    return settings_store


def _seed() -> dict:
    from config.settings import settings

    name = settings.embedding_model
    m = _new_model(name, suffix="")
    m["index"] = "ready"
    m["synced_at"] = now_iso()
    return {"active": m["id"], "models": [m]}


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9.-]+", "-", text.lower().split("/")[-1]).strip("-.")
    return s[:48] or "model"


def _new_model(name: str, suffix: str | None = None, existing: list[dict] = ()) -> dict:
    ids = {m["id"] for m in existing}
    sufs = {m.get("suffix", "") for m in existing}
    base = _slug(name)
    mid, n = base, 2
    while mid in ids or (suffix is None and mid in sufs):
        mid, n = f"{base}-{n}", n + 1
    return {
        "id": mid,
        "name": name.strip(),
        "query_prompt": emb.default_query_prompt(name),
        "remote_url": "",
        "remote_model": "",
        "remote_key": "",
        "use": "local",
        "suffix": mid if suffix is None else suffix,
        "index": "none",
        "progress": {},
        "synced_at": "",
        "parity": {},
        "thresholds": {},
    }


def _read_raw() -> str | None:
    """The stored JSON; "" when there is none; None when the table cannot be read.

    `settings_store.get` answers "" for both, and a locked database read as
    "nothing stored" would reseed and drop every model the admin added."""
    import sqlite3

    from werkbank.repository import _DB_PATH

    try:
        with sqlite3.connect(_DB_PATH, timeout=10) as conn:
            row = conn.execute(
                "SELECT value FROM werkbank_settings WHERE key = ?", (KEY,)).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else ""


def load() -> dict:
    """The registry (a copy — change it through the functions below)."""
    global _cache
    with _lock:
        if _cache is None:
            raw = _read_raw()
            if raw is None:  # before init_db, or the database is busy: don't decide now
                return _seed()
            reg = None
            if raw:
                try:
                    reg = json.loads(raw)
                except ValueError:
                    log.error("embedding registry unreadable, reseeding")
            if not reg or not reg.get("models"):
                reg = _seed()
                _store().set_value(KEY, json.dumps(reg))
            _cache = reg
            _apply_pins(reg)
        return copy.deepcopy(_cache)


def save(reg: dict) -> None:
    global _cache
    if not any(m["id"] == reg.get("active") for m in reg.get("models", [])):
        raise ValueError("the active model must exist")
    with _lock:
        _store().set_value(KEY, json.dumps(reg))
        _cache = copy.deepcopy(reg)
        _apply_pins(reg)


def _apply_pins(reg: dict) -> None:
    """Keep the active model, and one being built on this CPU, in RAM."""
    names = {m["name"] for m in reg["models"]
             if m.get("use", "local") == "local"
             and (m["id"] == reg["active"] or m.get("index") == "building")}
    emb.set_pinned(names)


def invalidate() -> None:
    global _cache
    with _lock:
        _cache = None


def models() -> list[dict]:
    return load()["models"]


def get(model_id: str) -> dict | None:
    return next((m for m in models() if m["id"] == model_id), None)


def active_id() -> str:
    return load()["active"]


def active_model() -> dict:
    reg = load()
    return next(m for m in reg["models"] if m["id"] == reg["active"])


def update(model_id: str, **fields) -> dict:
    """Change fields of one model and save. Returns the model."""
    reg = load()
    for m in reg["models"]:
        if m["id"] == model_id:
            m.update(fields)
            save(reg)
            return m
    raise KeyError(model_id)


def collection_name(model: dict, role: str) -> str:
    from config.settings import settings

    base = settings.chroma_collection if role == "documents" else _BASE[role]
    return f"{base}__{model['suffix']}" if model.get("suffix") else base


# ── Admin edits ───────────────────────────────────────────────────────────────


def add_model(name: str, query_prompt: str | None = None, remote_url: str = "",
              remote_model: str = "", api_key: str = "") -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("name required")
    reg = load()
    if any(m["name"] == name for m in reg["models"]):
        raise ValueError("model exists")
    m = _new_model(name, existing=reg["models"])
    if query_prompt is not None:
        m["query_prompt"] = query_prompt
    _set_remote(m, remote_url, remote_model, api_key)
    reg["models"].append(m)
    save(reg)
    return m


def _set_remote(m: dict, url: str, model: str, api_key: str | None) -> None:
    m["remote_url"] = emb.normalize_base_url(url) if url else ""
    m["remote_model"] = (model or "").strip()
    if api_key is not None:
        from services.session_auth import seal

        m["remote_key"] = seal(api_key) if api_key else ""
    if not m["remote_url"]:
        m["use"] = "local"


def edit_model(model_id: str, *, query_prompt: str, remote_url: str, remote_model: str,
               api_key: str | None) -> dict:
    """`api_key` None keeps the stored key. A changed endpoint voids the parity check."""
    reg = load()
    m = next(x for x in reg["models"] if x["id"] == model_id)
    before = (m["remote_url"], m["remote_model"])
    m["query_prompt"] = query_prompt
    _set_remote(m, remote_url, remote_model, api_key)
    if (m["remote_url"], m["remote_model"]) != before:
        m["parity"] = {}
    save(reg)
    return m


def remove_model(model_id: str) -> None:
    reg = load()
    m = next(x for x in reg["models"] if x["id"] == model_id)
    if reg["active"] == model_id:
        raise ValueError("the active model cannot be removed")
    if m["index"] != "none":
        raise ValueError("delete its index first")
    reg["models"] = [x for x in reg["models"] if x["id"] != model_id]
    save(reg)


def has_remote(m: dict) -> bool:
    return bool(m.get("remote_url") and m.get("remote_model"))


def parity_ok(m: dict) -> bool:
    p = m.get("parity") or {}
    return (bool(p) and p.get("endpoint") == f"{m['remote_url']}|{m['remote_model']}"
            and p.get("mean", 0) >= PARITY_MIN_MEAN)


def can_switch_use(m: dict, use: str) -> tuple[bool, str]:
    """May the model embed with `use` from now on? (reason when not)"""
    if use == m.get("use"):
        return True, ""
    if use == "remote" and not has_remote(m):
        return False, "no_remote"
    if m["index"] != "none" and not parity_ok(m):
        return False, "parity"
    return True, ""


def set_use(model_id: str, use: str) -> dict:
    m = get(model_id)
    ok, why = can_switch_use(m, use)
    if not ok:
        raise ValueError(why)
    return update(model_id, use=use)


def set_thresholds(model_id: str, values: dict) -> dict:
    clean = {k: float(v) for k, v in values.items() if k in THRESHOLD_KEYS and v is not None}
    return update(model_id, thresholds=clean)


# ── Use ───────────────────────────────────────────────────────────────────────


def api_key(m: dict) -> str:
    if not m.get("remote_key"):
        return ""
    from services.session_auth import open_sealed

    try:
        return open_sealed(m["remote_key"])
    except ValueError:
        log.warning("embedding endpoint key of %s no longer decrypts", m["id"])
        return ""


_embedders: dict[str, object] = {}


def embedder(m: dict, use: str | None = None):
    """The embedder for model `m` (its configured `use` unless given)."""
    use = use or m.get("use", "local")
    if use == "remote" and has_remote(m):
        key = f"r|{m['remote_url']}|{m['remote_model']}|{m['query_prompt']}|{m.get('remote_key', '')}"
        if key not in _embedders:
            _embedders[key] = emb.RemoteEmbedder(m["remote_url"], m["remote_model"],
                                                 api_key(m), m["query_prompt"])
        return _embedders[key]
    key = f"l|{m['name']}|{m['query_prompt']}"
    if key not in _embedders:
        _embedders[key] = emb.LocalEmbedder(m["name"], m["query_prompt"])
    return _embedders[key]


def threshold(key: str, default: float | None = None) -> float | None:
    """The active model's value for `key`; `default` (or the e5 value) when unset."""
    try:
        v = active_model().get("thresholds", {}).get(key)
    except Exception:  # noqa: BLE001
        v = None
    if v is not None:
        return float(v)
    return DEFAULT_THRESHOLDS.get(key, default) if default is None else default
