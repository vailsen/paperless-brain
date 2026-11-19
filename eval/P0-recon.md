# P0 — Code reconnaissance for hybrid search

Plan: `docs/hybrid-search-eval-plan.md`. Read-only pass over the code and the
running instance (app 0.20.0, Paperless-ngx 3.5.1, 2026-11-19). No code or data
changed.

## 1. Search entry point

The plan's `search_documents` is called **`search`** here, with a sibling
**`search_exact`**.

- Schema: `services/chat_service.py` `TOOL_DEFINITIONS`, entry `"search"`.
  Parameters: `semantic_query` (required, described as "phrase it the way one
  would describe a document, not like a search keyword"), `created_after`,
  `created_before`. Description: "semantic embedding search … even when
  official terms differ from everyday wording".
- Implementation: `_tool_search` (`services/chat_service.py:1082`) →
  `pipelines.search.search(filters, semantic_query, paperless_client=_user_paperless())`,
  run concurrently with brain and vault hints (`_brain_hints`, `_vault_hints`).
- Return shape: a text block for the model (`#id — title`, type | correspondent |
  date | `Score <distance>`, the sidecar summary up to 250 chars, picture line,
  best matched chunk up to 150 chars with its cite reference) plus a
  `list[DocumentResult]` for the UI (`document`, `relevance_score` = best chunk
  distance, `matched_chunks`, `matched_pages`).
- Default k: `search(n_results=20)` → asks Chroma for `20 × 5 = 100` chunks,
  collapses to documents by best distance, keeps 20; the tool prints at most
  `werkbank.settings_store.get_search_max_results()` (admin setting, 20 on the
  instance).
- `search_exact` (`_tool_search_exact`, `:1437`): hard criteria (tags,
  correspondent, type, dates) resolved to Paperless ids, plus `text` — a literal
  identifier. `text` becomes a Chroma `where_document` (`_build_where_document`,
  `:1063`): a separator-bearing identifier (`_identifier_atoms`) is matched as
  phrase variants, anything else as an **OR of `$contains` per word**; results
  are then ordered by embedding distance to `text`. Cross-references
  (`cross_ref_index.find_by_value`) are folded in.

## 2. Dense query path

- `ChromaClient.query` (`services/chroma.py:187`) embeds via
  `embedder.queries()` → `query_prompt + text`; passages via
  `embedder.passages()` with no prefix (`services/embeddings.py:187-191`,
  `encode(prompt="")` so the model's own default prompt is never added).
- Active model on the instance: `intfloat/multilingual-e5-large-instruct`,
  local, `query_prompt` = `Instruct: Given a search query, retrieve relevant
  passages that answer the query\nQuery: ` (`E5_INSTRUCT_QUERY`). **Correct: on
  queries only.**
- Not an inconsistency, but relevant to §13 of the plan: every stored chunk
  already starts with a context prefix `Dokument: <type>, <correspondent>,
  <DD.MM.YYYY> — ` (`services/chunking.build_context_prefix`). A partial
  contextual chunk header exists; the document title is not part of it.
- Collection config: HNSW, `ef_construction 100`, `max_neighbors 16`
  (`services/chroma.py:57`); search ef is Chroma's default.
- **Finding (not fixed):** Chroma 1.5.9's HNSW search is **not deterministic
  across processes** at the tail of a 100-chunk request: the same query vector
  in two processes returned a different chunk at rank ~20 (embedding verified
  bit-identical, same collection). On the eval set 3–4 of 60 production top-20
  lists differed between two process runs, always at ranks ≥ 14. A file copy of
  the live Chroma directory differed further (stale HNSW segment). With 2 617
  chunks an exact numpy search costs well under 10 ms; `search` could use it.

## 3. Chunking

- `services/chunking.chunk_document`: paragraphs → sentences when oversized,
  target 200 words, max 250, overlap 30 words. Median chunk on the instance:
  255 words including the prefix.
- Input: per-page `page_text` from the vision extraction (plus `[Image n: …]`
  lines for picture descriptions, ingest only).
- Chunk id: `paperless_{doc_id}_chunk_{chunk_index}` (`pipelines/ingest.py:233`).
- Metadata per chunk: `paperless_id`, `chunk_index`, `page_number`. Title,
  correspondent and type are **not** metadata; type/correspondent/date are only
  in the text prefix.

## 4. Ingest / update / delete

- Sync: `pipelines/paperless_db_sync.py` compares Paperless ids with Chroma →
  `ingest_document` (new, re-ingest) / `delete_document` (gone).
- Write: `pipelines/ingest.py:233` `chroma.upsert(ids, documents, metadatas)`;
  re-ingest deletes the sidecar first and upserts the chunks again.
- Delete: `pipelines/delete.py:18` `chroma.delete(where={"paperless_id": …})`.
- Embedding-model switch: `services/embedding_index.sync_role` copies stored
  text between per-model collections.
- A lexical index would hook into exactly `ingest.py:233`, `delete.py:18` and
  `sync_role` (or be rebuilt from Chroma's stored documents, which are the
  chunks verbatim — the eval builds its FTS5 tables that way and gets an equal
  chunk-id set).

## 5. Permission filtering

- The document Chroma collection is **shared by all users**; no `user`
  metadata on chunks.
- `search` without filters: Chroma ranks all users' chunks, the top 20 doc ids
  are fetched with **the user's own Paperless token**
  (`pl.list_documents(ids=…)`, `pipelines/search.py:131`); what the token cannot
  read is silently absent. Correct (no leak), but a **post-filter after the
  cut**: another user's documents consume result slots, so a user gets fewer
  than 20 (seen on the eval set: a hidden document took rank 19 of an
  identifier query).
- With filters: candidate ids come from Paperless with the user token and go
  into Chroma as `where paperless_id $in` — a pre-filter.
- The token is per turn (`_current_token` → `_user_paperless`, fails closed);
  an expert scope wraps the same client (`expert_scope.scoped_client`).
- A lexical path must use the same visibility set; the harness filters each
  ranker's list by the user's visible ids **before** fusion, which is stricter
  than dense today.

## 6. Analytic mode

`search_exact`. Its "full text" is **not** Paperless' full-text index (the code
says so: `"query": None  # never use Paperless full-text (OCR inferior to
chunks)`), but a Chroma `where_document` `$contains` filter on the chunk text,
OR across words, case-sensitive, **unranked** — the order then comes from the
embedding distance of the same text. Weakness seen on the eval set: an
identifier like `VR 1000631423` is not caught by `_identifier_atoms` (10-digit
group), falls to the word OR, and `$contains "VR"` matches every chunk with
"VR" anywhere; the right document landed at rank 12.

## 7. Paperless full-text search (H4)

Probed on the instance (`/api/documents/?query=`), Paperless-ngx 3.5.1:

- Ranked: results carry `__search_hit__.score` and `rank`; scores behave like
  BM25 with field boosts (title matches score highest). The exact scoring
  function is not visible through the API.
- **Default operator AND**: `Haftpflicht Vodafone` → 2 hits, `Haftpflicht OR
  Vodafone` → 45. A descriptive query under AND mostly returns nothing.
- German stemming: `Rechnungen` = `Rechnung`, `häuser` = `haus`; umlaut folding
  `Kundigung` = `Kündigung`, but no transliteration (`Kuendigung` → 0).
- **No compound splitting**: `Haftpflicht` 15 hits, `Haftpflichtversicherung`
  40, disjoint top hits.
- Identifiers with `/` are tokenised into parts (`00206538856/25/03` → 307
  hits, correct one still first).
- Document-level only; also returns documents that are not in Chroma
  (no-ingest tag, not yet synced) — an adapter must drop them.
- Latency from the app host: p50 131 ms, p95 194 ms (OR mode).

## 8. Agent model and tool calling

- The user's chat model: `qwen3.8:27b-mtp-q4_K_M` on Ollama (local lane,
  OpenAI-compatible `/v1/chat/completions`, `OpenAICompatibleChatBackend`,
  `services/chat_service.py:3652`). Tools are `TOOL_DEFINITIONS` filtered by the
  active groups (`app_ui/pages/chat.py` `_active_tools`), converted with
  `_to_ollama_tools`. System prompt: `config/chat_prompts.build_system_prompt`.
- Observed on the 60 eval queries (single turn, production prompt, default
  groups): **all 10 identifier queries (and one entity query) go to `search_exact`**, and for
  `search` the model rewrites the question into a keyword-rich
  `semantic_query` that already contains the synonyms a thesaurus would add
  (e.g. a colloquial term → the official one). Mean 1.5 s per call.

## 9. Corpus

- Paperless: 550 documents, 3 users; the eval user sees 528.
- Chroma: 528 documents, 2 617 chunks. Chunks per document: median 2, p90 10,
  max 127; 53 documents have ≥ 10 chunks. 153 one-chunk, 159 two-chunk.
- Language: German (archive language setting; UI language of the eval user is
  English).
- Top document types: Rechnung 179, Steuerbescheinigung 56, Information 52,
  Steuerbescheid 32, then Untersuchungsbericht, Steuererklärung, Kontoauszug,
  Belege (16 each).
