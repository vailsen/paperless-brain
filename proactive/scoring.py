"""Priority, the push/hold decision, and the advice validator — all code
(architecture §8, §10). The model proposes topic, urgency and content; it
never sets a priority, a weight or whether something is advice."""

from __future__ import annotations

from proactive.analysis import quote_in_text

URGENCY_SCORE = {"low": 0.25, "medium": 0.5, "high": 0.75, "critical": 1.0}


def priority(urgency: str, weight: float) -> float:
    return round(URGENCY_SCORE.get(urgency, 0.5) * weight, 3)


def decide(user: str, f, interests) -> tuple[str, float]:
    """`("push" | "hold", priority)` for a new insight."""
    from config.proactive import proactive_settings

    cfg = proactive_settings()
    pri = priority(f.urgency, interests.weight(f.topic))
    if f.topic in interests.muted and f.urgency != "critical":
        return "hold", pri
    if pri >= cfg.proactive_push_threshold:
        return "push", pri
    return "hold", pri


def validate_advice(
    recommendation: str,
    evidence: list[dict],
    sources: list,
    *,
    doc_texts: dict[int, str],
    fetched: dict[str, str],
) -> tuple[str, list[dict], list[dict]]:
    """Keep what is grounded. Returns `(recommendation, evidence, sources)`:
    evidence = quotes that occur in a referenced document's text; sources =
    URLs fetched in this run (with their retrieval date). A recommendation
    without at least one of each is dropped — the finding stays as facts."""
    ev = []
    for e in evidence or []:
        try:
            doc_id = int(e.get("doc_id"))
        except (TypeError, ValueError, AttributeError):
            continue
        quote = " ".join(str(e.get("quote") or "").split())
        if doc_id in doc_texts and quote_in_text(quote, doc_texts[doc_id]):
            ev.append({"doc_id": doc_id, "quote": quote})
    src = []
    for s in sources or []:
        url = (s.get("url") if isinstance(s, dict) else str(s or "")).strip()
        if url in fetched and url not in [x["url"] for x in src]:
            src.append({"url": url, "retrieved": fetched[url]})
    rec = (recommendation or "").strip()
    if not (ev and src):
        rec = ""
    return rec, ev, src
