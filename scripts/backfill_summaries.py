#!/usr/bin/env python3
"""Backfill full_summary_summarized for sidecars that don't have it yet.

Usage:
    python scripts/backfill_summaries.py           # process all missing
    python scripts/backfill_summaries.py --force   # re-generate even if already set
"""

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config.settings import settings
from services.vision import OllamaVisionClient

SIDECAR_PATH = settings.extraction_sidecar_path


async def main(force: bool) -> None:
    vision = OllamaVisionClient(
        base_url=settings.ollama_server,
        model=settings.ollama_ingest_model,
    )

    files = sorted(
        f for f in os.listdir(SIDECAR_PATH) if f.endswith(".json") and f != "index.json"
    )

    total = len(files)
    skipped = 0
    updated = 0
    failed = 0

    for i, filename in enumerate(files, 1):
        path = os.path.join(SIDECAR_PATH, filename)
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"[{i}/{total}] SKIP  {filename}  (read error: {e})")
            failed += 1
            continue

        already_set = bool(data.get("full_summary_summarized", "").strip())
        if already_set and not force:
            skipped += 1
            continue

        full_summary = data.get("full_summary", "").strip()
        if not full_summary:
            print(f"[{i}/{total}] SKIP  {filename}  (no full_summary)")
            skipped += 1
            continue

        doc_id = data.get("paperless_id", filename)
        print(f"[{i}/{total}] Processing doc {doc_id}...", end=" ", flush=True)
        try:
            condensed = await vision.summarize_document(full_summary)
            data["full_summary_summarized"] = condensed
            with open(path, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            print("OK")
            updated += 1
        except Exception as e:
            print(f"FAILED ({e})")
            failed += 1

    print(f"\nDone. updated={updated}  skipped={skipped}  failed={failed}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-generate even if full_summary_summarized already exists",
    )
    args = parser.parse_args()
    asyncio.run(main(force=args.force))
