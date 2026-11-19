"""Offline retrieval eval for hybrid search (docs/hybrid-search-eval-plan.md, P2).

Retrieval only — no answer generation, no LLM judge. Runs inside the app
container (same embedding model, same Chroma data, Paperless reachable), reads
production data read-only: Chroma is opened from a copy, the lexical indexes are
built into a separate eval DB. Everything it reads or writes lives under
``EVAL_LOCAL`` (default ``eval/local``), which is git-ignored.
"""
