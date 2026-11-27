"""Configurations, fusion, metrics and the report. Pure Python over rankings.jsonl —
no model, no network, so every rerun of the matrix is deterministic."""

from __future__ import annotations

import json
import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from eval.harness.common import (
    FINAL_K,
    KEYWORDS,
    LOCAL,
    PROD_C0,
    QUERIES,
    RANKINGS,
    RESULTS,
    RRF_K,
    file_hash,
    load_jsonl,
    load_queries,
)

CATEGORIES = ["semantic", "identifier", "entity", "synonym", "compound", "long_doc", "no_answer"]
LEX = {"C1": "lex_u", "C2": "lex_t", "C3": "pl_or", "C3and": "pl_and"}


def _visible(user: str, cache: dict) -> set[int]:
    if user not in cache:
        cache[user] = set(json.loads((LOCAL / f"visible_{user}.json").read_text()))
    return cache[user]


def _c0(row: dict, vis: set[int]) -> list[int]:
    """Production: the user's visible documents only (filtered before ranking,
    since 2026-11-19), top 20 by best chunk distance, then tag-only hits."""
    docs = [d for d in row["dense"] if d in vis][:FINAL_K]
    return _append_tags(docs, row)


def _append_tags(docs: list[int], row: dict) -> list[int]:
    """Production appends tag-only hits: docs whose tag equals a query word and
    that are in none of the dense chunks (not merely outside the top 20)."""
    out = list(docs)
    pool = set(row["dense"]) | set(out)
    for d in row.get("tags", []):
        if len(out) >= FINAL_K:
            break
        if d not in pool:
            out.append(d)
            pool.add(d)
    return out[:FINAL_K]


def rrf(lists: list[tuple[list[int], float]], k: int = RRF_K) -> list[int]:
    score: dict[int, float] = defaultdict(float)
    first: dict[int, int] = {}
    for li, (docs, w) in enumerate(lists):
        for r, d in enumerate(docs, 1):
            score[d] += w / (k + r)
            first.setdefault(d, li * 10_000 + r)
    return sorted(score, key=lambda d: (-score[d], first[d]))


def _fused(row: dict, vis: set[int], parts: list[tuple[str, float]], k: int = RRF_K) -> list[int]:
    lists = [([d for d in row.get(key, []) if d in vis], w) for key, w in parts]
    return _append_tags(rrf(lists, k)[:FINAL_K], row)


_DIGIT = re.compile(r"\d")


def codey(query: str) -> bool:
    """Gate for the PARTIAL-GO variant: the query carries a code — ≥ 5 digits in
    total, or one token of ≥ 5 characters mixing letters and digits."""
    if len(_DIGIT.findall(query)) >= 5:
        return True
    return any(len(w) >= 5 and _DIGIT.search(w) and re.search(r"[A-Za-z]", w) for w in query.split())


