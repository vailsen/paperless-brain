"""Phase 6 acceptance on real data: run doc_analysis over sidecars and list the
deadlines for a hand spot-check (docs/proactive-tasks.md, Phase 6).

    PROACTIVE_ENABLED=true python scripts/proactive_deadline_check.py --model qwen3:32b --docs 40

Uses an operator model id on OLLAMA_SERVER. Writes `data/proactive_deadline_check.md`:
every extracted deadline with kind, actionable, the verbatim quote and the
document id, plus the counts that matter for the "218 overdue" question —
how many actionable deadlines lie in the past (they never produce a reminder)
and how many would produce one. Results are cached in proactive.db like
normal analyses, so a re-run only analyses documents it has not seen.
"""

import argparse
import asyncio
import json
import os
import random
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="model id on OLLAMA_SERVER")
    ap.add_argument("--docs", type=int, default=40)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--with-actions-only", action="store_true",
                    help="only documents whose sidecar has actions (the D2 set)")
    args = ap.parse_args()

    from config.proactive import is_enabled
    from config.settings import settings
    from proactive import analysis
    from proactive.models import ResolvedModel

    if not is_enabled():
        sys.exit("set PROACTIVE_ENABLED=true")
    side = Path(settings.app_path) / settings.extraction_sidecar_path
    ids = []
    for p in side.glob("*.json"):
        if not p.stem.isdigit():
            continue
        if args.with_actions_only:
            try:
                if not json.loads(p.read_text()).get("actions"):
                    continue
            except ValueError:
                continue
        ids.append(int(p.stem))
    random.Random(args.seed).shuffle(ids)
    model = ResolvedModel(args.model, "local", "default", args.model)
    today = date.today()
    rows, past, remind = [], 0, 0
    for i, doc_id in enumerate(ids[: args.docs], 1):
        try:
            a, _ = await analysis.ensure(doc_id, model, "", "")
        except Exception as exc:
            print(f"#{doc_id}: {type(exc).__name__}: {exc}")
            continue
        print(f"[{i}/{args.docs}] #{doc_id}: {len(a.deadlines)} deadline(s)")
        for d in a.deadlines:
            if d.actionable and d.day < today:
                past += 1
            if d.actionable and d.day >= today:
                remind += 1
            q = d.quote.replace("|", "/")
            rows.append(f"| {doc_id} | {d.date} | {d.kind} | {'yes' if d.actionable else 'no'} | {q} | |")
    out = Path(settings.app_path) / "data" / "proactive_deadline_check.md"
    out.write_text(
        f"# doc_analysis spot-check ({today}, model {args.model})\n\n"
        f"- actionable deadlines in the past (never a reminder): {past}\n"
        f"- actionable deadlines today or later (would remind): {remind}\n\n"
        "Mark the last column ✓/✗ for 20 rows: is `actionable` right?\n\n"
        "| doc | date | kind | actionable | quote | ok? |\n|---|---|---|---|---|---|\n"
        + "\n".join(rows) + "\n"
    )
    print(f"written: {out}")


if __name__ == "__main__":
    asyncio.run(main())
