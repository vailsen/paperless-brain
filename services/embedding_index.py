"""Build, catch up, activate and delete an embedding model's index; compare two.

**No vision model, no re-chunking.** Every chunk is already stored in the
active index with the text it was embedded from, so a new index is that text
embedded again: read a batch from the active collection, embed it with the
new model, write it under the same id and metadata. Brain and vault entries
store the clean text and embed it with a one-line context header
(`vault/context.embed_text`); the header is rebuilt from the stored `path` and
`heading_path`, the same way the writers build it.

**One mechanism for everything: make index B equal to index A.** First build,
the catch-up of an index that sat dormant, and the last pass before switching
all compare ids and stored text, delete what A no longer has, and embed only
what is missing or changed. New documents keep going to the active index
while a build runs; the next pass picks them up.

**Activating an index is never a cold switch.** A full catch-up runs first,
then the pointer moves, then one more pass copies (never deletes) what was
written to the old index during the switch. A stale index made active would
look like an archive with documents missing — and Paperless sync re-ingests a
document missing from the index with the vision model.

**One job at a time**, in the app process. A restart leaves a build "paused";
the admin continues it (nothing restarts on its own — it can mean hours of CPU).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from services import embedding_registry as reg
from services import embeddings as emb
from services.chroma import ChromaClient
from services.vector_index import client_for, forget

log = logging.getLogger(__name__)

BATCH = 64
ID_PAGE = 5000
PARITY_SAMPLE = 32
COMPARE_CHUNKS = 60


@dataclass
class Job:
    model_id: str
    kind: str                      # "build" | "activate"
    task: asyncio.Task | None = None
    stop: bool = False
    started: float = field(default_factory=time.time)


_job: Job | None = None


def current_job() -> Job | None:
    return _job if _job and _job.task and not _job.task.done() else None


def startup() -> None:
    """A build that ran when the app stopped is paused, not restarted."""
    r = reg.load()
    changed = False
    for m in r["models"]:
        if m.get("index") == "building":
            m["index"] = "paused"
            m.setdefault("progress", {})["error"] = ""
            changed = True
    if changed:
        reg.save(r)


# ── What gets embedded ────────────────────────────────────────────────────────


def embed_text_for(role: str, document: str, meta: dict | None) -> str:
    """The text the writers embedded for this stored entry."""
    if role == "documents":
        return document or ""
    from config.settings import settings
    from vault.context import embed_text

    meta = meta or {}
    path = meta.get("path") or meta.get("rel_path") or ""
    if not path:
        return document or ""
    if role == "brain":
        return embed_text(document or "", path, strip_prefix=settings.brain_subfolder)
    return embed_text(document or "", path, meta.get("heading_path") or "")


async def _all_ids(c: ChromaClient) -> list[str]:
    out: list[str] = []
    offset = 0
    while True:
        raw = await asyncio.to_thread(c.collection.get, include=[], limit=ID_PAGE, offset=offset)
        ids = raw.get("ids") or []
        out.extend(ids)
        if len(ids) < ID_PAGE:
            return out
        offset += ID_PAGE


async def _wait_for_chat(embedder) -> None:
    """A local build waits while someone chats: the CPU embeds their questions."""
    if getattr(embedder, "kind", "") != "local":
        return
    from services.ollama_watchdog import chat_busy

    while chat_busy():
        await asyncio.sleep(2.0)


async def sync_role(src: ChromaClient, dst: ChromaClient, role: str, embedder, *,
                    deletes: bool = True, on_batch=None, should_stop=None) -> dict:
    """Make `dst` hold what `src` holds, embedded by `embedder`. Returns counts."""
    src_ids = await _all_ids(src)
    dst_ids = set(await _all_ids(dst))
    deleted = 0
    if deletes:
        gone = list(dst_ids - set(src_ids))
        for i in range(0, len(gone), 500):
            await dst.delete(ids=gone[i:i + 500])
        deleted = len(gone)
    embedded = 0
    for i in range(0, len(src_ids), BATCH):
        if should_stop and should_stop():
            break
        batch = src_ids[i:i + BATCH]
        items = await src.get(ids=batch)
        have = [x for x in batch if x in dst_ids]
        if have:
            mine = {x["id"]: x for x in await dst.get(ids=have)}
            items = [x for x in items if x["id"] not in mine
                     or mine[x["id"]]["document"] != x["document"]
                     or (mine[x["id"]]["metadata"] or {}) != (x["metadata"] or {})]
        if items:
            await _wait_for_chat(embedder)
            texts = [embed_text_for(role, x["document"], x["metadata"]) for x in items]
            vectors = await asyncio.to_thread(embedder.passages, texts)
            await dst.upsert_vectors(
                ids=[x["id"] for x in items], embeddings=vectors,
                documents=[x["document"] or "" for x in items],
                metadatas=[x["metadata"] or {} for x in items],
            )
            embedded += len(items)
        if on_batch:
            on_batch(role, min(i + BATCH, len(src_ids)), len(src_ids), embedded)
    return {"source": len(src_ids), "embedded": embedded, "deleted": deleted}


# ── Jobs ──────────────────────────────────────────────────────────────────────


def _progress(model_id: str, **fields) -> None:
    m = reg.get(model_id)
    if m is None:
        return
    p = dict(m.get("progress") or {})
    p.update(fields)
    reg.update(model_id, progress=p)


async def _ensure_endpoint(m: dict) -> None:
    if m.get("use") != "remote":
        return
    from services.wol import ensure_awake

    state = await ensure_awake(m["remote_url"], why="an embedding index")
    if state == "timeout":
        raise emb.EmbeddingError(f"{m['remote_url']} did not wake up")


async def _catch_up(src_model: dict, m: dict, job: Job, *, deletes: bool = True) -> dict:
    embedder = reg.embedder(m)
    await _ensure_endpoint(m)
    totals = {"source": 0, "embedded": 0, "deleted": 0}
    last_write = [0.0]

    def on_batch(role, done, total, embedded):
        now = time.monotonic()
        if now - last_write[0] > 2 or done == total:
            last_write[0] = now
            _progress(m["id"], role=role, done=done, total=total,
                      embedded=totals["embedded"] + embedded)

    from services.inference_activity import track

    for role in reg.ROLES:
        if job.stop:
            break
        src, dst = client_for(src_model, role), client_for(m, role)
        if m.get("use") == "remote":
            async with track(m["remote_url"], m["remote_model"], "background"):
                r = await sync_role(src, dst, role, embedder, deletes=deletes,
                                    on_batch=on_batch, should_stop=lambda: job.stop)
        else:
            r = await sync_role(src, dst, role, embedder, deletes=deletes,
                                on_batch=on_batch, should_stop=lambda: job.stop)
        for k in totals:
            totals[k] += r[k]
    return totals


async def _run(job: Job) -> None:
    global _job
    mid = job.model_id
    started = time.time()
    try:
        m = reg.get(mid)
        src = reg.active_model()
        if src["id"] == mid:
            raise ValueError("already active")
        reg.update(mid, index="building")
        _progress(mid, started=reg.now_iso(), error="", kind=job.kind, done=0, total=0,
                  embedded=0, role="")
        totals = await _catch_up(src, m, job)
        if job.stop:
            reg.update(mid, index="paused")
            _progress(mid, stopped=reg.now_iso())
            return
        reg.update(mid, index="ready", synced_at=reg.now_iso())
        _progress(mid, finished=reg.now_iso(), seconds=round(time.time() - started),
                  embedded=totals["embedded"], deleted=totals["deleted"])
        if job.kind == "activate":
            await _switch(src, reg.get(mid), job)
    except Exception as exc:  # noqa: BLE001
        log.exception("[embedding-index] %s %s failed", job.kind, mid)
        if reg.get(mid):
            m = reg.get(mid)
            reg.update(mid, index="paused" if m.get("index") == "building" else m.get("index"))
            _progress(mid, error=str(exc)[:300])
    finally:
        if _job is job:
            _job = None
        # The model built here is no longer pinned unless it became active.
        await asyncio.to_thread(emb.drop_unpinned)


async def _switch(old: dict, new: dict, job: Job) -> None:
    r = reg.load()
    r["active"] = new["id"]
    for x in r["models"]:
        if x["id"] in (old["id"], new["id"]):
            x["synced_at"] = reg.now_iso()
        if x["id"] == old["id"]:
            x["was_active"] = True  # "dormant" from now on, not just "ready"
    reg.save(r)
    log.info("[embedding-index] active index: %s (was %s)", new["id"], old["id"])
    # Anything written to the old index between the catch-up and the switch.
    await _catch_up(old, reg.get(new["id"]), job, deletes=False)
    reg.update(new["id"], index="ready")
    asyncio.get_running_loop().run_in_executor(None, _warm_active)


def _warm_active() -> None:
    """Load the active model; nothing else stays in RAM unless pinned."""
    emb.drop_unpinned()
    try:
        reg.embedder(reg.active_model()).warm()
    except Exception:  # noqa: BLE001
        log.warning("[embedding-index] active model could not be loaded", exc_info=True)


def start(model_id: str, kind: str = "build") -> Job:
    """Build/catch up (`build`) or catch up and make active (`activate`)."""
    global _job
    if current_job():
        raise RuntimeError("busy")
    m = reg.get(model_id)
    if m is None:
        raise KeyError(model_id)
    if model_id == reg.active_id():
        raise ValueError("already active")
    if kind == "activate" and m.get("index") not in ("ready", "paused"):
        raise ValueError("no index")
    job = Job(model_id, kind)
    _job = job
    job.task = asyncio.get_running_loop().create_task(_run(job))
    return job


def stop() -> None:
    if current_job():
        _job.stop = True


async def delete_index(model_id: str, keep_files: bool = False) -> int:
    """Drop a model's collections, and its downloaded files on this server
    (not with `keep_files`: a rebuild here needs them). Never the active one's.
    Returns the bytes of model files freed."""
    if current_job() and current_job().model_id == model_id:
        raise RuntimeError("busy")
    if model_id == reg.active_id():
        raise ValueError("the active index cannot be deleted")
    m = reg.get(model_id)
    for role in reg.ROLES:
        c = client_for(m, role)
        try:
            await asyncio.to_thread(c.client.delete_collection, c.collection.name)
        except Exception:  # noqa: BLE001 — already gone
            log.debug("delete %s", role, exc_info=True)
    forget(model_id)
    reg.update(model_id, index="none", progress={}, synced_at="")
    if keep_files:
        return 0
    return await delete_model_files(model_id)


async def rebuild_here(model_id: str) -> Job:
    """The remote copy gave other vectors: drop the index, embed on this server,
    build again."""
    await delete_index(model_id, keep_files=True)
    reg.update(model_id, use="local", parity={})
    return start(model_id, "build")


async def delete_model_files(model_id: str) -> int:
    """The model's files in this server's cache — also there when it embedded
    remotely (the parity check loads it here). Never the active model's."""
    m = reg.get(model_id)
    if m is None or model_id == reg.active_id():
        return 0
    try:
        return await asyncio.to_thread(emb.delete_local_files, m["name"])
    except Exception:  # noqa: BLE001 — the index is gone either way
        log.warning("[embedding-index] could not delete the files of %s", m["name"], exc_info=True)
        return 0


