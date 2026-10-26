"""`doc_analysis` — what a document says, once per document and analysis version
(architecture §4.1, §9). User-independent: it describes the document, not
anyone's interest in it. Everything per user (deadline findings, triage) is
derived afterwards, after that user's own access check.

A text-only pass over the sidecar text the ingestion already extracted — no new
vision run (decision 4, `docs/proactive-diagnostics.md`).

The model proposes; code decides:
- the output is schema-checked; a malformed deadline is dropped, not repaired;
- every deadline's `quote` must occur in the document text (whitespace- and
  Unicode-normalized). A deadline whose quote cannot be found was not read, it
  was written — and is discarded;
- dates are parsed by code; `actionable` and `kind` are the model's reading,
  but only `actionable and date >= today` ever becomes a reminder (§9).
"""

from __future__ import annotations

import hashlib
import json
import logging
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from proactive import clock
from proactive.db import connect

log = logging.getLogger("proactive.analysis")

KINDS = ("cancellation_window", "payment", "objection", "application", "renewal", "informational")
#: Legally binding windows: missing one costs a right, not just a fee.
BINDING_KINDS = {"cancellation_window", "objection"}

#: The model sees at most this much text. Long documents are mostly appendices
#: (terms, privacy notices); the deadlines are near the start.
MAX_CHARS = 30_000

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "topic_hint": {"type": "string"},
        "deadlines": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "actionable": {"type": "boolean"},
                    "description": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["date", "kind", "actionable", "description", "quote"],
            },
        },
    },
    "required": ["summary", "topic_hint", "deadlines"],
}

SYSTEM = """\
You read the text of one document from a personal document archive and report \
what it says. Answer only through the tool.

summary: 2–3 sentences: what the document is, who it is from, what it is about.
topic_hint: the subject area in 1–3 words (e.g. "car insurance", "electricity \
contract", "income tax"). Not the sender, not a date.
deadlines: every date in the document by which the RECIPIENT of the document \
must or can do something, or until which a right of theirs exists:
- kind: cancellation_window (last day to cancel / give notice), payment (pay by), \
objection (appeal / object / complain by), application (apply / submit / register \
by), renewal (contract renews or ends unless acted on), informational (a date \
that requires nothing of the recipient: issue date, delivery date, service \
period, a date in the past history of the matter).
- actionable: true only if the recipient has to or can act by that date. A \
payment that is collected by direct debit is not actionable. A date that \
already lies before the document's own date is not actionable.
- date: YYYY-MM-DD. If the document gives a period ("within 4 weeks of \
receipt"), compute it from the document date and say so in the description. If \
the year is missing, take it from the document's context.
- quote: copy the sentence or phrase that states the deadline EXACTLY as it \
appears in the text, character for character, at most 200 characters. Do not \
rephrase, translate or complete it. A deadline you cannot quote, leave out.
- description: one short line saying what has to be done, in {language}.
No deadline in the document → an empty list. Never invent one.
Write summary and topic_hint in {language}.\
"""


@dataclass
class Deadline:
    date: str
    kind: str
    actionable: bool
    description: str
    quote: str

    @property
    def day(self) -> date:
        return date.fromisoformat(self.date)


@dataclass
class Analysis:
    doc_id: int
    version: int
    summary: str
    topic_hint: str
    deadlines: list[Deadline] = field(default_factory=list)
    content_hash: str = ""
    model: str = ""


# ── Text ─────────────────────────────────────────────────────────────────────


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    t = t.replace("­", "")  # soft hyphen
    for a, b in (("“", '"'), ("”", '"'), ("„", '"'), ("‘", "'"), ("’", "'"), ("–", "-"), ("—", "-")):
        t = t.replace(a, b)
    return " ".join(t.split())


def quote_in_text(quote: str, text: str) -> bool:
    q = _norm(quote).strip(" .…\"'")
    return len(q) >= 8 and q in _norm(text)


def sidecar_text(doc_id: int) -> str | None:
    """The document's extracted text from its sidecar, page by page."""
    from config.settings import settings

    path = Path(settings.app_path) / settings.extraction_sidecar_path / f"{int(doc_id)}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pages = [p.get("page_text") or "" for p in data.get("pages") or []]
    text = "\n\n".join(p for p in pages if p.strip()) or data.get("full_text") or ""
    return text or None


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def _archive_language() -> str:
    from i18n import language_name

    try:
        from werkbank.settings_store import get_archive_language

        return language_name(get_archive_language())
    except Exception:
        from config.settings import settings

        return language_name(settings.archive_language)


# ── Validation ───────────────────────────────────────────────────────────────


