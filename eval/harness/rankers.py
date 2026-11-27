"""The base rankers. Each produces, per query, a ranked list of Paperless doc ids
(chunk lists are collapsed by first occurrence) plus its latency.

Needs the app environment (run inside the container, cwd /app): the embedding
model, the Chroma copy, Paperless with the user's own token.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time

import httpx

from eval.harness.common import (
    CANDIDATES,
    CHROMA_COPY,
    DENSE_CHUNKS,
    LEX_DB,
    LOCAL,
    PROD_C0,
    RANKINGS,
    collapse,
    fts_or_query,
    load_jsonl,
    load_queries,
    sanitize_keywords,
    words,
    write_jsonl,
    KEYWORDS,
)

TOKENIZERS = {
    "u": "unicode61 remove_diacritics 2",
    "t": "trigram",
}


def _user_token(user: str) -> str:
    from services import background_access

    tok = background_access.token_for(user)
    if not tok:
        raise SystemExit(f"No background-access grant for {user!r}; cannot act as that user.")
    return tok


def _pl_base() -> str:
    from config.settings import settings

    return settings.paperless_url.rstrip("/")


def _collection():
    """The documents collection. For ranking this is the live one, queried only
    (EVAL_CHROMA=/app/data/chroma_db): a file copy of a running Chroma carries a
    stale HNSW segment and ranks differently from production."""
    import chromadb

    from services import embedding_registry as reg

    m = reg.active_model()
    name = reg.collection_name(m, "documents")
    client = chromadb.PersistentClient(path=str(CHROMA_COPY))
    return client.get_collection(name, embedding_function=None), reg.embedder(m), m


# ── prepare ───────────────────────────────────────────────────────────────────


def prepare(users: list[str]) -> None:
    """Visibility per user + the two FTS5 indexes, built from the Chroma copy."""
    LOCAL.mkdir(parents=True, exist_ok=True)
    for user in users:
        h = {"Authorization": f"Token {_user_token(user)}"}
        ids, url = [], _pl_base() + "/api/documents/?page_size=100&fields=id"
        while url:
            r = httpx.get(url, headers=h, timeout=60).json()
            ids += [d["id"] for d in r["results"]]
            url = r["next"]
        (LOCAL / f"visible_{user}.json").write_text(json.dumps(sorted(ids)))
        print(f"{user}: {len(ids)} visible documents")

    col, _, m = _collection()
    g = col.get(include=["documents", "metadatas"])
    if LEX_DB.exists():
        LEX_DB.unlink()
    db = sqlite3.connect(LEX_DB)
    for key, tok in TOKENIZERS.items():
        db.execute(
            f"CREATE VIRTUAL TABLE fts_{key} USING fts5(chunk_id UNINDEXED, doc UNINDEXED, text, tokenize='{tok}')"
        )
        db.executemany(
            f"INSERT INTO fts_{key}(chunk_id, doc, text) VALUES (?,?,?)",
            [(i, md["paperless_id"], d) for i, md, d in zip(g["ids"], g["metadatas"], g["documents"])],
        )
        db.execute(f"CREATE VIRTUAL TABLE vocab_{key} USING fts5vocab(fts_{key}, 'row')")
    db.commit()
    fts_ids = {r[0] for r in db.execute("SELECT chunk_id FROM fts_u")}
    print(
        f"model {m['id']}: {len(g['ids'])} chunks in Chroma copy, {len(fts_ids)} in FTS, "
        f"chunk-id sets equal: {fts_ids == set(g['ids'])}"
    )


# ── lexical ───────────────────────────────────────────────────────────────────


def _fts(db: sqlite3.Connection, key: str, match: str) -> tuple[list[int], float]:
    if not match:
        return [], 0.0
    t0 = time.perf_counter()
    try:
        rows = db.execute(
            f"SELECT doc FROM fts_{key} WHERE fts_{key} MATCH ? ORDER BY bm25(fts_{key}) LIMIT ?",
            (match, CANDIDATES),
        ).fetchall()
    except sqlite3.OperationalError as e:
        print(f"  fts_{key} error on {match!r}: {e}")
        rows = []
    return collapse([r[0] for r in rows]), (time.perf_counter() - t0) * 1000


_PL_SYNTAX = str.maketrans({c: " " for c in ':*?~^(){}[]"!'})


def _paperless(h: dict, q: str, mode: str) -> tuple[list[int], float]:
    if mode == "or":
        terms = [w.translate(_PL_SYNTAX).strip() for w in words(q)]
        query = " OR ".join(t for t in terms if t)
    else:
        query = q.translate(_PL_SYNTAX)
    if not query.strip():
        return [], 0.0
    t0 = time.perf_counter()
    r = httpx.get(
        _pl_base() + "/api/documents/",
        params={"query": query, "page_size": CANDIDATES, "fields": "id"},
        headers=h,
        timeout=60,
    )
    ms = (time.perf_counter() - t0) * 1000
    if r.status_code != 200:
        print(f"  paperless {mode} {r.status_code} on {query!r}")
        return [], ms
    return [d["id"] for d in r.json()["results"]], ms


def rank() -> None:
    """Every base ranker for every query → rankings.jsonl (deterministic inputs for `run`)."""
    from pipelines.search import _tag_search
    from services.paperless import PaperlessClient

    queries = load_queries()
    kw = {r["id"]: r for r in load_jsonl(KEYWORDS)}
    col, embedder, _ = _collection()
    db = sqlite3.connect(LEX_DB)
    indexed = {r[0] for r in db.execute("SELECT DISTINCT doc FROM fts_u")}
    tokens: dict[str, str] = {}
    visible = {u: json.loads((LOCAL / f"visible_{u}.json").read_text()) for u in {q["user"] for q in queries}}
    embedder.queries(["warm-up"])  # first call loads the model; not a query latency

    async def _tags(pl, q):
        return list((await _tag_search(pl, q, None)).keys())

    # Exact (brute-force) dense ranking as a control: Chroma's HNSW is approximate
    # and, across processes, not deterministic at the tail of a 100-chunk request.
    import numpy as np

    allv = col.get(include=["embeddings", "metadatas"])
    mat = np.asarray(allv["embeddings"], dtype=np.float32)
    mat /= np.linalg.norm(mat, axis=1, keepdims=True)
    chunk_doc = [md["paperless_id"] for md in allv["metadatas"]]
    chunk_doc_arr = np.asarray(chunk_doc)
    from pipelines.search import search as prod_search
    from werkbank.settings_store import get_search_max_results

    prod_rows = []
    filters = {"query": None, "correspondent": None, "document_type": None,
               "tags": None, "created": None, "added": None}

    out = []
    for q in queries:
        user = q["user"]
        tok = tokens.setdefault(user, _user_token(user))
        h = {"Authorization": f"Token {tok}"}
        row = {"id": q["id"], "lat": {}}

        t0 = time.perf_counter()
        vec = embedder.queries([q["query"]])
        t1 = time.perf_counter()
        # Ranked over the user's visible documents, as production does.
        vis_where = {"paperless_id": {"$in": sorted(visible[user])}}
        raw = col.query(query_embeddings=vec, n_results=DENSE_CHUNKS, where=vis_where,
                        include=["metadatas", "distances"])
        t2 = time.perf_counter()
        metas, dists = raw["metadatas"][0], raw["distances"][0]
        # Production order: best (smallest) distance per doc, stable on ties.
        best: dict[int, float] = {}
        for md, d in zip(metas, dists):
            pid = md["paperless_id"]
            if pid not in best or d < best[pid]:
                best[pid] = d
        row["dense"] = sorted(best, key=lambda p: best[p])
        row["dense_dist"] = [round(best[p], 5) for p in row["dense"]]
        vmask = np.isin(chunk_doc_arr, visible[user])
        sims = np.where(vmask, mat @ np.asarray(vec[0], dtype=np.float32), -np.inf)
        order = np.argsort(-sims, kind="stable")[:DENSE_CHUNKS]
        row["dense_exact"] = collapse([chunk_doc[i] for i in order])
        row["lat"]["embed"] = (t1 - t0) * 1000
        row["lat"]["dense"] = (t2 - t1) * 1000

        terms = words(q["query"])
        row["lex_u_q"] = fts_or_query(terms, 2)
        row["lex_t_q"] = fts_or_query(terms, 3)
        row["lex_u"], row["lat"]["lex_u"] = _fts(db, "u", row["lex_u_q"])
        row["lex_t"], row["lat"]["lex_t"] = _fts(db, "t", row["lex_t_q"])

        for mode in ("or", "and"):
            ids, ms = _paperless(h, q["query"], mode)
            row[f"pl_{mode}"] = [i for i in ids if i in indexed]
            row["lat"][f"pl_{mode}"] = ms

        k = kw.get(q["id"])
        if k and k.get("first_tool") == "search_exact":
            # What the agent really did: the production search_exact with its own arguments.
            row["route"] = "search_exact"
            row["x0"], row["lat"]["x0"] = _search_exact(tok, user, k.get("first_args") or {})
        elif k:
            row["route"] = k.get("first_tool")
        # The query `search` really receives in production is the agent's own
        # rewrite (semantic_query), not the user's words. Rank that too ("_a").
        aq = (k or {}).get("semantic_query")
        if aq:
            row["agent_query"] = aq
            av = embedder.queries([aq])
            araw = col.query(query_embeddings=av, n_results=DENSE_CHUNKS, where=vis_where,
                             include=["metadatas", "distances"])
            abest: dict[int, float] = {}
            for md, d in zip(araw["metadatas"][0], araw["distances"][0]):
                if md["paperless_id"] not in abest or d < abest[md["paperless_id"]]:
                    abest[md["paperless_id"]] = d
            row["dense_a"] = sorted(abest, key=lambda p: abest[p])
            asims = np.where(vmask, mat @ np.asarray(av[0], dtype=np.float32), -np.inf)
            row["dense_a_exact"] = collapse([chunk_doc[i] for i in np.argsort(-asims, kind="stable")[:DENSE_CHUNKS]])
            at = words(aq)
            row["lex_u_a"], _ = _fts(db, "u", fts_or_query(at, 2))
            row["lex_t_a"], _ = _fts(db, "t", fts_or_query(at, 3))
            row["tags_a"] = asyncio.run(_tags(PaperlessClient(_pl_base(), tok), aq))
        if k:
            kws = sanitize_keywords(k.get("keywords"))
            row["kw_terms"] = kws
            row["kw_u"], row["lat"]["kw_u"] = _fts(db, "u", fts_or_query(kws, 2))
            row["kw_t"], row["lat"]["kw_t"] = _fts(db, "t", fts_or_query(kws, 3))

        pl = PaperlessClient(_pl_base(), tok)
        row["tags"] = asyncio.run(_tags(pl, q["query"]))
        # The production `search` path in this same process (same HNSW state) — C0's reference.
        res = asyncio.run(prod_search(filters=dict(filters), semantic_query=q["query"], paperless_client=pl))
        prod_rows.append({"id": q["id"], "docs": [r.document.id for r in res[: get_search_max_results()]]})
        out.append(row)
        print(f"{q['id']}: dense {len(row['dense'])} lex_u {len(row['lex_u'])} lex_t {len(row['lex_t'])} "
              f"pl_or {len(row['pl_or'])} pl_and {len(row['pl_and'])}")
    write_jsonl(RANKINGS, out)
    write_jsonl(PROD_C0, prod_rows)


def _search_exact(tok: str, user: str, args: dict) -> tuple[list[int], float]:
    from services import chat_service as cs

    async def go():
        t1 = cs._current_token.set(tok)
        t2 = cs._current_owner.set(user)
        try:
            return await cs._tool_search_exact(dict(args))
        finally:
            cs._current_token.reset(t1)
            cs._current_owner.reset(t2)

    t0 = time.perf_counter()
    _, results = asyncio.run(go())
    return [r.document.id for r in results], (time.perf_counter() - t0) * 1000


# ── production reference ──────────────────────────────────────────────────────


def prod() -> None:
    """The real `search` tool path (pipelines.search.search on the live collection,
    the user's own Paperless token) — what C0 must reproduce."""
    from pipelines.search import search
    from services.paperless import PaperlessClient
    from werkbank.settings_store import get_search_max_results

    filters = {"query": None, "correspondent": None, "document_type": None,
               "tags": None, "created": None, "added": None}
    out = []
    for q in load_queries():
        pl = PaperlessClient(_pl_base(), _user_token(q["user"]))
        res = asyncio.run(search(filters=dict(filters), semantic_query=q["query"], paperless_client=pl))
        out.append({"id": q["id"], "docs": [r.document.id for r in res[: get_search_max_results()]]})
    write_jsonl(PROD_C0, out)
    print(f"{len(out)} production result lists → {PROD_C0}")


def vocab_check(terms: list[str]) -> dict[str, bool]:
    """Does a keyword occur anywhere in the corpus? (fts5vocab, unicode61 tokens)."""
    db = sqlite3.connect(LEX_DB)
    out = {}
    for t in terms:
        match = fts_or_query([t], 2)
        n = db.execute("SELECT count(*) FROM fts_u WHERE fts_u MATCH ?", (match,)).fetchone()[0] if match else 0
        out[t] = n > 0
    return out
