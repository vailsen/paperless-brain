"""Deadlines (architecture §9): from a document's analysis to a user's deadline
findings, and from those to reminders.

- Only `actionable` deadlines with `date >= today` become findings. Past dates
  never produce a reminder — the class of bug behind "218 overdue".
- One finding per (sender, date, kind) per user, upserted by `finding_key`
  (per document when Paperless knows no sender): every yearly pension letter
  repeats the same latest retirement date, and ten letters were ten findings
  of one date. A further document with the same deadline joins the finding
  (`doc_ids`, quotes) and sends nothing — the date is already known.
  Re-analysing a document (a lost DB, a version bump) finds the same finding
  and writes nothing when the evidence is unchanged.
- The title says what to do (`title_for`, the analysis' description), never
  "<kind>: <file name> (<date>)"; the date is shown next to it anyway.
- Reminders at the user's offsets (default 30/7/1 days). Which offsets were
  sent lives in the finding's `reminders_sent`, so a restart or a deleted
  `proactive.db` never sends one twice. A worker that was down past several
  offsets sends one reminder, not a burst.
- Urgency by code: ≤ 1 day or a legally binding window (cancellation,
  objection) is `critical`; critical reminders are never held back (Phase 7).
- The reminder run needs no LLM: it reads the ledger, so reminders go out
  while the inference host sleeps.
"""

from __future__ import annotations

import logging
from datetime import date

from proactive import analysis, clock, findings
from proactive.analysis import BINDING_KINDS, Analysis, Deadline

log = logging.getLogger("proactive.deadlines")

def _kind_label(kind: str, lang: str) -> str:
    from i18n import N_, translator_for

    labels = {
        "cancellation_window": N_("Cancellation deadline"),
        "payment": N_("Payment due"),
        "objection": N_("Objection deadline"),
        "application": N_("Application deadline"),
        "renewal": N_("Renewal"),
        "informational": N_("Date"),
    }
    return translator_for(lang)(labels.get(kind, labels["informational"]))


#: A title is read in a list next to its date; the date is not repeated.
TITLE_MAX = 90


def title_for(d: Deadline, lang: str, doc_title: str = "") -> str:
    """What has to be done — the analysis' one-line description ("Einspruch
    gegen den Steuerbescheid einlegen"), not "Widerspruchsfrist:
    2026_10_22_Steuerbescheid_…": a file name says which paper, not what to
    do. Only without a description the kind and the document title."""
    text = " ".join((d.description or "").split()).strip()
    text = text.split(". ")[0].rstrip(" .;:")
    if len(text) > TITLE_MAX:
        cut = text[:TITLE_MAX].rsplit(" ", 1)[0]
        text = (cut or text[:TITLE_MAX]).rstrip(" ,;:-") + " …"
    if text:
        return text[0].upper() + text[1:]
    label = _kind_label(d.kind, lang)
    return f"{label}: {doc_title}" if doc_title else label


def urgency(d: Deadline, today: date) -> str:
    days = (d.day - today).days
    if days <= 1 or d.kind in BINDING_KINDS and days <= 7:
        return "critical"
    if d.kind in BINDING_KINDS or days <= 7:
        return "high"
    return "medium" if days <= 30 else "low"


def is_critical(f: findings.Finding, today: date) -> bool:
    dd = f.deadline_date
    if dd is None:
        return False
    return (dd - today).days <= 1 or (f.deadline_kind in BINDING_KINDS)


def finding_key(doc_id: int, d: Deadline, correspondent: int | None = None) -> str:
    """One deadline per (sender, date, kind); without a sender, per document."""
    if correspondent:
        return f"deadline:c{int(correspondent)}:{d.date}:{d.kind}"
    return f"deadline:{int(doc_id)}:{d.date}:{d.kind}"


