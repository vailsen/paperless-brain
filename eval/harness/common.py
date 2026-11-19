"""Paths, data loading and query → lexical-query translation shared by every step."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

LOCAL = Path(os.environ.get("EVAL_LOCAL", "eval/local")).resolve()
QUERIES = Path(os.environ.get("EVAL_QUERIES", str(LOCAL / "queries.v1.jsonl")))
CHROMA_COPY = Path(os.environ.get("EVAL_CHROMA", str(LOCAL / "chroma_copy")))
LEX_DB = LOCAL / "lex.db"
RANKINGS = LOCAL / "rankings.jsonl"
PROD_C0 = LOCAL / "prod_c0.jsonl"
KEYWORDS = LOCAL / "keywords.v1.jsonl"
RESULTS = LOCAL / "results"

# Fixed parameters (plan §6.2). Changing one of them is a new experiment.
DENSE_CHUNKS = 100      # production: n_results (20) * 5
CANDIDATES = 50         # lexical candidate depth, in chunks
FINAL_K = 20            # production: search(n_results=20), trimmed to max results (20)
RRF_K = 60

# A short German + English stopword list. FTS5 has none; without one an OR query
# over a descriptive sentence ("die Rechnung von der …") ranks by "die"/"der".
STOPWORDS = set(
    """
    der die das den dem des ein eine einer eines einem einen und oder aber
    zu zum zur im in ins am an auf aus bei mit von vom für fur über uber unter
    nach vor ist sind war wurde wird werden hat haben habe mein meine meiner
    meines meinem meinen unser unsere ich wir sie er es was wie wo wann welche
    welcher welches nicht kein keine auch noch nur so als ob dass da dort hier
    alle alles mir mich uns ihr ihre sein seine man the a an of to for and or
    in on is are with from by at
    """.split()
)

_EDGE_PUNCT = ".,;:!?()[]{}\"'„“”‚‘’«»"


def words(query: str) -> list[str]:
    """Whitespace words of a query, edge punctuation stripped, stopwords dropped."""
    out = []
    for w in query.split():
        w = w.strip(_EDGE_PUNCT)
        if len(w) < 2 or w.lower() in STOPWORDS:
            continue
        out.append(w)
    return out


def _phrase(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def fts_or_query(terms: list[str], min_len: int = 2) -> str:
    """OR of quoted phrases. Each term is one phrase, so an identifier like
    'RE-2025-0042' stays one adjacency match instead of three loose tokens."""
    ph = [_phrase(t) for t in terms if len(t) >= min_len]
    return " OR ".join(dict.fromkeys(ph))


def sanitize_keywords(kws) -> list[str]:
    """Plan §10 sanitizer: max 8 terms, FTS5 syntax characters stripped, < 2 chars dropped."""
    out = []
    for k in kws or []:
        k = re.sub(r'["*^():{}]', " ", str(k)).strip()
        k = re.sub(r"\s+", " ", k)
        if len(k) >= 2 and k.lower() not in (x.lower() for x in out):
            out.append(k)
    return out[:8]


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def load_queries() -> list[dict]:
    return load_jsonl(QUERIES)


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12] if path.exists() else "-"


def collapse(chunk_docs: list[int]) -> list[int]:
    """Chunk ranking → document ranking by first occurrence."""
    return list(dict.fromkeys(chunk_docs))
