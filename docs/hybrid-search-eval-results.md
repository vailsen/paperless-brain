# Hybrid search — evaluation results (P0–P3)

Plan: `docs/hybrid-search-eval-plan.md`. Reconnaissance: `eval/P0-recon.md`.
Harness: `eval/harness/` (`eval/README.md`). Real queries, ground truth, the
per-query report and annotated examples with document content are in
`eval/local/` (git-ignored); this file has numbers and anonymised patterns only.

Date: 2026-11-19. Instance: app 0.20.0, Paperless-ngx 3.5.1,
`multilingual-e5-large-instruct`, agent `qwen3.8:27b` (local). Corpus: 528
documents, 2 617 chunks, German.

## Decision

**NO-GO for hybrid search in `search` (P4, P5, P7) and NO-GO for the
`keywords` parameter (P6).** The plan's PARTIAL-GO (lexical only for queries
that carry a code) was checked and is not worth building either: the agent
already sends those queries to `search_exact`. What the eval did find is
cheaper to fix where it is — see *What I would do instead*.

This rests on a set that Claude drafted and judged. The owner's review of
`eval/local/review.md` is still owed (plan §5.3); the decision holds unless
that review moves several identifier or semantic judgments.

## What was measured

- 60 queries (12 semantic, 10 identifier, 8 entity, 10 synonym with
  distractors, 6 compound, 8 long-document, 6 without an answer), one user,
  document-level ground truth from pooling the top results of every ranker.
  Few real queries existed (31 chat messages, 39 logged Werkbank tool calls),
  so the set is drafted from the documents.
- Final k = 20 (production), candidate depth 50 chunks per lexical ranker,
  RRF k = 60. Every list is filtered by the user's own Paperless visibility
  before fusion.
- C0 reproduces the production `search` path for **60/60** queries (same
  process). All other configurations run on an exact dense ranking (C0x),
  which agrees with C0 query by query; two full reruns gave identical reports.
- Keyword generation: the production prompt and tools, an eval-only
  `keywords` parameter, the user's own local model, one call per query,
  cached.

## Results (54 answerable queries)

"net" = queries whose Recall@10 improved minus those that got worse, vs C0.

| config | what | R@5 | R@10 | MRR@10 | net |
|---|---|---|---|---|---|
| C0 | dense only (production `search`) | 0.811 | 0.876 | 0.728 | ±0 |
| C1 / C2 | BM25 only, FTS5 unicode61 / trigram | 0.611 / 0.667 | 0.678 / 0.717 | 0.647 / 0.705 | −15 / −12 |
| C3 | Paperless full text, OR (AND: R@10 0.294) | 0.615 | 0.740 | 0.618 | −11 |
| C4 | RRF dense + trigram, equal weight | 0.748 | 0.874 | 0.748 | **−1** |
| C5 | weighted, lexical 0.25 / 0.5 / 0.75 | 0.74 | 0.846 | 0.66–0.74 | −2 |
| C4d | RRF dense + Paperless (doc level) | 0.826 | 0.898 | 0.731 | ±0 |
| C6 | C5 + agent keywords as own list (w 1.0) | 0.828 | 0.925 | 0.808 | +2 |
| C7 | dense + agent keywords only (w 1.0) | 0.798 | 0.875 | 0.809 | −1 |
| G1.0 | C4 only when the query carries a code, else C0 | 0.909 | 0.981 | 0.833 | +6 |
| SYS | what the agent does: `search_exact` where it chose it, else C0 | 0.915 | 0.963 | 0.844 | +5 |
| SYS+G1.0 | SYS with the gated fusion in `search` | 0.915 | 0.963 | 0.844 | +5 |
| A0 | dense on the agent's own `semantic_query` rewrite | 0.797 | 0.866 | 0.760 | −1 |
| A4 | A0 fused with trigram | 0.817 | 0.915 | 0.839 | ±0 |
| A7 | A0 + agent keywords (w 0.5 / 1.0) | 0.756 / 0.745 | 0.824 / 0.829 | 0.774 / 0.769 | −6 / −5 |

Recall@10 by category (net vs C0 in brackets):

