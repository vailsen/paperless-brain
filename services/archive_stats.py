"""What the dashboard shows about the archive (docs/ux-overhaul-plan.md, Phase 6).

Everything here is computed in code from data the app already has — the
user's document catalogue (`PaperlessClient.list_catalogue`, their own token)
and the active index's vectors — never by a model, and always over the
documents *this user* can see: a topic label built from someone else's
correspondents would show them.

- `over_time`: documents per month (created or added).
- `health`: what keeps the archive tidy (no correspondent, no type, no tags).
- `duplicates`: near-identical pairs, from the mean vector of each document's
  chunks.

Removed after a look at a real archive (2026-11-27): "expected but missing"
(rhythm detection), topics (k-means clusters named after correspondents mixed
unrelated ones) and "possibly mis-filed".
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime

import numpy as np


def _d(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    return v if isinstance(v, date) else None


# ── Over time ────────────────────────────────────────────────────────────────

def months_back(today: date, n: int) -> list[str]:
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out[::-1]


def over_time(docs: list, *, field_name: str = "created", months: int = 24, today: date | None = None,
              doc_type: str = "") -> dict:
    """{"months": ["2025-01", …], "counts": [n, …]} for the last `months` months."""
    today = today or date.today()
    keys = months_back(today, months)
    c = Counter()
    for d in docs:
        if doc_type and (d.document_type or "") != doc_type:
            continue
        v = _d(getattr(d, field_name, None))
        if v:
            c[f"{v:%Y-%m}"] += 1
    return {"months": keys, "counts": [c.get(k, 0) for k in keys]}


# ── Expected but missing ─────────────────────────────────────────────────────

# ── Health ───────────────────────────────────────────────────────────────────

def health(docs: list) -> dict[str, int]:
    return {
        "no_correspondent": sum(1 for d in docs if not d.correspondent),
        "no_type": sum(1 for d in docs if not d.document_type),
        "no_tags": sum(1 for d in docs if not d.tags),
    }


# ── Vectors: duplicates ────────────────────────────────────

def doc_vectors(chunks: list[dict], allowed: set[int]) -> tuple[list[int], np.ndarray]:
    """Mean unit vector per document from its chunks (`{"metadata", "embedding"}`)."""
    acc: dict[int, list] = defaultdict(list)
    for c in chunks:
        pid = (c.get("metadata") or {}).get("paperless_id")
        emb = c.get("embedding")
        if pid is None or emb is None:
            continue
        pid = int(pid)
        if pid in allowed:
            acc[pid].append(emb)
    ids = sorted(acc)
    if not ids:
        return [], np.zeros((0, 0), np.float32)
    mat = np.stack([np.mean(np.asarray(acc[i], dtype=np.float32), axis=0) for i in ids])
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    return ids, mat / np.where(norms == 0, 1, norms)


def duplicates(docs: list, ids: list[int], mat: np.ndarray, threshold: float = 0.985) -> list[tuple[int, int, float]]:
    """Pairs that are nearly the same text and have the same page count —
    double scans, a letter uploaded twice."""
    if len(ids) < 2:
        return []
    by_id = {d.id: d for d in docs}
    sim = mat @ mat.T
    out = []
    for i in range(len(ids)):
        for j in np.nonzero(sim[i, i + 1:] >= threshold)[0]:
            j = int(j) + i + 1
            a, b = by_id.get(ids[i]), by_id.get(ids[j])
            if a and b and (a.page_count or 0) == (b.page_count or 0):
                out.append((ids[i], ids[j], float(sim[i, j])))
    out.sort(key=lambda p: -p[2])
    return out[:50]
