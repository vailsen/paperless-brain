"""What the dashboard shows about the archive (docs/ux-overhaul-plan.md, Phase 6).

Everything here is computed in code from data the app already has — the
user's document catalogue (`PaperlessClient.list_catalogue`, their own token)
and the active index's vectors — never by a model, and always over the
documents *this user* can see: a topic label built from someone else's
correspondents would show them.

- `over_time`: documents per month (created or added).
- `expected_missing`: series that come on a rhythm (monthly, quarterly, half-
  yearly, yearly) whose next document is overdue — with the history that makes
  the rhythm, so the user can judge it.
- `health`: what keeps the archive tidy (no correspondent, no type, no tags).
- `topics` / `duplicates` / `outliers`: from the mean vector of each document's
  chunks — k-means in numpy, labels from the topic's most common correspondents
  and types; near-identical pairs; documents whose type disagrees with their
  nearest neighbours.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np

# cadence name → (days, tolerance in days)
CADENCES = {"monthly": (30.4, 6), "quarterly": (91.3, 14), "half-yearly": (182.6, 25), "yearly": (365.25, 35)}


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

@dataclass
class Series:
    correspondent: str
    document_type: str
    cadence: str
    last: date
    expected: date
    history: list[date] = field(default_factory=list)
    last_id: int = 0

    @property
    def key(self) -> str:
        return f"{self.correspondent}|{self.document_type}"


def _cadence(intervals: list[float]) -> str | None:
    """The rhythm most intervals keep, if one does."""
    if not intervals:
        return None
    med = statistics.median(intervals)
    for name, (days, tol) in CADENCES.items():
        if abs(med - days) <= tol:
            within = sum(1 for i in intervals if abs(i - days) <= tol * 1.5)
            if within / len(intervals) >= 0.7:
                return name
    return None


def expected_missing(docs: list, *, today: date | None = None, dismissed: set[str] | None = None,
                     min_docs: int = 3) -> list[Series]:
    """Series whose next document is overdue — newest overdue first."""
    today = today or date.today()
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for d in docs:
        if d.correspondent and _d(d.created):
            groups[(d.correspondent, d.document_type or "")].append(d)
    out: list[Series] = []
    for (corr, typ), items in groups.items():
        items.sort(key=lambda d: _d(d.created))
        dates: list[date] = []
        for d in items:  # one per day: two pages of one letter are not a rhythm
            c = _d(d.created)
            if not dates or (c - dates[-1]).days > 3:
                dates.append(c)
        if len(dates) < min_docs:
            continue
        intervals = [(b - a).days for a, b in zip(dates, dates[1:])]
        cad = _cadence(intervals[-8:])
        if not cad:
            continue
        days, tol = CADENCES[cad]
        expected = dates[-1] + timedelta(days=round(days))
        overdue_since = (today - expected).days
        # Overdue beyond the tolerance, and not so long ago that the series
        # has clearly ended (more than three missed periods).
        if overdue_since <= max(tol, 7) or overdue_since > 3 * days:
            continue
        s = Series(corr, typ, cad, dates[-1], expected, dates[-8:], items[-1].id)
        if dismissed and s.key in dismissed:
            continue
        out.append(s)
    out.sort(key=lambda s: s.expected, reverse=True)
    return out


# ── Health ───────────────────────────────────────────────────────────────────

def health(docs: list) -> dict[str, int]:
    return {
        "no_correspondent": sum(1 for d in docs if not d.correspondent),
        "no_type": sum(1 for d in docs if not d.document_type),
        "no_tags": sum(1 for d in docs if not d.tags),
    }


# ── Vectors: topics, duplicates, outliers ────────────────────────────────────

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


def kmeans(mat: np.ndarray, k: int, *, iters: int = 30, seed: int = 7) -> np.ndarray:
    """Spherical k-means (cosine), k-means++ seeding. Returns a label per row."""
    rng = np.random.default_rng(seed)
    n = len(mat)
    k = max(1, min(k, n))
    centers = [mat[rng.integers(n)]]
    for _ in range(1, k):
        d = 1 - np.max(mat @ np.stack(centers).T, axis=1)
        d = np.clip(d, 0, None) ** 2
        p = d / d.sum() if d.sum() > 0 else None
        centers.append(mat[rng.choice(n, p=p)])
    c = np.stack(centers)
    labels = np.zeros(n, dtype=int)
    for _ in range(iters):
        new = np.argmax(mat @ c.T, axis=1)
        if np.array_equal(new, labels) and _ > 0:
            break
        labels = new
        for j in range(k):
            members = mat[labels == j]
            if len(members):
                v = members.mean(axis=0)
                c[j] = v / (np.linalg.norm(v) or 1)
    return labels


def silhouette(mat: np.ndarray, labels: np.ndarray, sample: int = 400, seed: int = 3) -> float:
    """Mean silhouette on a sample, cosine distance."""
    n = len(mat)
    if n < 3 or len(set(labels.tolist())) < 2:
        return -1.0
    idx = np.random.default_rng(seed).choice(n, size=min(sample, n), replace=False)
    dist = 1 - mat[idx] @ mat.T
    scores = []
    for row, i in zip(dist, idx):
        own = labels == labels[i]
        if own.sum() < 2:
            continue
        a = row[own].sum() / (own.sum() - 1)
        b = min(row[labels == j].mean() for j in set(labels.tolist()) if j != labels[i])
        scores.append((b - a) / max(a, b) if max(a, b) > 0 else 0)
    return float(np.mean(scores)) if scores else -1.0


@dataclass
class Topic:
    label: str
    ids: list[int]


def _label(members: list) -> str:
    corr = Counter(d.correspondent for d in members if d.correspondent)
    typ = Counter(d.document_type for d in members if d.document_type)
    tags = Counter(t for d in members for t in (d.tags or []))
    parts = []
    names = [c for c, n in corr.most_common(2) if n >= max(2, len(members) * 0.15)]
    if names:
        parts.append(", ".join(names))
    t = [x for x, n in typ.most_common(1) if n >= len(members) * 0.3]
    if t:
        parts.append(t[0])
    if not parts:
        parts = [x for x, _n in tags.most_common(2)]
    return " · ".join(parts) or "—"


def topics(docs: list, ids: list[int], mat: np.ndarray) -> list[Topic]:
    """Topics of the archive, labelled by code (no model, no invented names)."""
    if len(ids) < 8:
        return []
    by_id = {d.id: d for d in docs}
    n = len(ids)
    best, best_s = None, -2.0
    for k in sorted({max(2, round((n / 2) ** 0.5 * f)) for f in (0.6, 0.8, 1.0, 1.25)}):
        if k >= n:
            continue
        lab = kmeans(mat, k)
        s = silhouette(mat, lab)
        if s > best_s:
            best, best_s = lab, s
    if best is None:
        return []
    out = []
    for j in sorted(set(best.tolist())):
        members = [ids[i] for i in range(n) if best[i] == j]
        out.append(Topic(_label([by_id[i] for i in members if i in by_id]), members))
    out.sort(key=lambda t: -len(t.ids))
    return out


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


@dataclass
class Outlier:
    doc_id: int
    field: str  # "document_type" | "correspondent"
    has: str
    neighbours_say: str
    share: float


def outliers(docs: list, ids: list[int], mat: np.ndarray, k: int = 10, majority: float = 0.8) -> list[Outlier]:
    """Documents whose type (or correspondent) disagrees with the clear
    majority of their nearest neighbours — candidates to re-tag, nothing more."""
    if len(ids) <= k:
        return []
    by_id = {d.id: d for d in docs}
    sim = mat @ mat.T
    out = []
    for i, pid in enumerate(ids):
        d = by_id.get(pid)
        if d is None:
            continue
        near = [ids[j] for j in np.argsort(-sim[i])[1:k + 1]]
        for fld in ("document_type", "correspondent"):
            vals = Counter(getattr(by_id[j], fld) for j in near if j in by_id and getattr(by_id[j], fld))
            if not vals:
                continue
            top, n = vals.most_common(1)[0]
            mine = getattr(d, fld)
            if mine and top != mine and n / k >= majority:
                out.append(Outlier(pid, fld, mine, top, n / k))
                break
    out.sort(key=lambda o: -o.share)
    return out[:50]
