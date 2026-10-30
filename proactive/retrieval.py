"""What of the findings folder may be retrieved (architecture §11, Phase 9).

Decided by code from the frontmatter at indexing time (`vault/sync._embed_file`):

- findings with status `surfaced | interested | researched | done` and all
  dossiers are indexed into the `vault` collection with `source_type` and
  `trust = low`;
- `new`, `held`, `snoozed`, `dismissed` findings and `_interests.md` are not —
  and lose their chunks when their status moves there (a status edit in
  Obsidian re-indexes the file);
- with `PROACTIVE_ENABLED=false` nothing of the folder is indexed.

Retrieval shows them marked as the system's own conclusions, and the prompt
says to cite the underlying documents instead (`config/chat_prompts.FINDINGS_RULE`).
"""

from __future__ import annotations

from pathlib import PurePosixPath

INDEXED_STATUSES = frozenset({"surfaced", "relevant", "interested", "researched", "done"})


def index_meta(rel_path, meta: dict) -> dict | None:
    """Extra Chroma metadata for an indexable finding/dossier, or None (= keep
    it out of the index)."""
    from config.proactive import is_enabled
    from proactive.paths import DOSSIERS, INTERESTS, NOTICES, findings_rel

    if not is_enabled():
        return None
    try:
        inside = PurePosixPath(str(rel_path)).relative_to(findings_rel())
    except ValueError:
        return None
    if inside.name == INTERESTS or not inside.parts:
        return None
    top = inside.parts[0]
    if top == DOSSIERS and meta.get("topic"):
        return {"source_type": "dossier", "trust": "low"}
    if top == NOTICES and meta.get("finding_id"):
        status = str(meta.get("status") or "").strip().lower()
        if status in INDEXED_STATUSES:
            return {"source_type": "finding", "trust": "low", "finding_status": status}
    return None
