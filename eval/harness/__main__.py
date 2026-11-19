"""python -m eval.harness <step>

  prepare [--users A,B]   visibility per user + FTS5 indexes from the Chroma copy
  prod                    production `search` results (the C0 reference)
  keywords                agent keywords, once, cached (local model only)
  rank                    every base ranker → rankings.jsonl
  run [--config NAME]     the matrix → results/<timestamp>/report.md
  check                   C0 vs production, query by query
  pool [--n 10]           union of the rankers' top n per query (ground-truth review aid)

Steps up to `rank` need the app environment (container, cwd /app); `run`,
`check` and `pool` only need eval/local.
"""

from __future__ import annotations

import argparse
import json

from eval.harness.common import LOCAL, RANKINGS, load_jsonl, load_queries


def main() -> None:
    ap = argparse.ArgumentParser(prog="eval.harness")
    ap.add_argument("step", choices=["prepare", "prod", "keywords", "rank", "run", "check", "pool"])
    ap.add_argument("--users", required=False, default="", help="comma-separated Paperless usernames")
    ap.add_argument("--config")
    ap.add_argument("--n", type=int, default=10)
    a = ap.parse_args()

    if a.step == "prepare":
        from eval.harness.rankers import prepare

        users = [u for u in a.users.split(",") if u] or sorted({q["user"] for q in load_queries()})
        prepare(users)
    elif a.step == "prod":
        from eval.harness.rankers import prod

        prod()
    elif a.step == "keywords":
        from eval.harness.keywords import generate

        generate()
    elif a.step == "rank":
        from eval.harness.rankers import rank

        rank()
    elif a.step == "run":
        from eval.harness.run import check_c0, run

        if a.config == "C0":
            ok, n, bad = check_c0()
            print(f"C0 reproduces production for {ok}/{n} queries" + (f"; differ: {bad}" if bad else ""))
            if bad:
                raise SystemExit(1)
        run(a.config)
    elif a.step == "check":
        from eval.harness.run import check_c0

        ok, n, bad = check_c0()
        print(f"{ok}/{n} identical; differ: {bad}")
    elif a.step == "pool":
        rows = {r["id"]: r for r in load_jsonl(RANKINGS)}
        out = {}
        for q in load_queries():
            r = rows.get(q["id"])
            if not r:
                continue
            pool = []
            for key in ("dense", "lex_u", "lex_t", "pl_or", "kw_u"):
                pool += r.get(key, [])[: a.n]
            out[q["id"]] = list(dict.fromkeys(pool))
        (LOCAL / "pool.json").write_text(json.dumps(out))
        print(f"pool for {len(out)} queries → {LOCAL / 'pool.json'}")


if __name__ == "__main__":
    main()
