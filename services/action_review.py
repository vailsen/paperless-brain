# services/action_review.py
"""LLM review of extracted document actions/deadlines.

The vision model over-extracts: tautological validity notes ("gültig bis zum
Ablauf der Gültigkeitsfrist"), informational dates without any required action,
and the same real-world deadline reported by several documents of one contract
(e.g. two letters both naming the latest possible pension start). The
deterministic per-document dedupe (services/action_dedupe.py) cannot judge
importance or cross-document paraphrases, so after each sync the aggregated
action list is reviewed once by an LLM.

Verdicts persist in {EXTRACTION_SIDECAR_PATH}/action_review.json, keyed by
(paperless_id, deadline, description-hash). A re-ingested document that yields
identical actions keeps its verdicts — only genuinely new actions hit the LLM.
Sidecar files are never modified (they are extraction ground truth);
SidecarService.create_index_file filters dropped actions when building
index.json, which is what the dashboard and the get_actions chat tool read.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Awaitable, Callable

_THINK_RE = re.compile(
    r"<think(?:ing)?>(.*?)</think(?:ing)?>", re.DOTALL | re.IGNORECASE
)
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

REVIEW_FILENAME = "action_review.json"

# Batch size for one LLM call. Actions are sorted by deadline first, so
# cross-document duplicates of the same date land in the same batch.
_BATCH_SIZE = 60

SYSTEM_PROMPT = "You are a deadline curator for a private document archive. You receive\nactions/deadlines that a vision model extracted from scanned documents.\nDecide which entries are DROPPED — all others remain.\n\nDROP:\n1. Empty or tautological entries, e.g. \"The document is valid until the\n   end of its validity period.\"\n2. Purely informational dates without any action required from the\n   recipient: issue dates, contract terms, billing periods, expired\n   offer deadlines (\"offer valid until\").\n3. General obligations without a concrete date: \"keep on file\",\n   \"report immediately\", conditional clauses like \"within X weeks of\n   the event\" without a concrete date.\n4. Duplicates of the same real deadline — including across DIFFERENT\n   documents and rephrased (same date + same matter). Keep only the most\n   informative entry, drop all others. Entries under \"Already reviewed\n   and kept\" count as present: drop new entries describing the same\n   deadline.\n\nKEEP:\n- Payment, objection, appeal and cancellation deadlines with a concrete date\n- Appointments (vehicle inspection, doctor, handover, return obligations)\n- Long-term contract options with a concrete date (e.g. earliest/latest\n  pension start) — but only ONE entry per deadline\n- When in doubt, keep.\n\nRespond ONLY with JSON, no explanations before or after:\n{\"drop\": [{\"id\": 3, \"reason\": \"short reason\"}]}\nIf nothing should be dropped: {\"drop\": []}"


def action_key(paperless_id, action: dict) -> str:
    """Stable identity of one extracted action: doc + date + description hash."""
    desc = re.sub(r"\s+", " ", str(action.get("description", ""))).strip().lower()
    digest = hashlib.sha1(desc.encode("utf-8")).hexdigest()[:12]
    return f"{paperless_id}:{action.get('deadline') or '-'}:{digest}"


def _review_path(extr_path: str) -> str:
    return os.path.join(extr_path, REVIEW_FILENAME)


def load_verdicts(extr_path: str) -> dict[str, dict]:
    try:
        with open(_review_path(extr_path)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_verdicts(extr_path: str, verdicts: dict[str, dict]) -> None:
    tmp = _review_path(extr_path) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(verdicts, f, indent=2, ensure_ascii=False)
    os.replace(tmp, _review_path(extr_path))


def collect_actions(extr_path: str) -> list[dict]:
    """All actions from all sidecars, per-document deduped, with paperless_id."""
    from services.action_dedupe import dedupe_actions

    out: list[dict] = []
    for filename in os.listdir(extr_path):
        if filename == "index.json" or not filename.endswith(".json"):
            continue
        try:
            with open(os.path.join(extr_path, filename)) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        paperless_id = data.get("paperless_id")
        if paperless_id is None:
            continue
        for action in dedupe_actions(data.get("actions", [])):
            out.append({"paperless_id": paperless_id, **action})
    return out


def filter_actions(extr_path: str, actions: list[dict]) -> list[dict]:
    """Drop actions whose persisted verdict is 'drop' (entries need paperless_id)."""
    verdicts = load_verdicts(extr_path)
    if not verdicts:
        return actions
    return [
        a
        for a in actions
        if verdicts.get(action_key(a.get("paperless_id"), a), {}).get("verdict")
        != "drop"
    ]


def _fmt_action(idx: int, action: dict) -> str:
    date = action.get("deadline") or "no date"
    certain = "" if action.get("deadline_certain", True) else " (date uncertain)"
    return (
        f"[{idx}] Document #{action.get('paperless_id')} · {date}{certain}\n"
        f"    {str(action.get('description', '')).strip()}"
    )


def _parse_drop_ids(raw: str, valid_range: int) -> dict[int, str]:
    cleaned = _THINK_RE.sub("", raw).strip()
    m = _JSON_RE.search(cleaned)
    if not m:
        raise ValueError(f"No JSON found in LLM response: {raw[:400]}")
    data = json.loads(m.group())
    drops: dict[int, str] = {}
    for item in data.get("drop", []):
        try:
            idx = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < valid_range:
            drops[idx] = str(item.get("reason", "")).strip()
    return drops


async def review_actions(
    extr_path: str,
    actions: list[dict],
    llm: Callable[..., Awaitable[str]],
) -> tuple[int, int]:
    """Review actions without a persisted verdict; returns (reviewed, dropped).

    Verdicts of batches that completed before an LLM failure are kept; the
    remaining actions simply stay unreviewed and are retried on the next sync.
    Stale verdicts (deleted docs, re-extracted wording) are pruned.
    """
    verdicts = load_verdicts(extr_path)
    keyed = [(action_key(a.get("paperless_id"), a), a) for a in actions]
    current_keys = {k for k, _ in keyed}
    verdicts = {k: v for k, v in verdicts.items() if k in current_keys}

    new = [(k, a) for k, a in keyed if k not in verdicts]
    if not new:
        save_verdicts(extr_path, verdicts)  # persist pruning
        return 0, 0

    # Same-date entries adjacent → cross-document duplicates land in one batch.
    new.sort(key=lambda t: (t[1].get("deadline") or "9999-99-99", t[0]))

    reviewed = dropped = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        for start in range(0, len(new), _BATCH_SIZE):
            batch = new[start : start + _BATCH_SIZE]
            batch_dates = {a.get("deadline") for _, a in batch if a.get("deadline")}
            kept_context = [
                a
                for k, a in keyed
                if verdicts.get(k, {}).get("verdict") == "keep"
                and a.get("deadline") in batch_dates
            ]

            parts = []
            if kept_context:
                parts.append(
                    "Already reviewed and kept (do NOT evaluate, context for duplicate detection only):\n"
                    + "\n".join(
                        f"- Document #{a.get('paperless_id')} · "
                        f"{a.get('deadline')} · {str(a.get('description', '')).strip()}"
                        for a in kept_context
                    )
                )
            parts.append(
                f"Entries to review ({len(batch)}):\n\n"
                + "\n".join(_fmt_action(i, a) for i, (_, a) in enumerate(batch))
                + "\n\nRespond with JSON. Use the numbers in square brackets as id (e.g. 0, 3)."
            )

            raw = await llm(
                SYSTEM_PROMPT,
                [{"role": "user", "content": "\n\n".join(parts)}],
                max_tokens=16_000,
                temperature=0.1,
                think=False,  # structured JSON, no CoT
            )
            drops = _parse_drop_ids(raw, len(batch))

            for i, (key, _a) in enumerate(batch):
                if i in drops:
                    verdicts[key] = {
                        "verdict": "drop",
                        "reason": drops[i],
                        "reviewed_at": now,
                    }
                    dropped += 1
                else:
                    verdicts[key] = {"verdict": "keep", "reviewed_at": now}
                reviewed += 1
    finally:
        save_verdicts(extr_path, verdicts)

    return reviewed, dropped