def validate(raw: dict, text: str) -> tuple[str, str, list[Deadline]]:
    """Schema + quote check by code. Returns (summary, topic_hint, deadlines)."""
    summary = str(raw.get("summary") or "").strip()[:2000]
    topic = str(raw.get("topic_hint") or "").strip()[:80]
    out: list[Deadline] = []
    for d in raw.get("deadlines") or []:
        if not isinstance(d, dict):
            continue
        try:
            day = date.fromisoformat(str(d.get("date", "")).strip()[:10])
        except ValueError:
            log.info("analysis: deadline with unparsable date %r dropped", d.get("date"))
            continue
        kind = str(d.get("kind") or "").strip()
        if kind not in KINDS:
            log.info("analysis: deadline with unknown kind %r dropped", kind)
            continue
        quote = str(d.get("quote") or "").strip()
        if not quote_in_text(quote, text):
            log.info("analysis: deadline %s dropped — quote not found in the document", day)
            continue
        out.append(Deadline(
            date=day.isoformat(),
            kind=kind,
            actionable=bool(d.get("actionable")) and kind != "informational",
            description=str(d.get("description") or "").strip()[:200],
            quote=" ".join(quote.split())[:300],
        ))
    return summary, topic, out


# ── Cache ────────────────────────────────────────────────────────────────────


def version() -> int:
    from config.proactive import proactive_settings

    return proactive_settings().proactive_analysis_version


def get(doc_id: int, ver: int | None = None) -> Analysis | None:
    ver = version() if ver is None else ver
    with connect() as conn:
        r = conn.execute(
            "SELECT * FROM doc_analysis WHERE doc_id = ? AND analysis_version = ?",
            (int(doc_id), ver),
        ).fetchone()
    if r is None:
        return None
    return Analysis(
        doc_id=r["doc_id"],
        version=r["analysis_version"],
        summary=r["summary"],
        topic_hint=r["topic_hint"],
        deadlines=[Deadline(**d) for d in json.loads(r["deadlines_json"] or "[]")],
        content_hash=r["content_hash"],
        model=r["model"],
    )


def save(a: Analysis) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO doc_analysis (doc_id, analysis_version, summary, deadlines_json,"
            " topic_hint, content_hash, model, created_at) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(doc_id, analysis_version) DO UPDATE SET summary = excluded.summary,"
            " deadlines_json = excluded.deadlines_json, topic_hint = excluded.topic_hint,"
            " content_hash = excluded.content_hash, model = excluded.model,"
            " created_at = excluded.created_at",
            (
                a.doc_id, a.version, a.summary,
                json.dumps([d.__dict__ for d in a.deadlines], ensure_ascii=False),
                a.topic_hint, a.content_hash, a.model, clock.now_iso(),
            ),
        )


def all_current() -> list[Analysis]:
    with connect() as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT doc_id FROM doc_analysis WHERE analysis_version = ?", (version(),)
        )]
    return [a for a in (get(i) for i in ids) if a is not None]


# ── Running ──────────────────────────────────────────────────────────────────


class NoText(Exception):
    """The document has no sidecar (yet): nothing to analyse."""


async def ensure(doc_id: int, model, user: str, token: str, *, doc_date: str = "") -> tuple[Analysis, bool]:
    """The current analysis of `doc_id`, computed if missing or if the text
    changed (re-ingest). Returns `(analysis, used_llm)`."""
    from proactive import llm

    text = sidecar_text(doc_id)
    if text is None:
        cached = get(doc_id)
        if cached is not None:
            return cached, False
        raise NoText(f"document {doc_id} has no extracted text yet")
    text = text[:MAX_CHARS]
    h = content_hash(text)
    cached = get(doc_id)
    if cached is not None and cached.content_hash == h:
        return cached, False

    llm.check_cloud_cap(user, model)
    lang = _archive_language()
    today = clock.local_now().date().isoformat()
    header = f"Today: {today}\nDocument date: {doc_date or 'unknown'}\n\n"
    raw = await llm.structured(
        model, user, token,
        system=SYSTEM.format(language=lang),
        messages=[{"role": "user", "content": header + text}],
        json_schema=SCHEMA,
        tool_name="doc_analysis",
    )
    llm.record_cloud_doc(user, model)
    summary, topic, deadlines = validate(raw if isinstance(raw, dict) else {}, text)
    a = Analysis(doc_id=int(doc_id), version=version(), summary=summary, topic_hint=topic,
                 deadlines=deadlines, content_hash=h, model=model.key)
    save(a)
    log.info("doc %s analysed with %s: %d deadline(s)", doc_id, model.key, len(deadlines))
    return a, True
