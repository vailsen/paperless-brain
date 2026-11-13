# Embedding models and indexes

The admin area (`/admin`, users in `ADMIN_USERS`) manages which model turns
text into vectors for search — documents, notes and the agent's memory alike.

## Changing the model

1. **Add the model.** Its Hugging Face id (e.g. `Qwen/Qwen3-Embedding-0.6B`),
   run by sentence-transformers on the server. Optionally the same model
   behind an OpenAI-compatible endpoint (Ollama: `http://gpu:11434`, model
   `qwen3-embedding:0.6b`; TEI; vLLM).
2. **Build its index.** Every stored chunk is embedded again: no vision model,
   no download from Paperless, no re-chunking. Search keeps using the active
   index meanwhile; new documents go there and are caught up later.
   - On a large archive, build remotely on a GPU: switch the model to
     "Embed remotely" before building. Afterwards run "Check local against
     remote"; when both give the same vectors (mean cosine ≥ 0.99), switch to
     "Embed on this server" — queries are then embedded on the CPU against the
     index built on the GPU.
   - A local build keeps the new model and the active one in RAM.
3. **Compare.** Questions, one per line. Each index is searched in turn, its
   model alone in RAM: the documents it finds (best chunk per document),
   distances, load time, query-embedding and vector-search time. The overlap
   says how much changes for users.
4. **Set thresholds** for the new model from its distances (the archive hint
   on web search, "similar fact exists", the memory hint). Empty = the values
   tuned for e5.
5. **Make it active.** It is caught up with the active index first, then every
   search reads it. The old index stays dormant (not updated) — delete it, or
   keep it to switch back; switching back catches it up first.

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