async def remove_model(model_id: str) -> int:
    """Take a model off the list (no index left) and delete its files here."""
    m = reg.get(model_id)
    if m is None or model_id == reg.active_id() or m.get("index") != "none":
        reg.remove_model(model_id)  # raises with the reason
    freed = await delete_model_files(model_id)
    reg.remove_model(model_id)
    return freed


async def counts(m: dict) -> dict[str, int]:
    """What the index holds: chunks, and the documents / notes they come from.
    A document is split into ~200-word chunks, so chunks are several times the
    documents — the card shows both, or 2 600 reads like 2 600 documents."""
    out = {"doc_chunks": 0, "docs": 0, "note_chunks": 0, "notes": 0, "facts": 0}
    try:
        c = client_for(m, "documents")
        raw = await asyncio.to_thread(c.collection.get, include=["metadatas"])
        metas = raw.get("metadatas") or []
        out["doc_chunks"] = len(metas)
        out["docs"] = len({(x or {}).get("paperless_id") for x in metas} - {None})
    except Exception:  # noqa: BLE001
        log.debug("counting documents", exc_info=True)
    try:
        c = client_for(m, "vault")
        raw = await asyncio.to_thread(c.collection.get, include=["metadatas"])
        metas = raw.get("metadatas") or []
        out["note_chunks"] = len(metas)
        out["notes"] = len({(x or {}).get("pbrain_id") or (x or {}).get("path") for x in metas} - {None, ""})
    except Exception:  # noqa: BLE001
        log.debug("counting notes", exc_info=True)
    try:
        out["facts"] = await client_for(m, "brain").count()
    except Exception:  # noqa: BLE001
        pass
    return out


