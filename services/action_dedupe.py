# services/action_dedupe.py
"""Deduplication of extracted document actions/deadlines.

The vision model extracts page by page and frequently reports the same
real-world deadline several times with different wording ("Überweisung des
1. Zahlungsbetrags" vs "Überweisung des ersten Zahlungsbetrags"), with or
without a time part, or with a literal "null" deadline. An exact-match key
cannot catch paraphrases, so dated actions are collapsed to one entry per
due date within a document — the row links to the document anyway, detail
lives there.

Used by pipelines/ingest.py (new extractions) and
services/sidecar_service.create_index_file (index rebuild over old sidecars).
"""

from __future__ import annotations

_NULLISH = {"", "null", "none", "nil", "-", "n/a", "unbekannt"}


def normalize_deadline(value) -> str:
    """Return YYYY-MM-DD, or "" for missing/nullish values.

    Strips time parts ("2022-02-23T12:00:00" → "2022-02-23"). Non-ISO strings
    are returned as-is (trimmed) so unexpected formats stay visible.
    """
    s = str(value or "").strip()
    if s.lower() in _NULLISH:
        return ""
    if len(s) >= 10 and s[4:5] == "-" and s[7:8] == "-":
        return s[:10]
    return s


def dedupe_actions(actions: list[dict]) -> list[dict]:
    """Collapse duplicate actions within ONE document.

    - Dated actions: one entry per normalized due date; the variant with the
      longest description wins (most informative).
    - Undated actions: deduped by lowercased description only.
    - Normalized date is written back to 'deadline' (None if nullish).

    Order of first occurrence is preserved.
    """
    result: list[dict] = []
    by_date: dict[str, dict] = {}
    seen_undated: set[str] = set()

    for action in actions:
        a = dict(action)
        date = normalize_deadline(a.get("deadline"))
        a["deadline"] = date or None

        if date:
            kept = by_date.get(date)
            if kept is None:
                by_date[date] = a
                result.append(a)
            elif len(str(a.get("description", ""))) > len(str(kept.get("description", ""))):
                kept["description"] = a.get("description", "")
        else:
            key = str(a.get("description", "")).strip().lower()
            if not key or key in seen_undated:
                continue
            seen_undated.add(key)
            result.append(a)

    return result
