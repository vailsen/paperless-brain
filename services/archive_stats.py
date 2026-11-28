"""What the dashboard shows about the archive (docs/ux-overhaul-plan.md, Phase 6).

Everything here is computed in code from data the app already has — the
user's document catalogue (`PaperlessClient.list_catalogue`, their own token)
and the active index's vectors — never by a model, and always over the
documents *this user* can see: a topic label built from someone else's
correspondents would show them.

- `over_time`: documents per month (created or added).
- `health`: what keeps the archive tidy (no correspondent, no type, no tags).
- `duplicates`: candidate pairs, from the mean vector of each document's
  chunks; `confirm_duplicates` keeps only those that are the same file (the
  checksum Paperless stores of the original) or the same text (a letter
  scanned twice) — the vectors alone also paired every monthly invoice of the
  same template with the next one.

Removed after a look at a real archive (2026-11-27): "expected but missing"
(rhythm detection), topics (k-means clusters named after correspondents mixed
unrelated ones) and "possibly mis-filed".
"""

from __future__ import annotations

import re
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


def duplicates(docs: list, ids: list[int], mat: np.ndarray, threshold: float = 0.975,
               limit: int = 100) -> list[tuple[int, int, float]]:
    """Candidate pairs: close in meaning and the same page count. A
    pre-selection only — `confirm_duplicates` decides."""
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
    return out[:limit]


# A number as written: "15.03.2026", "1.240,00", "DE89 3704" stays two, "4F60" none.
_NUM_RE = re.compile(r"\d(?:[\d.,/:-]*\d)?")
_WORD_RE = re.compile(r"[^\W\d_]{2,}")

#: Share of the word shingles two texts must share.
TEXT_MIN = 0.8
#: Numbers present in only one of the two texts: at most this many (OCR misreads
#: a digit now and then — one misread number is one missing plus one extra) …
NUM_DIFF_ABS = 2
#: … or this share of all numbers, whichever is larger.
NUM_DIFF_REL = 0.03


def _shingles(text: str, n: int = 3) -> set[tuple[str, ...]]:
    w = _WORD_RE.findall(text.lower())
    if len(w) < n:
        return {tuple(w)} if w else set()
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def same_text(a: str, b: str) -> bool:
    """The same document's text, read twice — not two letters on one template.

    Words decide whether it is the same kind of text; the numbers decide
    whether it is the same letter: a monthly invoice repeats every word of the
    last one and differs in its date, number and amounts."""
    a, b = a or "", b or ""
    sa, sb = _shingles(a), _shingles(b)
    if not sa or not sb:
        return False
    if len(sa & sb) / len(sa | sb) < TEXT_MIN:
        return False
    na, nb = Counter(m.rstrip(".,:-/") for m in _NUM_RE.findall(a)), Counter(
        m.rstrip(".,:-/") for m in _NUM_RE.findall(b))
    union = sum((na | nb).values())
    diff = sum((na - nb).values()) + sum((nb - na).values())
    return diff <= max(NUM_DIFF_ABS, NUM_DIFF_REL * union)


def confirm_duplicates(pairs: list[tuple[int, int, float]], checksums: dict[int, str],
                       texts: dict[int, str], limit: int = 50) -> list[tuple[int, int, float, str]]:
    """The candidate pairs that really are duplicates, with why:
    "file" — the same original file (Paperless' checksum of it);
    "text" — another file with the same text (scanned or exported twice).
    A pair with nothing to compare (no checksum, no text) is dropped: a
    duplicate shown on similarity alone was the near miss this step removes."""
    out = []
    for a, b, score in pairs:
        ca, cb = checksums.get(a), checksums.get(b)
        if ca and ca == cb:
            out.append((a, b, score, "file"))
        elif same_text(texts.get(a, ""), texts.get(b, "")):
            out.append((a, b, score, "text"))
    out.sort(key=lambda p: (p[3] != "file", -p[2]))
    return out[:limit]
