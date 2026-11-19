# Retrieval eval harness

Offline, retrieval-only evaluation of `search` against lexical (BM25) and fused
(RRF) rankers. Plan: `docs/hybrid-search-eval-plan.md`; findings:
`eval/P0-recon.md`, `docs/hybrid-search-eval-results.md`.

Keep it as the regression suite: rerun after any change to embeddings, chunking
or retrieval.

## Privacy

Everything with real content — the query set, the rankings, the results, the
cached keywords — lives in `eval/local/`, which is git-ignored. Only the code,
this README and `example_queries.jsonl` (made up) are committed.

## Layout

| file | what |
|---|---|
| `harness/common.py` | paths, fixed parameters, query → FTS5 translation |
| `harness/rankers.py` | dense (HNSW + exact), FTS5 `unicode61` / `trigram`, Paperless full-text, `search_exact`, tags; the production reference |
| `harness/keywords.py` | the agent's `keywords` (and its `semantic_query` rewrite), local model only, cached |
| `harness/run.py` | configurations, RRF, metrics, report |

## Running

The steps up to `rank` need the app environment: the embedding model, the
Chroma data, Paperless and a user's background-access grant. Run them inside
the app container with this directory on `PYTHONPATH`:

```bash
# copy eval/ to /tmp/pbeval in the container, then:
docker exec -w /app -e PYTHONPATH=/app:/tmp/pbeval \
  -e EVAL_LOCAL=/tmp/pbeval/local \
  -e EVAL_QUERIES=/tmp/pbeval/local/queries.v1.jsonl \
  -e EVAL_CHROMA=/app/data/chroma_db \
  <app container> python -m eval.harness <step>
```

| step | does |
|---|---|
| `prepare --users A,B` | visible ids per user; FTS5 tables built from the Chroma documents (needs `EVAL_CHROMA` pointing at a copy or the live dir — it only reads) |
| `keywords` | one single-turn call per query to the user's local chat model with the production prompt + an eval-only `keywords` parameter; cached in `keywords.v1.jsonl` |
| `rank` | every base ranker per query → `rankings.jsonl`; also the production `search` result in the same process → `prod_c0.jsonl` |
| `run [--config C0]` | the matrix → `results/<timestamp>/report.md`; with `--config C0` it first fails unless C0 reproduces production for every query |
| `check` | C0 vs production, query by query |
| `pool` | union of the rankers' top n per query, for judging relevance |

`run`, `check` and `pool` only read `eval/local` and can run anywhere.

## Two things that bite

- **Read the live Chroma directory for ranking, query only.** A file copy of a
  running Chroma carries a stale HNSW segment and ranks differently.
- **HNSW is not deterministic across processes** at the tail of a 100-chunk
  request. The production reference is therefore taken in the same process as
  the rankers, and every configuration except C0 uses an exact (brute-force)
  dense ranking, so reruns produce identical numbers. The report states that
  C0 and the exact baseline C0x agree per query.

## Query file format

See `example_queries.jsonl`. Ground truth is document-level (Paperless ids) and
per user. Never edit a frozen set; a correction is a new version.
