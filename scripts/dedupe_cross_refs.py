"""One-shot script: deduplicate cross_refs in existing sidecar JSON files.

Run from the project root:
    python scripts/dedupe_cross_refs.py
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import settings

# Trivial references: lone § / $ sign followed by only a number (+ optional single letter).
# Examples: "§1", "§ 3", "$ 1", "§1a"  →  dropped.
# Kept: "§ 31 Abs. 2", "§ 242 StGB", "4 32 Absatz 4 Satz 1"
_TRIVIAL_RE = re.compile(r"^[§$]\s*\d+[a-zA-Z]?\s*$")


def _is_trivial(ref: dict) -> bool:
    return bool(_TRIVIAL_RE.match(ref.get("value", "").strip()))


def _is_specific_value(val: str) -> bool:
    """True when value contains at least one non-digit non-space char AND at least one digit.

    Such values are specific enough to deduplicate across different 'type' labels.
    Pure numbers (e.g. year "2024") are NOT specific — same value can legitimately
    appear under different types and must not be collapsed.
    """
    has_digit = any(c.isdigit() for c in val)
    has_other = any(not c.isdigit() and not c.isspace() for c in val)
    return has_digit and has_other


sidecar_dir = str(settings.app_path / settings.extraction_sidecar_path)
changed = 0
skipped = 0

for filename in sorted(os.listdir(sidecar_dir)):
    if filename == "index.json" or not filename.endswith(".json"):
        continue
    path = os.path.join(sidecar_dir, filename)
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"  [SKIP] {filename}: {e}")
        skipped += 1
        continue

    refs = data.get("cross_refs") or []
    seen_pair: set = set()   # (type, value) — for non-specific values
    seen_val: set = set()    # value alone   — for specific values
    unique: list = []

    for ref in refs:
        # Drop trivial bare-paragraph references
        if _is_trivial(ref):
            continue

        val = ref.get("value", "")
        typ = ref.get("type", "")

        if _is_specific_value(val):
            if val in seen_val:
                continue
            seen_val.add(val)
        else:
            key = (typ, val)
            if key in seen_pair:
                continue
            seen_pair.add(key)

        unique.append(ref)

    if len(unique) == len(refs):
        continue

    data["cross_refs"] = unique
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  {filename}: {len(refs)} → {len(unique)} refs")
    changed += 1

print(f"\nDone. {changed} sidecar(s) updated, {skipped} skipped.")