def configs(best_lex: str, best_w: float, best_kw_w: float | None, have_kw: bool) -> dict:
    """name → (function(row, vis) → doc list, lexical keys it needs)."""
    # Every config but C0 reads `dense` = the exact (brute-force) ranking, so a rerun
    # of `rank` cannot move them; C0 is the HNSW ranking, checked against production.
    cfg = {"C0": (lambda r, v: _c0({**r, "dense": r["dense_hnsw"]}, v), []),
           "C0x": (lambda r, v: _c0(r, v), [])}
    for name, key in LEX.items():
        cfg[name] = (lambda r, v, key=key: [d for d in r[key] if d in v][:FINAL_K], [key])
    cfg["C4"] = (lambda r, v: _fused(r, v, [("dense", 1.0), (best_lex, 1.0)]), [best_lex])
    for w in (0.25, 0.5, 0.75):
        cfg[f"C5w{w}"] = (lambda r, v, w=w: _fused(r, v, [("dense", 1.0), (best_lex, w)]), [best_lex])
    # PARTIAL-GO candidate: lexical only for a query with a code in it.
    for w in (0.75, 1.0):
        cfg[f"G{w}"] = (lambda r, v, w=w: _fused(r, v, [("dense", 1.0), (best_lex, w)]) if r.get("codey") else _c0(r, v),
                        [best_lex])
    # The system as it is: the agent's own route — search_exact where it chose it, else search.
    def _sys(inner):
        return lambda r, v: [d for d in r["x0"] if d in v][:FINAL_K] if r.get("route") == "search_exact" else inner(r, v)
    cfg["SYS"] = (_sys(lambda r, v: _c0(r, v)), [])
    cfg["SYS+C4"] = (_sys(lambda r, v: _fused(r, v, [("dense", 1.0), (best_lex, 1.0)])), [best_lex])
    cfg["SYS+G1.0"] = (_sys(lambda r, v: _fused(r, v, [("dense", 1.0), (best_lex, 1.0)]) if r.get("codey") else _c0(r, v)), [best_lex])
    # "A": the agent's rewritten semantic_query as the input (what production sees).
    def _a(r):
        if "dense_a" not in r:
            return r
        return {**r, "dense": r["dense_a_exact"], "lex_u": r["lex_u_a"], "lex_t": r["lex_t_a"], "tags": r["tags_a"]}
    cfg["A0"] = (lambda r, v: _c0(_a(r), v), [])
    cfg["A4"] = (lambda r, v: _fused(_a(r), v, [("dense", 1.0), (best_lex, 1.0)]), [best_lex])
    cfg["SYS-A"] = (_sys(lambda r, v: _c0(_a(r), v)), [])
    cfg["SYS-A+G1.0"] = (_sys(lambda r, v: _fused(_a(r), v, [("dense", 1.0), (best_lex, 1.0)])
                                if r.get("codey") else _c0(_a(r), v)), [best_lex])
    if have_kw:
        for wk in (0.5, 1.0):
            cfg[f"A7k{wk}"] = (lambda r, v, wk=wk: _fused(_a(r), v, [("dense", 1.0), ("kw_u", wk)]), ["kw_u"])
    # Doc-level fusion with Paperless (H4).
    cfg["C4d"] = (lambda r, v: _fused(r, v, [("dense", 1.0), ("pl_or", 1.0)]), ["pl_or"])
    cfg[f"C5d"] = (lambda r, v: _fused(r, v, [("dense", 1.0), ("pl_or", best_w)]), ["pl_or"])
    if have_kw:
        kw_key = "kw_u" if best_lex in ("lex_u", "pl_or", "pl_and") else "kw_t"
        for wk in (0.25, 0.5, 1.0):
            cfg[f"C6k{wk}"] = (
                lambda r, v, wk=wk: _fused(r, v, [("dense", 1.0), (best_lex, best_w), (kw_key, wk)]),
                [best_lex, kw_key],
            )
            cfg[f"C7k{wk}"] = (lambda r, v, wk=wk: _fused(r, v, [("dense", 1.0), (kw_key, wk)]), [kw_key])
    return cfg


# ── metrics ───────────────────────────────────────────────────────────────────


def _first_rel(docs: list[int], rel: set[int]) -> int | None:
    return next((i for i, d in enumerate(docs, 1) if d in rel), None)


def per_query(docs: list[int], q: dict) -> dict:
    rel = set(q.get("relevant_docs") or [])
    part = set(q.get("partially_relevant_docs") or [])
    if not rel:
        return {"first": None}
    fr = _first_rel(docs[:10], rel)
    dcg = sum((2 if d in rel else 1 if d in part else 0) / math.log2(i + 1) for i, d in enumerate(docs[:10], 1))
    ideal = [2] * len(rel) + [1] * len(part)
    idcg = sum(g / math.log2(i + 1) for i, g in enumerate(ideal[:10], 1))
    return {
        "r5": len(rel & set(docs[:5])) / len(rel),
        "r10": len(rel & set(docs[:10])) / len(rel),
        "mrr": 1 / fr if fr else 0.0,
        "ndcg": dcg / idcg if idcg else 0.0,
        "first": _first_rel(docs, rel),
    }


def _agg(ms: list[dict]) -> dict:
    ms = [m for m in ms if "r10" in m]
    if not ms:
        return {}
    return {k: statistics.mean(m[k] for m in ms) for k in ("r5", "r10", "mrr", "ndcg")} | {"n": len(ms)}


def _paired(a: list[dict], b: list[dict], key: str) -> tuple[int, int, int]:
    """(improved, unchanged, degraded) of b vs a."""
    imp = same = deg = 0
    for x, y in zip(a, b):
        if "r10" not in x:
            continue
        if key == "first":
            fx = x["first"] or 10_000
            fy = y["first"] or 10_000
            better, worse = fy < fx, fy > fx
        else:
            better, worse = y[key] > x[key] + 1e-9, y[key] < x[key] - 1e-9
        imp += better
        deg += worse
        same += not (better or worse)
    return imp, same, deg


def _pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(p * (len(s) - 1))))]


# ── run ───────────────────────────────────────────────────────────────────────


def _best_lex(rows, qs, vis_cache) -> str:
    """Pick the better FTS5 tokenizer by Recall@10 (then MRR) over the answerable queries."""
    scores = {}
    for key in ("lex_u", "lex_t"):
        ms = [per_query([d for d in r[key] if d in _visible(q["user"], vis_cache)], q) for r, q in zip(rows, qs)]
        a = _agg(ms)
        scores[key] = (a["r10"], a["mrr"])
    return max(scores, key=scores.get)