| config | semantic | identifier | entity | synonym | compound | long_doc |
|---|---|---|---|---|---|---|
| C0 | 1.00 | 0.43 | 1.00 | 1.00 | 0.83 | 1.00 |
| C4 | 0.70 (−5) | 1.00 (+6) | 1.00 | 0.83 (−2) | 0.74 (±0) | 1.00 |
| C5 w 0.75 | 0.70 (−5) | 0.85 (+5) | 1.00 | 0.83 (−2) | 0.74 (±0) | 1.00 |
| C4d | 0.88 (−3) | 0.92 (+6) | 1.00 | 0.87 (−2) | 0.70 (−1) | 1.00 |
| C6 k 1.0 | 0.97 (−1) | 0.95 (+6) | 1.00 | 0.85 (−2) | 0.71 (−1) | 1.00 |
| G1.0 | 1.00 | 1.00 (+6) | 1.00 | 1.00 | 0.83 | 1.00 |
| SYS | 1.00 | 0.90 (+5) | 1.00 | 1.00 | 0.83 | 1.00 |

Latency per retrieval call (ms, p50 / p95): query embedding 359 / 474 (CPU),
Chroma 7 / 8, FTS5 unicode61 1 / 2, FTS5 trigram 5 / 9, Paperless full text
131 / 194, `search_exact` 451 / 555.

## Against the fixed criteria

| GO for hybrid (best fused: C4) | needed | got | |
|---|---|---|---|
| R@10 net overall | ≥ +3 | −1 | ✗ |
| identifier + entity net | ≥ +3 | +6 | ✓ |
| no category below −1 | | semantic −5, synonym −2 | ✗ |
| added p95 latency | ≤ 50 ms | +9 ms (FTS5) | ✓ |

| GO for keywords (C6 vs best C4/C5) | needed | got | |
|---|---|---|---|
| net gain, mainly synonym/compound | ≥ +2 | +3, all of it identifier; synonym −2, compound −1 | ✗ |
| degradations from drift / hallucination | ≤ 1 | 2 | ✗ |
| output tokens per call | ≤ 30 | ≈ 24 | ✓ |

Lexical backend (H4): Paperless' index is within one query of FTS5 on its own
(−11 vs −12) but misses the latency budget (194 ms p95) and is useless with its
default AND operator. FTS5 trigram beat unicode61 (German compounds).
RRF k = 20 vs 60 on the best fused config: +1 vs ±0, i.e. insensitive.

## My take

**1. Lexical retrieval is the right tool for exactly one thing, and that
thing already has a tool.** Dense search cannot find a document by its
number: identifier Recall@10 is 0.43, and BM25 lifts it to 1.00. But the agent
sends all 10 identifier questions to `search_exact` on its own, which already
gets 0.90 (SYS). Putting BM25 into `search` behind a code-pattern gate (G1.0)
would be the cleanest PARTIAL-GO there is — +6, zero losses — and in the
system as it runs it changes nothing (SYS+G1.0 = SYS).