# ── Parity: are local and remote the same model? ──────────────────────────────


async def parity(model_id: str) -> dict:
    """Embed the same stored chunks locally and remotely; cosine per pair."""
    m = reg.get(model_id)
    if not reg.has_remote(m):
        raise ValueError("no remote endpoint")
    src = client_for(reg.active_model(), "documents")
    items = await src.peek(limit=PARITY_SAMPLE)
    texts = [x["document"] for x in items if x.get("document")]
    if not texts:
        raise ValueError("nothing to compare")
    texts += ["Wann läuft meine Versicherung ab?", "invoice number of the laptop"]
    local, remote = reg.embedder(m, "local"), reg.embedder(m, "remote")
    await _ensure_endpoint({**m, "use": "remote"})
    a = await asyncio.to_thread(local.passages, texts)
    b = await asyncio.to_thread(remote.passages, texts)
    if len(a[0]) != len(b[0]):
        sims = [0.0]
    else:
        sims = [emb.cosine(x, y) for x, y in zip(a, b)]
    result = {"at": reg.now_iso(), "endpoint": f"{m['remote_url']}|{m['remote_model']}",
              "n": len(sims), "mean": round(sum(sims) / len(sims), 4),
              "min": round(min(sims), 4), "dims": [len(a[0]), len(b[0])]}
    reg.update(model_id, parity=result)
    # The local copy was loaded only for the check; it stays unless pinned.
    await asyncio.to_thread(emb.drop_unpinned)
    return result