def check_c0() -> tuple[int, int, list[str]]:
    rows = {r["id"]: r for r in load_jsonl(RANKINGS)}
    prod = load_jsonl(PROD_C0)
    vis_cache: dict = {}
    qs = {q["id"]: q for q in load_queries()}
    bad = []
    for p in prod:
        mine = _c0(rows[p["id"]], _visible(qs[p["id"]]["user"], vis_cache))
        if mine != p["docs"]:
            bad.append(p["id"])
    return len(prod) - len(bad), len(prod), bad


def run(only: str | None = None) -> Path:
    qs = load_queries()
    rows_by = {r["id"]: r for r in load_jsonl(RANKINGS)}
    rows = [rows_by[q["id"]] for q in qs]
    for r, q in zip(rows, qs):
        r["codey"] = codey(q["query"])
        r["dense_hnsw"], r["dense"] = r["dense"], r["dense_exact"]
    vis_cache: dict = {}
    have_kw = all("kw_u" in r for r in rows)
    best_lex = _best_lex(rows, qs, vis_cache)

    def evaluate(cfgs):
        res = {}
        for name, (fn, _) in cfgs.items():
            docs = [fn(r, _visible(q["user"], vis_cache)) for r, q in zip(rows, qs)]
            res[name] = {"docs": docs, "m": [per_query(d, q) for d, q in zip(docs, qs)]}
        return res

    # Weight for the lexical list: the best C5 by Recall@10 net vs C0, then MRR.
    pre = evaluate(configs(best_lex, 0.5, None, False))
    c0m = pre["C0x"]["m"]

    def net(name, res):
        i, _, d = _paired(c0m, res[name]["m"], "r10")
        return (i - d, _agg(res[name]["m"])["mrr"])

    best_c5 = max((n for n in pre if n.startswith("C5w")), key=lambda n: net(n, pre))
    best_w = float(best_c5[3:])
    res = evaluate(configs(best_lex, best_w, None, have_kw))

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    outdir = RESULTS / ts
    outdir.mkdir(parents=True, exist_ok=True)
    meta = {
        "queries": str(QUERIES), "queries_hash": file_hash(QUERIES),
        "rankings_hash": file_hash(RANKINGS), "keywords_hash": file_hash(KEYWORDS),
        "final_k": FINAL_K, "rrf_k": RRF_K, "best_lex": best_lex, "best_w_lex": best_w,
    }
    # RRF k sanity sweep on the best fused config only (not tuned on).
    best_fused = max((n for n in res if n.startswith(("C4", "C5"))), key=lambda n: net(n, res))
    k_sweep = {}
    parts = {"C4": [("dense", 1.0), (best_lex, 1.0)], "C4d": [("dense", 1.0), ("pl_or", 1.0)],
             "C5d": [("dense", 1.0), ("pl_or", best_w)]}.get(best_fused, [("dense", 1.0), (best_lex, best_w)])
    for k in (20, 60):
        m = [per_query(_fused(r, _visible(q["user"], vis_cache), parts, k), q) for r, q in zip(rows, qs)]
        i, _, d = _paired(c0m, m, "r10")
        k_sweep[k] = {"net_r10": i - d, **_agg(m)}

    if only:
        res = {k: v for k, v in res.items() if k == only or k == "C0"}
    report = _report(qs, rows, res, meta, best_fused, k_sweep)
    (outdir / "report.md").write_text(report, encoding="utf-8")
    (outdir / "per_query.json").write_text(json.dumps(
        {n: [{"id": q["id"], "docs": d[:10], **m} for q, d, m in zip(qs, v["docs"], v["m"])] for n, v in res.items()},
        ensure_ascii=False, indent=1), encoding="utf-8")
    (outdir / "meta.json").write_text(json.dumps(meta, indent=1))
    print(report)
    print(f"→ {outdir / 'report.md'}")
    return outdir