**2. Everywhere else lexical evidence is noise that RRF cannot discount.** On
the paraphrase and synonym queries BM25 ranks on whatever words of the
question happen to be frequent ("costs", "new", "apartment", "work"). A query
whose distinctive word does not occur in the corpus (colloquial term vs the
authority's term) leaves BM25 a list of pure noise, and at any weight from
0.25 up that list displaces relevant dense hits. Weighting does not rescue it
— C5 is worse than C4 at every weight — because RRF only sees ranks, not how
weak the lexical evidence was. H1 and H2 are both rejected.

**3. The dynamic thesaurus already exists: it is the agent's rewrite.** The
model does not pass the user's words to `search`; it writes a keyword-rich
`semantic_query` that already swaps colloquial terms for official ones. Dense
on that rewrite is level with C0 (−1 net, MRR +0.03); a separate `keywords`
list on top of it drops to −5/−6 net (A7).
Against the raw question, keywords do lift the first relevant document
(12 queries better, none worse, MRR +0.08), but they cost recall through
drift: generic terms like "certificate", "tax", "employer" pull in the tax
returns that quote every amount. H3 is rejected.

**4. The baseline is near its ceiling on this corpus.** C0 has Recall@10 =
1.00 in four of six categories. 528 documents with distinctive types and a
`Dokument: <type>, <correspondent>, <date> —` prefix on every chunk leave
dense search little to miss. Where headroom exists it is in ranking (MRR
0.69 on long documents, 0.82 semantic), not in recall — and fusion moves
MRR in both directions.

## What I would do instead

Status 2026-11-19: 1 and 3 are done (identifier R@10 with the agent's own routing
0.90 → 1.00, overall MRR 0.844 → 0.875; `search` latency unchanged with a warm id
cache); 2 is not; 4 is being measured.

1. **Fix `search_exact`'s literal match.** An identifier of the form
   "two letters + a 10-digit number" is not recognised by
   `_identifier_atoms` (digit groups are capped at 6), falls to the word OR,
   and `$contains` on the two letters matches hundreds of chunks: the right
   document landed at rank 12, outside the top 10. Accepting long digit
   groups, or dropping ≤ 2-character words when the text holds a code token,
   is a few lines and needs no index.
2. **Make dense search exact.** Chroma's HNSW returns a different chunk at the
   tail of a 100-chunk request depending on the process (3–4 of 60 top-20 lists
   changed between two runs). With 2 617 chunks a brute-force search is a few
   milliseconds and makes answers reproducible.
3. **Filter visibility before the cut, not after.** `search` takes the top 20
   documents of all users and then drops what the user's token cannot read, so
   another user's documents consume result slots. A `paperless_id $in`
   pre-filter (the filtered path already does this) gives every user a full
   result list.
4. **Contextual chunk headers (plan §13)** remain the open lever for long
   documents. The harness and this set are ready for it; note that a partial
   header (type, correspondent, date) is already embedded, so the experiment
   is adding the title and section, not adding a header.

## What would change the decision

- The owner's review moving identifier or semantic judgments.
- Real traffic with codes inside `search` queries rather than `search_exact`
  (the Werkbank log has a few amounts like "350.000" inside semantic queries —
  amounts, not identifiers). Then G1.0 is the design: lexical list only when
  the query carries ≥ 5 digits or a letters+digits token, FTS5 trigram, equal
  weight.
- A much larger or more homogeneous archive, where dense search starts
  missing documents that differ only by a name or number.

## Limits of this evaluation

- One user, 60 queries; one query ≈ 1.9 pp of the answerable set. Counts are
  the evidence, percentages are secondary.
- Queries and judgments by Claude; pooled judgments miss documents no ranker
  surfaced.
- The gate G1.0 is tuned to "codes" and this set has no number-bearing
  non-identifier queries, so its false-positive rate is untested here.
- Keyword generation ran single-turn; in a real conversation the model sees
  earlier turns and the memory hints.

## Follow-up: contextual chunk headers (plan §13), 2026-11-19

`eval/harness/headers.py`: every chunk re-embedded under each variant (same
model, fp16 on a GPU, B0 = today's text re-embedded the same way), exact dense
search, the user's visibility, production's tag append. Same frozen set.

| variant | chunk text | chunks > 512 tokens | MRR@10 user / agent wording | R@10 net user / agent | long_doc MRR user / agent |
|---|---|---|---|---|---|
| B0 | today: `Dokument: type, correspondent, date — ` + body | 1 081 of 2 617 | 0.732 / 0.763 | ±0 / ±0 | 0.69 / 0.78 |
| H1 | + document title | 1 230 | 0.774 / 0.780 | ±0 / +2 | 0.77 / 0.77 |
| **H2** | + title + first sentence of the document summary | 1 620 | **0.828 / 0.817** | **+2 / +3** | **0.88 / 0.88** |
| H3 | + title + first sentence of the page summary | 1 684 | 0.804 / 0.779 | +3 / +4 | 0.84 / 0.79 |
| S0 | today's prefix, chunks of 150 words (re-cut from the sidecars) | 176 of 3 469 | 0.789 / 0.774 | −1 / −1 | 0.94 / 0.94 |
| S2 | H2 header on 150-word chunks | 594 of 3 469 | 0.810 / 0.823 | +1 / +3 | 0.90 / 0.90 |

- **H2 is the variant to build.** Outside identifier queries (which go to
  `search_exact`) it moves the first relevant document up in 8 queries and
  down by one rank in 2; no other loss. Long documents 0.69 → 0.88.
- **Side finding: 41 % of today's chunks are longer than the model's 512-token
  window**, so their tail is never embedded (a 250-word German chunk is ~480
  tokens before the prefix). Only the 30-word overlap carries it into the next
  chunk. Smaller chunks (S0) fix that and help long documents most, but cost
  elsewhere; with the header on top (S2) they are level with H2. H2 keeps
  today's chunks, ids and citations and needs only new chunk text + embedding.