async def _join(user: str, existing: findings.Finding, new: findings.Finding) -> tuple[findings.Finding, str]:
    """Add `new`'s document to the finding of the same deadline. Status,
    reminders and title stay: the date is the same, the matter is known."""
    doc_ids = sorted(set(existing.doc_ids) | set(new.doc_ids))
    original = findings.split_timeline(existing.body, existing.lang)[1]
    ev = findings.parse_evidence(original) + findings.parse_evidence(new.body)
    from i18n import translator_for

    _ = translator_for(existing.lang)
    seen_prefix = _("Seen in {n} documents: {docs}").split("{n}")[0]
    summary = "\n\n".join(
        p for p in findings._section(original, _("Summary")).split("\n\n") if not p.startswith(seen_prefix)
    ) or findings._section(new.body, _("Summary"))
    body = findings.series_body(existing.lang, summary=summary, doc_ids=doc_ids, evidence=ev)
    fields = {"doc_ids": doc_ids, "evidence_hash": findings.evidence_hash(doc_ids, [existing.finding_key])}
    done = await findings.update(
        user, existing.finding_id, lambda _cur: dict(fields),
        body=lambda cur: findings.join_timeline(findings.split_timeline(cur.body, cur.lang)[0], body))
    return done or existing, "merged"


async def upsert(user: str, f: findings.Finding) -> tuple[findings.Finding, str]:
    """`findings.upsert` for deadlines: a further document of the same deadline
    joins it; a document already part of a merged finding changes nothing (its
    own evidence hash would overwrite the others' documents)."""
    existing = findings.find_by_key(user, f.finding_key)
    if existing is None and f.correspondent and f.doc_ids and f.deadline:
        # written before deadlines were keyed by sender: adopt it, never a twin
        legacy = findings.find_by_key(user, f"deadline:{int(f.doc_ids[0])}:{f.deadline}:{f.deadline_kind}")
        if legacy is not None:
            existing = await findings.update(user, legacy.finding_id, lambda _x: {
                "finding_key": f.finding_key, "correspondent": f.correspondent}) or legacy
    if existing is not None:
        if not set(f.doc_ids) <= set(existing.doc_ids):
            return await _join(user, existing, f)
        if len(existing.doc_ids) > 1:
            return existing, "unchanged"
    return await findings.upsert(user, f)


async def findings_for(user: str, a: Analysis, *, doc_title: str, lang: str,
                       correspondent: int | None = None) -> list[str]:
    """Create/refresh this user's deadline findings for one analysed document.
    The caller has checked the user's access. Returns the outcomes."""
    from proactive import dossiers
    from proactive.interests import slugify

    today = clock.local_now().date()
    outcomes = []
    # A merged-in or renamed matter files under its dossier, like triage.
    topic = dossiers.resolve_topic(user, slugify(a.topic_hint))
    topic_label = findings.topic_label_for(a.topic_hint, topic) if topic == slugify(a.topic_hint) else (
        next((d.display for d in dossiers.list_dossiers(user) if d.topic == topic), ""))
    for d in a.deadlines:
        if not d.actionable or d.day < today:
            continue
        f = findings.Finding(
            finding_id="",
            kind="deadline",
            title=title_for(d, lang, doc_title),
            topic=topic,
            topic_label=topic_label,
            urgency=urgency(d, today),
            doc_ids=[a.doc_id],
            evidence_hash=findings.evidence_hash([a.doc_id], [d.date, d.kind, d.quote]),
            analysis_version=a.version,
            deadline=d.date,
            deadline_kind=d.kind,
            finding_key=finding_key(a.doc_id, d, correspondent),
            correspondent=correspondent,
            lang=lang,
            body=findings.render_body(
                lang,
                summary="\n\n".join(p for p in (d.description, a.summary) if p),
                evidence=[{"doc_id": a.doc_id, "quote": d.quote}],
            ),
        )
        _f, outcome = await upsert(user, f)
        outcomes.append(outcome)
    return outcomes


def due_offsets(f: findings.Finding, offsets: list[int], today: date) -> list[int]:
    """Offsets that have come due and were not sent yet."""
    dd = f.deadline_date
    if dd is None or dd < today:
        return []
    left = (dd - today).days
    return [o for o in offsets if left <= o and o not in f.reminders_sent]


