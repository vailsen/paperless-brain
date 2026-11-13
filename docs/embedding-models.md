# Embedding models and indexes

The admin area (`/admin`, users in `ADMIN_USERS`) manages which model turns
text into vectors for search — documents, notes and the agent's memory alike.

## Changing the model

The admin page shows the next step on each model's card; this is the path it
takes.

1. **Add the model.** Its Hugging Face id (e.g. `Qwen/Qwen3-Embedding-0.6B`),
   run by sentence-transformers on the server. Optionally the same model
   behind an OpenAI-compatible endpoint (Ollama: `http://gpu:11434`, model
   `qwen3-embedding:0.6b`; TEI; vLLM).
2. **Build its index.** Every stored chunk is embedded again: no vision model,
   no download from Paperless, no re-chunking. Search keeps using the active
   index meanwhile; new documents go there and are caught up later.
   - On a large archive, build remotely on a GPU: set "Embeds on" to
     "Remote endpoint" before building. Afterwards set it back to "This
     server" whenever you like — the check that both give the same vectors
     (mean cosine ≥ 0.99) runs on its own first, and the switch happens only
     if it passes. Staying remote is fine too: then every search needs the
     endpoint to be up.
   - A local build keeps the new model and the active one in RAM.
3. **Compare.** Questions, one per line. Each index is searched in turn, its
   model alone in RAM: the documents it finds (best chunk per document),
   distances, load time, query-embedding and vector-search time. The overlap
   says how much changes for users.
4. **Set thresholds** for the new model from its distances — all four are
   distances (0 = identical): memory and note hints (max. distance and a
   window factor over the best hint), the archive hint on web search, "a very
   similar fact exists". Until set, the values every install ran with apply
   (the old memory-hint similarity 0.82 = distance 0.18).
5. **Make it active.** It is caught up with the active index first, then every
   search reads it. The old index stays dormant (not updated) — delete it, or
   keep it to switch back; switching back catches it up first. Deleting an
   index also deletes the model's downloaded files on this server (about
   1–2 GB); a remote endpoint is not touched.

## The query instruction

Both models are *asymmetric*: a search query gets an instruction in front,
stored text gets nothing. The field "Query instruction" holds it, filled in
for known models:

- e5-instruct: `Instruct: Given a search query, retrieve relevant passages that answer the query\nQuery: `
- Qwen3-Embedding: `Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:`
  (the model's own default; Qwen recommends an English task description
  fitted to the use, which may help by a few percent — run the comparison
  before and after changing it).

Changing it on a model with an index is safe: only queries carry it, so the
stored vectors stay valid.

## What it costs

- Vectors are 1024 numbers for both e5-large-instruct and Qwen3-Embedding-0.6B,
  so the vector search costs the same. Embedding a query is tens of
  milliseconds on a CPU for both; Qwen needs roughly 1.5× the compute per
  token, which matters for building an index more than for searching.
- RAM: about 2.5 GB per local model (fp32).
- Disk: each index is a copy of the chunk texts plus its vectors.

## Not in scope

The chunking stays as it is: a comparison should measure the model, not two
changes at once. A different chunking would be a rebuild from the sidecars.