# ── Comparison: same questions, two indexes, one after the other ──────────────


async def compare(model_ids: list[str], questions: list[str], n_docs: int = 10) -> dict:
    """Semantic search only — no LLM. Per model: load time, and per question the
    query-embedding time, the vector-search time and the documents (best chunk
    distance per document). Runs model after model, each loaded alone; the
    active model is loaded again at the end."""
    from config.settings import settings

    runs: dict[str, dict] = {}
    try:
        for mid in model_ids:
            m = reg.get(mid)
            if m is None or m.get("index") == "none":
                continue
            if m.get("use") == "remote":
                embedder = reg.embedder(m)
                await _ensure_endpoint(m)
            else:
                embedder = emb.LocalEmbedder(m["name"], m["query_prompt"], exclusive=True)
            coll = ChromaClient(reg.collection_name(m, "documents"),
                                str(settings.app_path / settings.chroma_path), embedder)
            load_s = await asyncio.to_thread(embedder.warm)
            out = []
            for q in questions:
                t0 = time.perf_counter()
                vec = (await asyncio.to_thread(embedder.queries, [q]))[0]
                t1 = time.perf_counter()
                hits = await coll.query_by_embedding(vec, n_results=COMPARE_CHUNKS)
                t2 = time.perf_counter()
                out.append({"q": q, "embed_ms": round((t1 - t0) * 1000, 1),
                            "search_ms": round((t2 - t1) * 1000, 1),
                            "docs": rank_documents(hits, n_docs)})
            runs[mid] = {"load_s": round(load_s, 1), "use": m.get("use", "local"),
                         "chunks": await coll.count(), "questions": out}
    finally:
        await asyncio.to_thread(_warm_active)
    return runs


def rank_documents(hits: list[dict], n_docs: int) -> list[dict]:
    """Chunk hits → documents by their best chunk, as the search tool ranks them."""
    best: dict[int, dict] = {}
    for h in hits:
        meta = h.get("metadata") or {}
        pid = meta.get("paperless_id")
        if pid is None or h.get("distance") is None:
            continue
        if pid not in best or h["distance"] < best[pid]["distance"]:
            best[pid] = {"id": int(pid), "distance": round(float(h["distance"]), 4),
                         "page": int(meta.get("page_number") or 0),
                         "snippet": (h.get("document") or "")[:240]}
    return sorted(best.values(), key=lambda d: d["distance"])[:n_docs]


def overlap(a: list[dict], b: list[dict]) -> float:
    """Share of documents both lists contain (0–1)."""
    sa, sb = {d["id"] for d in a}, {d["id"] for d in b}
    return len(sa & sb) / max(1, len(sa | sb))