async def remind(user: str, offsets: list[int]) -> int:
    """Send the reminders that are due for one user. Returns how many."""
    from proactive import reactions

    today = clock.local_now().date()
    sent = 0
    for f in findings.list_findings(user):
        if f.kind != "deadline" or f.status not in ("new", "surfaced", "held", "snoozed", "relevant"):
            continue
        due = due_offsets(f, offsets, today)
        if not due:
            continue
        if f.status == "snoozed" and (f.snoozed_until or "") > today.isoformat():
            continue
        crit = is_critical(f, today)
        left = (f.deadline_date - today).days

        def mark(x: findings.Finding, due=due) -> dict | None:
            missing = [o for o in due if o not in x.reminders_sent]
            if not missing:
                return None  # another run got here first
            return {
                "reminders_sent": sorted(set(x.reminders_sent) | set(missing), reverse=True),
                "urgency": "critical" if crit else x.urgency,
            }

        updated = await findings.update(user, f.finding_id, mark)
        if updated is None or sorted(updated.reminders_sent) == sorted(f.reminders_sent):
            continue
        from i18n import translator_for

        _ = translator_for(f.lang)
        text = _("{n} days left.").format(n=left) if left > 1 else (
            _("Due tomorrow.") if left == 1 else _("Due today.")
        )
        if f.status in ("new", "held"):
            await reactions.surface(user, f.finding_id, counts=not crit, note=text)
        else:
            await _resend(user, updated, text, counts=not crit)
        sent += 1
        log.info("%s: reminder for finding %s (%d days left, offsets %s)", user, f.finding_id, left, due)
    return sent


async def _resend(user: str, f: findings.Finding, text: str, *, counts: bool) -> None:
    from proactive import notify
    from proactive.notify import DEADLINE_ACTIONS

    if f.status in ("snoozed", "relevant"):
        # A reminder puts a finding the user set aside back in the inbox.
        await findings.update(user, f.finding_id, lambda x: (
            {"status": "surfaced", "snoozed_until": None} if x.status in ("snoozed", "relevant") else None))
    notify.enqueue(notify.compose(
        user, f.lang, finding_id=f.finding_id, title=f.title, urgency=f.urgency,
        note=text, actions=DEADLINE_ACTIONS, counts=counts,
    ))


# ── Dashboard ────────────────────────────────────────────────────────────────


def dashboard_actions(actions: list[dict], user: str) -> list[dict]:
    """The dashboard's deadline list with `doc_analysis` as the source.

    For an analysed document its sidecar actions are replaced — never both for
    the same document. Upcoming = actionable and not past. Overdue = past, but
    only where this user has an open deadline finding: a date we reminded them
    of and they have not closed. Everything else in the past is history, not
    "overdue". Documents not analysed yet keep their sidecar actions until the
    sweep reaches them. The caller still filters by the user's access.
    """
    from config.proactive import is_enabled

    if not is_enabled():
        return actions
    try:
        analysed = {a.doc_id: a for a in analysis.all_current()}
    except Exception as exc:
        log.warning("dashboard: doc_analysis unreadable, using sidecar actions: %s", exc)
        return actions
    if not analysed:
        return actions
    today = clock.local_now().date()
    # (doc, date, kind) of the user's open deadline findings — a merged
    # finding stands for each of its documents.
    open_keys = set()
    try:
        for f in findings.list_findings(user) if user else []:
            if f.kind == "deadline" and f.status in ("new", "surfaced", "held", "snoozed"):
                open_keys |= {(int(d), f.deadline, f.deadline_kind) for d in f.doc_ids}
    except Exception:
        pass
    out = [a for a in actions if not a.get("paperless_id") or a.get("paperless_id") not in analysed]
    for a in analysed.values():
        for d in a.deadlines:
            if not d.actionable:
                continue
            if d.day < today and (a.doc_id, d.date, d.kind) not in open_keys:
                continue
            out.append({
                "paperless_id": a.doc_id,
                "deadline": d.date,
                "description": d.description or d.quote,
                "deadline_certain": True,
                "kind": d.kind,
            })
    return out