def _report(qs, rows, res, meta, best_fused, k_sweep) -> str:
    L = [f"# Hybrid-search eval — {datetime.now():%Y-%m-%d %H:%M}", "", "```", json.dumps(meta, indent=1), "```", ""]
    ok, n, bad = check_c0() if PROD_C0.exists() else (0, 0, [])
    L += [f"C0 vs production `search`: **{ok}/{n}** identical top-{FINAL_K} lists" + (f" (differ: {bad})" if bad else ""), ""]

    c0m = res["C0x"]["m"]
    same = all(a.get("r10") == b.get("r10") and a.get("first") == b.get("first") for a, b in zip(res["C0"]["m"], c0m))
    L += [f"Paired baseline: **C0x** (exact dense). Per-query R@10 and first-relevant rank identical to C0: **{same}**.", ""]
    L += ["## Overall (answerable queries)", "",
          "| config | R@5 | R@10 | MRR@10 | nDCG@10 | R@10 vs C0 +/=/− | net | first-rel vs C0 +/=/− |",
          "|---|---|---|---|---|---|---|---|"]
    for name, v in res.items():
        a = _agg(v["m"])
        i, s, d = _paired(c0m, v["m"], "r10")
        fi, fs, fd = _paired(c0m, v["m"], "first")
        L.append(f"| {name} | {a['r5']:.3f} | {a['r10']:.3f} | {a['mrr']:.3f} | {a['ndcg']:.3f} | "
                 f"{i}/{s}/{d} | {i - d:+d} | {fi}/{fs}/{fd} |")
    L.append("")

    L += ["## Per category — R@10 (net vs C0 in brackets)", ""]
    cats = [c for c in CATEGORIES if any(q["category"] == c for q in qs) and c != "no_answer"]
    L += ["| config | " + " | ".join(f"{c} (n={sum(q['category'] == c for q in qs)})" for c in cats) + " |",
          "|---|" + "---|" * len(cats)]
    for name, v in res.items():
        cells = []
        for c in cats:
            idx = [i for i, q in enumerate(qs) if q["category"] == c]
            a = _agg([v["m"][i] for i in idx])
            ii, _, dd = _paired([c0m[i] for i in idx], [v["m"][i] for i in idx], "r10")
            cells.append(f"{a.get('r10', 0):.2f} ({ii - dd:+d})")
        L.append(f"| {name} | " + " | ".join(cells) + " |")
    L.append("")

    L += ["## Per category — MRR@10", "", "| config | " + " | ".join(cats) + " |", "|---|" + "---|" * len(cats)]
    for name, v in res.items():
        cells = []
        for c in cats:
            idx = [i for i, q in enumerate(qs) if q["category"] == c]
            cells.append(f"{_agg([v['m'][i] for i in idx]).get('mrr', 0):.2f}")
        L.append(f"| {name} | " + " | ".join(cells) + " |")
    L.append("")

    # Latency of the retrieval calls (ms). A fused config runs dense + one lexical list (+ keywords).
    L += ["## Latency (ms, per retrieval call)", "", "| part | p50 | p95 |", "|---|---|---|"]
    for key in ("embed", "dense", "lex_u", "lex_t", "pl_or", "pl_and", "kw_u", "kw_t", "x0"):
        vals = [r["lat"][key] for r in rows if key in r["lat"]]
        if vals:
            L.append(f"| {key} | {_pct(vals, .5):.1f} | {_pct(vals, .95):.1f} |")
    L.append("")

    L += ["## RRF k sanity sweep on " + best_fused, ""]
    for k, a in k_sweep.items():
        L.append(f"- k={k}: R@10 {a['r10']:.3f}, MRR {a['mrr']:.3f}, net R@10 vs C0 {a['net_r10']:+d}")
    L.append("")

    na = [i for i, q in enumerate(qs) if q["category"] == "no_answer"]
    if na:
        ans = [i for i, q in enumerate(qs) if q["category"] != "no_answer"]
        top = lambda i: rows[i]["dense_dist"][0] if rows[i]["dense_dist"] else None
        L += ["## no_answer — top-1 dense distance", "",
              f"- no_answer: {sorted(round(top(i), 3) for i in na)}",
              f"- answerable median {statistics.median(top(i) for i in ans):.3f}, "
              f"p10 {_pct([top(i) for i in ans], .1):.3f}, p90 {_pct([top(i) for i in ans], .9):.3f}",
              f"- lexical lists non-empty on no_answer (lex_u / lex_t / pl_or): "
              f"{sum(bool(rows[i]['lex_u']) for i in na)} / {sum(bool(rows[i]['lex_t']) for i in na)} / "
              f"{sum(bool(rows[i]['pl_or']) for i in na)} of {len(na)}", ""]

    L += ["## Per query — rank of first relevant doc (— = not in top 20)", "",
          "| id | cat | " + " | ".join(res) + " |", "|---|---|" + "---|" * len(res)]
    for i, q in enumerate(qs):
        if q["category"] == "no_answer":
            continue
        L.append(f"| {q['id']} | {q['category']} | " + " | ".join(
            str(v["m"][i]["first"] or "—") for v in res.values()) + " |")
    L.append("")
    return "\n".join(L)
