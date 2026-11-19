"""Plan §13 — contextual chunk headers, measured on the frozen set.

Re-embeds every chunk under several header variants with the active embedding
model and compares dense retrieval (exact search, the user's visibility,
production's tag append) on the same queries. Inputs, all in EVAL_LOCAL:

  raw/chunks.jsonl      the stored chunks (id, doc, page, ci, text)
  raw/docs_meta.json    Paperless metadata (title) per document
  raw/extractions/      the sidecars (page summaries, document summary)
  rankings.jsonl        tags and agent queries from `rank`
  visible_<user>.json

Runs anywhere with sentence-transformers (a GPU makes it minutes):
  python -m eval.harness.headers [--model intfloat/multilingual-e5-large-instruct]
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from eval.harness.common import LOCAL, load_jsonl, load_queries
from eval.harness.run import CATEGORIES, _agg, _c0, _paired, per_query

E5_QUERY = "Instruct: Given a search query, retrieve relevant passages that answer the query\nQuery: "
PREFIX_RE = re.compile(r"^Dokument: (?P<ctx>.*?) — (?P<body>.*)$", re.S)


def _first_sentence(text: str, limit: int = 200) -> str:
    text = " ".join((text or "").split())
    m = re.match(r"(.{20,}?[.!?])\s", text + " ")
    s = m.group(1) if m else text
    return s if len(s) <= limit else s[:limit].rsplit(" ", 1)[0] + " …"


def _title(meta: dict) -> str:
    return " ".join((meta.get("title") or "").replace("_", " ").split())


def variants(chunk: dict, meta: dict, sidecar: dict) -> dict[str, str]:
    m = PREFIX_RE.match(chunk["text"])
    ctx, body = (m.group("ctx"), m.group("body")) if m else ("", chunk["text"])
    title = _title(meta)
    page_sum = ""
    for p in sidecar.get("pages") or []:
        if p.get("page_number") == chunk.get("page"):
            page_sum = _first_sentence(p.get("page_summary") or "")
    doc_sum = _first_sentence(sidecar.get("full_summary_summarized") or sidecar.get("full_summary") or "")
    head = f"Dokument: {title} | {ctx} — " if ctx else f"Dokument: {title} — "
    return {
        "B0": chunk["text"],
        "H1": head + body,
        "H2": head + (f"Inhalt: {doc_sum} — " if doc_sum else "") + body,
        "H3": head + (f"Seite: {page_sum} — " if page_sum else "") + body,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="intfloat/multilingual-e5-large-instruct")
    a = ap.parse_args()

    import numpy as np
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(a.model, device="cuda", model_kwargs={"torch_dtype": "float16"})
    tok = model.tokenizer
    limit = model.max_seq_length

    def token_len(t: str) -> int:
        return len(tok(t, add_special_tokens=True)["input_ids"])

    chunks = load_jsonl(LOCAL / "raw" / "chunks.jsonl")
    meta = {d["id"]: d for d in json.loads((LOCAL / "raw" / "docs_meta.json").read_text())}
    side_dir = LOCAL / "raw" / "extractions"
    sidecars: dict[int, dict] = {}
    for c in chunks:
        if c["doc"] not in sidecars:
            p = side_dir / f"{c['doc']}.json"
            sidecars[c["doc"]] = json.loads(p.read_text()) if p.exists() else {}
    texts: dict[str, list[str]] = {}
    docs_of: dict[str, list[int]] = {}
    for c in chunks:
        for name, t in variants(c, meta.get(c["doc"], {}), sidecars[c["doc"]]).items():
            texts.setdefault(name, []).append(t)
            docs_of.setdefault(name, []).append(c["doc"])
    # Smaller chunks, re-cut from the sidecars as ingest does — to tell the
    # header's effect apart from the 512-token window it pushes text out of.
    from services.chunking import ChunkingConfig, chunk_document
    from services.page_images import embedding_lines

    ctx_of = {}
    for c in chunks:
        m = PREFIX_RE.match(c["text"])
        if m and c["doc"] not in ctx_of:
            ctx_of[c["doc"]] = m.group("ctx")
    for size, cfg in {"S": ChunkingConfig(target_words=150, max_words=190, overlap_words=25)}.items():
        for doc, sc in sidecars.items():
            pages = []
            for pg in sc.get("pages") or []:
                lines = embedding_lines(pg.get("images") or [])
                pages.append(f"{pg.get('page_text') or ''}\n\n{lines}".strip() if lines else (pg.get("page_text") or ""))
            ctx = ctx_of.get(doc, "")
            title = _title(meta.get(doc, {}))
            doc_sum = _first_sentence(sc.get("full_summary_summarized") or sc.get("full_summary") or "")
            heads = {
                f"{size}0": f"Dokument: {ctx} — " if ctx else "",
                f"{size}2": (f"Dokument: {title} | {ctx} — " if ctx else f"Dokument: {title} — ")
                + (f"Inhalt: {doc_sum} — " if doc_sum else ""),
            }
            for ch in chunk_document(pages, context_prefix="", config=cfg):
                for name, head in heads.items():
                    texts.setdefault(name, []).append(head + ch.text)
                    docs_of.setdefault(name, []).append(doc)

    # Token-fitted chunks (production since HEADER_VERSION 2): header + text
    # within the model window, the header built by the production code.
    from services.chunking import build_context_prefix, strip_context_prefix  # noqa: F401

    for doc, sc in sidecars.items():
        pages = []
        for pg in sc.get("pages") or []:
            lines = embedding_lines(pg.get("images") or [])
            pages.append(f"{pg.get('page_text') or ''}\n\n{lines}".strip() if lines else (pg.get("page_text") or ""))
        ctx = ctx_of.get(doc, "")
        title = _title(meta.get(doc, {}))
        summ = sc.get("full_summary_summarized") or sc.get("full_summary") or ""
        heads = {
            "T0": f"Dokument: {ctx} — " if ctx else "",
            "T2": (f"Dokument: {title} | {ctx} — " if ctx else f"Dokument: {title} — ")
            + (f"Inhalt: {_first_sentence(summ)} — " if _first_sentence(summ) else ""),
        }
        for name, head in heads.items():
            for ch in chunk_document(pages, context_prefix=head, token_len=token_len, max_tokens=limit):
                texts.setdefault(name, []).append(ch.text)
                docs_of.setdefault(name, []).append(doc)

    qs = load_queries()
    rows = {r["id"]: r for r in load_jsonl(LOCAL / "rankings.jsonl")}
    vis = {u: set(json.loads((LOCAL / f"visible_{u}.json").read_text())) for u in {q["user"] for q in qs}}
    inputs = {"user": [q["query"] for q in qs],
              "agent": [rows[q["id"]].get("agent_query") or q["query"] for q in qs]}
    qvec = {k: model.encode([E5_QUERY + t for t in v], prompt="", normalize_embeddings=True, batch_size=32)
            for k, v in inputs.items()}

    out = {}
    lines = ["# Contextual chunk headers — dense only, exact search", ""]
    lines += ["| variant | truncated chunks (> {} tokens) | mean tokens |".format(limit), "|---|---|---|"]
    for name, tl in texts.items():
        ntok = [len(tok(t, add_special_tokens=True)["input_ids"]) for t in tl]
        lines.append(f"| {name} | {sum(n > limit for n in ntok)} of {len(ntok)} | {sum(ntok) / len(ntok):.0f} |")
        lines[-1] = lines[-1][:-1] + f"| {len(tl)} chunks |"
        emb = model.encode(tl, prompt="", normalize_embeddings=True, batch_size=32, show_progress_bar=False)
        chunk_doc = np.asarray(docs_of[name])
        for qname, qv in qvec.items():
            res = []
            for q, v in zip(qs, qv):
                mask = np.isin(chunk_doc, list(vis[q["user"]]))
                sims = np.where(mask, emb @ v, -np.inf)
                order = np.argsort(-sims, kind="stable")[:100]
                dense = list(dict.fromkeys(int(chunk_doc[i]) for i in order))
                docs = _c0({"dense": dense, "tags": rows[q["id"]].get("tags_a" if qname == "agent" and "tags_a" in rows[q["id"]] else "tags", [])},
                           vis[q["user"]])
                res.append(per_query(docs, q))
            out[(name, qname)] = res
    lines.append("")

    for qname in inputs:
        base = out[("B0", qname)]
        lines += [f"## Query = {qname} wording", "",
                  "| variant | R@5 | R@10 | MRR@10 | nDCG@10 | R@10 net | first-rel +/=/− | long_doc MRR | long_doc R@5 |",
                  "|---|---|---|---|---|---|---|---|---|"]
        idx = [i for i, q in enumerate(qs) if q["category"] == "long_doc"]
        for name in texts:
            m = out[(name, qname)]
            g = _agg(m)
            i, _, d = _paired(base, m, "r10")
            fi, fs, fd = _paired(base, m, "first")
            ld = _agg([m[j] for j in idx])
            lines.append(f"| {name} | {g['r5']:.3f} | {g['r10']:.3f} | {g['mrr']:.3f} | {g['ndcg']:.3f} | "
                         f"{i - d:+d} | {fi}/{fs}/{fd} | {ld['mrr']:.2f} | {ld['r5']:.2f} |")
        lines += ["", "MRR@10 per category:", "",
                  "| variant | " + " | ".join(c for c in CATEGORIES if c != "no_answer") + " |",
                  "|---|" + "---|" * (len(CATEGORIES) - 1)]
        for name in texts:
            m = out[(name, qname)]
            cells = [f"{_agg([m[j] for j, q in enumerate(qs) if q['category'] == c]).get('mrr', 0):.2f}"
                     for c in CATEGORIES if c != "no_answer"]
            lines.append(f"| {name} | " + " | ".join(cells) + " |")
        lines.append("")
        lines += ["First relevant rank per query (changed vs B0 only):", ""]
        for j, q in enumerate(qs):
            ranks = {n: out[(n, qname)][j].get("first") for n in texts}
            if len(set(ranks.values())) > 1 and q["category"] != "no_answer":
                lines.append(f"- {q['id']} ({q['category']}): " + ", ".join(f"{n} {r or '—'}" for n, r in ranks.items()))
        lines.append("")

    report = "\n".join(lines)
    Path(LOCAL / "headers_report.md").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
