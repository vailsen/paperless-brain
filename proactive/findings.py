"""The findings ledger: one Markdown file per finding in the user's vault
(architecture §4.3). The files are the source of truth for status and
reactions — `proactive.db` holds none of it, so notification dedupe
(`surfaced_at`, `reminders_sent`) survives losing the DB.

Writes go through `vault.notes.save_note`: the cross-process vault lock, a
hash check against what was read, and a three-way merge per frontmatter key.
A user editing `status` in Obsidian while the worker updates the same file is
therefore merged when they touched different keys, and otherwise re-read and
re-decided — never overwritten. New files are written atomically under the
same lock. The worker never commits; the app's vault sync does.

The findings folder is excluded from the vault index (vault/sync.py) until
Phase 9 decides what of it may be retrieved.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

from proactive import clock
from proactive.paths import NOTICES, findings_root

log = logging.getLogger("proactive.findings")

_FID_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"
FID_LEN = 7
_FID_RE = re.compile(rf"^[{_FID_ALPHABET}]{{{FID_LEN}}}$")

#: Frontmatter keys in the order a new file gets them.
KEYS = (
    "pbrain_id", "finding_id", "finding_key", "kind", "title", "status", "topic",
    "urgency", "priority", "advice", "doc_ids", "evidence_hash", "analysis_version",
    "created", "surfaced_at", "snoozed_until", "deadline", "deadline_kind",
    "reminders_sent", "lang", "correspondent",
)


class LedgerConflict(Exception):
    """The file kept changing under us; the job retries later."""


def new_finding_id() -> str:
    return "".join(secrets.choice(_FID_ALPHABET) for _ in range(FID_LEN))


_RUN_LINK = r"\[([^\]]+)\]\((?:https?://[^)\s]*)?/werkbank\?run={run}\)"


def link_research_note(body: str, run_id: str, note_rel: str) -> str:
    """The "[Full report in deep research](…/werkbank?run=…)" link of an older
    finding → an Obsidian link to the report's note, keeping its text."""
    from vault.research import wikilink

    if not run_id:
        return body
    return re.sub(_RUN_LINK.format(run=re.escape(run_id)), lambda m: wikilink(note_rel, m.group(1)), body)


def strip_research(body: str, lang: str) -> str:
    """The body without an earlier research result on top (`research_body` +
    `---`). A new result replaces the old one; stacked, the finding showed
    yesterday's result — and its dead link — above today's."""
    from i18n import translator_for

    heads = {translator_for(code)("Research result ({date})").split("(")[0].strip()
             for code in ("en", lang or "en")}
    text = body.lstrip()
    first = text.split("\n", 1)[0]
    if not (first.startswith("## ") and any(first[3:].startswith(h) for h in heads if h)):
        return body
    _head, sep, rest = text.partition("\n---\n")
    return rest.lstrip("\n") if sep else body


def valid_finding_id(fid: str) -> bool:
    return bool(_FID_RE.match(fid or ""))


def evidence_hash(doc_ids: list[int], facts: list[str]) -> str:
    """sha256 over the sorted documents and the normalized key facts."""
    norm = sorted(" ".join(str(f).lower().split()) for f in facts if str(f).strip())
    raw = json.dumps({"docs": sorted(int(d) for d in doc_ids), "facts": norm})
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


@dataclass
class Finding:
    finding_id: str
    kind: str  # deadline | insight
    title: str
    status: str = "new"
    topic: str = ""
    urgency: str = "medium"
    priority: float = 0.0
    advice: bool = False
    doc_ids: list[int] = field(default_factory=list)
    evidence_hash: str = ""
    analysis_version: int = 1
    created: str = ""
    surfaced_at: str | None = None
    snoozed_until: str | None = None
    deadline: str | None = None
    deadline_kind: str | None = None
    reminders_sent: list[int] = field(default_factory=list)
    finding_key: str = ""
    lang: str = "en"
    pbrain_id: str = ""
    #: Paperless correspondent id of the documents (insights): with the topic
    #: it is what makes a series — twelve invoices, one price change — one finding.
    correspondent: int | None = None
    #: The Werkbank run of the deep dive ("Interesting"), once it was started.
    research_run: str | None = None
    #: The deep dive's report in the vault, as an Obsidian link
    #: (`[[PaperlessBrain Research/26-10-29 …]]`) — it stays when the run is deleted.
    research_note: str | None = None
    #: The topic as the model wrote it ("Kfz-Versicherung"). `topic` is the key
    #: derived from it (`interests.slugify`) and what everything matches on.
    topic_label: str = ""
    # not in frontmatter
    user: str = ""
    rel: str = ""  # vault-relative path
    body: str = ""
    sha: str = ""

    def meta(self) -> dict:
        d = {
            "pbrain_id": self.pbrain_id,
            "finding_id": self.finding_id,
            "finding_key": self.finding_key,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "topic": self.topic,
            "urgency": self.urgency,
            "priority": round(float(self.priority), 3),
            "advice": bool(self.advice),
            "doc_ids": [int(x) for x in self.doc_ids],
            "evidence_hash": self.evidence_hash,
            "analysis_version": int(self.analysis_version),
            "created": self.created,
            "surfaced_at": self.surfaced_at,
            "snoozed_until": self.snoozed_until,
            "deadline": self.deadline,
            "deadline_kind": self.deadline_kind,
            "reminders_sent": [int(x) for x in self.reminders_sent],
            "lang": self.lang,
        }
        if self.kind != "deadline":
            for k in ("deadline", "deadline_kind", "reminders_sent"):
                d.pop(k)
        if self.correspondent is not None:
            d["correspondent"] = int(self.correspondent)
        if self.research_run:
            d["research_run"] = self.research_run
        if self.research_note:
            d["research_note"] = self.research_note
        if self.topic_label:
            d["topic_label"] = self.topic_label
        return d

    @property
    def topic_display(self) -> str:
        return topic_display(self.topic, self.topic_label, self.lang)

    @property
    def deadline_date(self) -> date | None:
        try:
            return date.fromisoformat(str(self.deadline)) if self.deadline else None
        except ValueError:
            return None


def topic_label_for(label: str, topic: str) -> str:
    """The model's label, if it is the one `topic` was derived from."""
    from proactive.interests import slugify

    label = " ".join((label or "").split())[:60]
    return label if label and slugify(label) == topic else ""


def topic_display(topic: str, label: str = "", lang: str = "en") -> str:
    """How a topic is shown. Findings from before `topic_label` only have the
    key: German capitalises each part ("kfz-versicherung" → "Kfz-Versicherung"),
    English the first letter. Umlauts are not guessed back — "ue" is also the
    "ue" in "Feuer"."""
    if label:
        return label
    if not topic:
        return ""
    if lang == "de":
        return "-".join(p[:1].upper() + p[1:] for p in topic.split("-"))
    return topic[:1].upper() + topic[1:].replace("-", " ")


def _s(v) -> str | None:
    """YAML gives dates/datetimes as objects; the ledger compares strings."""
    if v is None or v == "":
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def parse(text: str, *, user: str = "", rel: str = "") -> Finding | None:
    """A finding from file text, or None if it is not one (no finding_id)."""
    from vault.note_text import properties, split_note
    from vault.notes import sha_of

    note = split_note(text)
    m = properties(note.fm_raw)
    fid = str(m.get("finding_id") or "")
    if not valid_finding_id(fid):
        return None

    def _ints(v) -> list[int]:
        out = []
        for x in v if isinstance(v, list) else []:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                pass
        return out

    from vault.frontmatter import get_id

    try:
        prio = float(m.get("priority") or 0)
    except (TypeError, ValueError):
        prio = 0.0
    return Finding(
        finding_id=fid,
        kind=str(m.get("kind") or "insight"),
        title=str(m.get("title") or Path(rel).stem),
        status=str(m.get("status") or "new").strip().lower(),
        topic=str(m.get("topic") or ""),
        urgency=str(m.get("urgency") or "medium"),
        priority=prio,
        advice=bool(m.get("advice")),
        doc_ids=_ints(m.get("doc_ids")),
        evidence_hash=str(m.get("evidence_hash") or ""),
        analysis_version=int(m.get("analysis_version") or 1),
        created=_s(m.get("created")) or "",
        surfaced_at=_s(m.get("surfaced_at")),
        snoozed_until=_s(m.get("snoozed_until")),
        deadline=_s(m.get("deadline")),
        deadline_kind=_s(m.get("deadline_kind")),
        reminders_sent=_ints(m.get("reminders_sent")),
        finding_key=str(m.get("finding_key") or ""),
        lang=str(m.get("lang") or "en"),
        pbrain_id=get_id(m),
        correspondent=_ints([m.get("correspondent")])[0] if _ints([m.get("correspondent")]) else None,
        research_run=_s(m.get("research_run")),
        research_note=_s(m.get("research_note")),
        topic_label=str(m.get("topic_label") or ""),
        user=user,
        rel=rel,
        body=note.body,
        sha=sha_of(text),
    )


def render(f: Finding) -> str:
    from vault.note_text import dump_mapping

    return f"---\n{dump_mapping(f.meta())}---\n{f.body}"


# ── Body ─────────────────────────────────────────────────────────────────────


def paperless_link(doc_id: int) -> str:
    from config.settings import settings

    return f"{settings.paperless_url.rstrip('/')}/documents/{int(doc_id)}/details"


def render_body(
    lang: str,
    *,
    summary: str,
    evidence: list[dict] | None = None,
    sources: list[dict] | None = None,
    recommendation: str = "",
    open_points: list[str] | None = None,
) -> str:
    """Body sections in the user's language. `evidence` = `[{doc_id, quote}]`,
    `sources` = `[{url, retrieved}]`, `open_points` = what is not known or
    assumed (deep dive), shown before the recommendation it qualifies. The
    disclaimer is added here, by code, whenever there is a recommendation."""
    from i18n import translator_for

    _ = translator_for(lang)
    out = [f"## {_('Summary')}", "", summary.strip(), ""]
    if evidence:
        out += [f"## {_('Evidence')}", ""]
        for e in evidence:
            quote = " ".join(str(e.get("quote", "")).split())
            out.append(f"> {quote}")
            out.append(f"> — [#{e['doc_id']}]({paperless_link(e['doc_id'])})")
            out.append("")
    sources = dedupe_sources(sources or [])
    if sources:
        out += [f"## {_('Sources')}", ""]
        for s in sources:
            out.append(f"- {s['url']} ({s.get('retrieved', '')})")
        out.append("")
    points = [" ".join(str(p).split()) for p in open_points or [] if str(p).strip()]
    if points:
        out += [f"## {_('Open points')}", "", *(f"- {p}" for p in points), ""]
    if recommendation.strip():
        out += [f"## {_('Recommendation')}", "", recommendation.strip(), ""]
        out.append(f"> {disclaimer(lang)}")
        out.append("")
    return "\n".join(out)


#: A finding lists at most this many web sources (the newest run's first).
MAX_SOURCES = 10
# Query parameters that only track the visit: the same page fetched twice
# came back with a new `msockid` each time and listed twenty times.
_TRACKING = re.compile(r"^(utm_.*|msockid|gclid|fbclid|dclid|msclkid|mc_cid|mc_eid|srsltid|_ga|_gl|ref|ref_src)$", re.I)
_SOURCE_LINE = re.compile(r"^- (\S+) \((.*)\)$")


def clean_source_url(url: str) -> str:
    """`url` without tracking parameters and fragment — what a finding stores."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    try:
        u = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    query = urlencode([(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if not _TRACKING.match(k)])
    return urlunsplit((u.scheme, u.netloc, u.path, query, ""))


def is_home_page(url: str) -> bool:
    """A site's front page (`https://www.wikipedia.de/`) backs no particular
    statement — triage cited exactly that as a source."""
    from urllib.parse import urlsplit

    try:
        u = urlsplit(url.strip())
    except ValueError:
        return True
    return u.path.strip("/") == "" and not u.query


def source_key(url: str) -> str:
    """What makes two source URLs the same page: scheme, `www.`, a trailing
    slash, the fragment and tracking parameters do not count."""
    from urllib.parse import parse_qsl, urlencode, urlsplit

    try:
        u = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = (u.hostname or "").lower().removeprefix("www.")
    query = urlencode(sorted((k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
                             if not _TRACKING.match(k)))
    return f"{host}{u.path.rstrip('/')}" + (f"?{query}" if query else "")


def dedupe_sources(sources: list[dict]) -> list[dict]:
    """First occurrence of each page, at most `MAX_SOURCES`."""
    seen, out = set(), []
    for s in sources:
        key = source_key(str(s.get("url", "")))
        if key and key not in seen:
            seen.add(key)
            out.append(s)
    return out[:MAX_SOURCES]


def tidy_sources(body: str, lang: str) -> str:
    """`body` with its Sources list deduplicated — for findings written before
    `dedupe_sources` existed. Display only; the file is left as it is."""
    from i18n import translator_for

    heading = f"## {translator_for(lang)('Sources')}"
    lines = body.splitlines()
    try:
        start = lines.index(heading) + 1
    except ValueError:
        return body
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines))
    entries = [m for m in (_SOURCE_LINE.match(x.strip()) for x in lines[start:end]) if m]
    kept = dedupe_sources([{"url": m.group(1), "retrieved": m.group(2)} for m in entries])
    if len(kept) == len(entries):
        return body
    new = ["", *(f"- {s['url']} ({s['retrieved']})" for s in kept), ""]
    return "\n".join(lines[:start] + new + lines[end:])


def disclaimer(lang: str) -> str:
    from i18n import translator_for

    _ = translator_for(lang)
    return _("Not financial or investment advice. Check the recommendation yourself.")


# ── Reading ──────────────────────────────────────────────────────────────────


def _notices_dir(user: str) -> Path:
    return findings_root(user) / NOTICES


def _rel(user: str, path: Path) -> str:
    from vault.paths import vault_path

    return path.relative_to(vault_path(user)).as_posix()


def _read_file(user: str, path: Path) -> Finding | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse(text, user=user, rel=_rel(user, path))


def list_findings(user: str) -> list[Finding]:
    d = _notices_dir(user)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.md")):
        if p.name.startswith(".") or p.name.endswith(".conflict.md"):
            continue
        f = _read_file(user, p)
        if f is not None:
            out.append(f)
    return out


def get(user: str, finding_id: str) -> Finding | None:
    """A finding in *this user's* ledger only — the access rule of the page."""
    if not valid_finding_id(finding_id):
        return None
    hit = _index.get(finding_id)
    if hit and hit[0] == user:
        f = _load_rel(user, hit[1])
        if f is not None and f.finding_id == finding_id:
            return f
    for f in list_findings(user):
        _index[f.finding_id] = (user, f.rel)
        if f.finding_id == finding_id:
            return f
    return None


def _load_rel(user: str, rel: str) -> Finding | None:
    from vault.paths import vault_path

    return _read_file(user, vault_path(user) / rel)


#: finding_id → (user, rel). A hint only; every hit is re-read and checked.
_index: dict[str, tuple[str, str]] = {}


def locate(finding_id: str) -> Finding | None:
    """Find a finding in any user's ledger (by id alone)."""
    from config.settings import settings

    if not valid_finding_id(finding_id):
        return None
    hit = _index.get(finding_id)
    if hit:
        f = _load_rel(*hit)
        if f is not None and f.finding_id == finding_id:
            return f
    root = Path(settings.vault_root)
    if not root.is_dir():
        return None
    for vault in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        f = get(vault.name, finding_id)
        if f is not None:
            return f
    return None


def find_by_key(user: str, finding_key: str) -> Finding | None:
    for f in list_findings(user):
        if f.finding_key == finding_key:
            return f
    return None


# ── Writing ──────────────────────────────────────────────────────────────────


def _safe_filename(title: str) -> str:
    name = re.sub(r'[\\/:*?"<>|#^\[\]\0\n\r\t]', " ", title)
    name = " ".join(name.split()).strip(". ")[:120] or "Finding"
    return name


async def create(user: str, f: Finding) -> Finding:
    """Write a new finding file. `f.finding_id` and `pbrain_id` are minted here
    when empty."""
    from vault.locks import get_user_lock
    from vault.paths import atomic_write_text, ensure_vault_dir

    f.user = user
    f.finding_id = f.finding_id or new_finding_id()
    f.pbrain_id = f.pbrain_id or str(uuid.uuid4())
    f.created = f.created or clock.local_now().isoformat(timespec="seconds")
    d = _notices_dir(user)
    stem = f"{clock.local_now():%Y-%m-%d} {_safe_filename(f.title)}"

    def _write() -> Path:
        ensure_vault_dir(findings_root(user))
        ensure_vault_dir(d)
        path = d / f"{stem}.md"
        n = 2
        while path.exists():
            path = d / f"{stem} ({n}).md"
            n += 1
        atomic_write_text(path, render(f))
        return path

    async with get_user_lock(user):
        path = await asyncio.to_thread(_write)
    f.rel = _rel(user, path)
    _index[f.finding_id] = (user, f.rel)
    loaded = _load_rel(user, f.rel)
    return loaded or f


Mutation = Callable[[Finding], "dict | None"]


async def update(user: str, finding_id: str, mutate: Mutation, *, body: Callable[[Finding], str] | None = None) -> Finding | None:
    """Apply `mutate(finding) -> {key: value}` to the frontmatter (None = no
    change) and optionally replace the body. Re-read and re-decided once if
    the file changed in between; a second clash raises `LedgerConflict`.
    Returns the finding as written, or None if it is gone."""
    from vault.note_text import apply_edits, join_note, split_note
    from vault.notes import NoteConflict, read_note, save_note

    for attempt in range(2):
        f = get(user, finding_id)
        if f is None:
            return None
        snap = await read_note(user, f.rel)
        f = parse(snap.text, user=user, rel=f.rel)
        if f is None:
            return None
        changes = mutate(f)
        new_body = body(f) if body else None
        if not changes and new_body is None:
            return f
        note = split_note(snap.text)
        fm_raw = apply_edits(note.fm_raw, set_=changes or {})
        new_note = type(note)(
            fm_raw=fm_raw,
            body=note.body if new_body is None else new_body,
            open_fence=note.open_fence,
            close_fence=note.close_fence,
        )
        text = join_note(new_note)
        try:
            saved = await save_note(user, f.rel, text, expected_sha=snap.sha, base_text=snap.text)
        except NoteConflict:
            log.info("%s: finding %s changed while updating — re-reading (attempt %d)", user, finding_id, attempt + 1)
            continue
        return parse(saved.text, user=user, rel=f.rel)
    raise LedgerConflict(f"{user}: finding {finding_id} kept changing")


async def upsert(user: str, f: Finding) -> tuple[Finding, str]:
    """Create or refresh the finding for `f.finding_key`.

    Returns `(finding, outcome)`, outcome one of:
    - `created`   — no finding with this key yet
    - `unchanged` — same evidence: nothing written (a dismissed finding stays
                    dismissed — it is never re-surfaced on the same evidence)
    - `updated`   — open finding, new evidence: content refreshed, status kept
    - `reopened`  — dismissed/done finding, new evidence: back to `new`
    """
    from proactive.states import transition

    if not f.finding_key:
        raise ValueError("finding_key is required")
    existing = find_by_key(user, f.finding_key)
    if existing is None:
        return await create(user, f), "created"
    if existing.evidence_hash == f.evidence_hash:
        return existing, "unchanged"
    reopen = transition(existing.status, "reopen") is not None
    fields = {
        "title": f.title,
        "topic": f.topic,
        "urgency": f.urgency,
        "advice": bool(f.advice),
        "doc_ids": [int(x) for x in f.doc_ids],
        "evidence_hash": f.evidence_hash,
        "analysis_version": int(f.analysis_version),
    }
    if f.kind == "deadline":
        fields.update(deadline=f.deadline, deadline_kind=f.deadline_kind)
    if reopen:
        fields.update(status="new", surfaced_at=None, snoozed_until=None)
        if f.kind == "deadline":
            fields["reminders_sent"] = []

    def mutate(cur: Finding) -> dict:
        if not reopen and cur.status != existing.status:
            # The user changed it meanwhile; refresh content, keep their status.
            fields.pop("status", None)
        return dict(fields)

    updated = await update(user, existing.finding_id, mutate, body=lambda _cur: f.body)
    return updated or existing, "reopened" if reopen else "updated"


# ── Series: several documents, one finding ───────────────────────────────────

_EV_QUOTE = re.compile(r"^> (?!— \[#)(.+)$")
_EV_REF = re.compile(r"^> — \[#(\d+)\]")
#: A merged finding keeps at most this many quotes (newest documents first).
MAX_EVIDENCE = 12
_URGENCY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def series_key(topic: str, correspondent: int | None, doc_id: int) -> str:
    """One finding per (topic, sender); without a sender, one per document."""
    if correspondent:
        return f"insight:{topic}:c{int(correspondent)}"
    return f"insight:{topic}:{int(doc_id)}"


def parse_evidence(body: str) -> list[dict]:
    """The `[{doc_id, quote}]` of a body written by `render_body`."""
    out, quote = [], None
    for line in body.splitlines():
        m = _EV_REF.match(line)
        if m and quote is not None:
            out.append({"doc_id": int(m.group(1)), "quote": quote})
            quote = None
            continue
        q = _EV_QUOTE.match(line)
        quote = q.group(1).strip() if q else None
    return out


def _section(body: str, heading: str) -> str:
    lines, inside = [], False
    for line in body.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip() == heading
            continue
        if inside:
            lines.append(line)
    return "\n".join(lines).strip()


def series_body(lang: str, *, summary: str, doc_ids: list[int], evidence: list[dict],
                sources: list[dict] | None = None, recommendation: str = "") -> str:
    """`render_body` for a series: the documents named in the summary, the
    quotes of the newest documents first, deduplicated."""
    from i18n import translator_for

    _ = translator_for(lang)
    seen, ev = set(), []
    for e in sorted(evidence, key=lambda e: -int(e["doc_id"])):
        k = " ".join(e["quote"].lower().split())
        if k not in seen:
            seen.add(k)
            ev.append(e)
    docs = sorted(set(int(d) for d in doc_ids))
    text = summary.strip()
    if len(docs) > 1:
        text += "\n\n" + _("Seen in {n} documents: {docs}").format(
            n=len(docs), docs=", ".join(f"#{d}" for d in docs)
        )
    return render_body(lang, summary=text, evidence=ev[:MAX_EVIDENCE], sources=sources,
                       recommendation=recommendation)


async def add_to_series(user: str, existing: Finding, new: Finding) -> tuple[Finding, str]:
    """Attach `new` (one more document of the same series) to `existing`.

    Nothing is sent for it: the matter is already known. The user's status
    stands — a dismissed or done series stays so, unless the new document makes
    it critical. Returns `(finding, "merged" | "unchanged" | "reopened")`."""
    from i18n import translator_for
    from proactive.states import transition

    new_docs = [d for d in new.doc_ids if d not in existing.doc_ids]
    if not new_docs:
        return existing, "unchanged"
    old_ev = parse_evidence(existing.body)
    ev = old_ev + [e for e in parse_evidence(new.body) if e["doc_id"] in new_docs]
    doc_ids = sorted(set(existing.doc_ids) | set(new_docs))
    escalates = _URGENCY_RANK.get(new.urgency, 1) > _URGENCY_RANK.get(existing.urgency, 1)
    reopen = new.urgency == "critical" and escalates and transition(existing.status, "reopen") is not None
    _ = translator_for(existing.lang)
    seen_prefix = _("Seen in {n} documents: {docs}").split("{n}")[0]
    summary = "\n\n".join(
        p for p in _section(existing.body, _("Summary")).split("\n\n") if not p.startswith(seen_prefix)
    ) or _section(new.body, _("Summary"))
    rec = _section(new.body, _("Recommendation")) or _section(existing.body, _("Recommendation"))
    rec = rec.split("\n> ")[0].strip()  # the disclaimer is re-added by code
    srcs = []
    for body in (new.body, existing.body):
        for line in _section(body, _("Sources")).splitlines():
            m = _SOURCE_LINE.match(line.strip())
            if m:
                srcs.append({"url": m.group(1), "retrieved": m.group(2)})
    fields = {
        "doc_ids": doc_ids,
        "urgency": new.urgency if escalates else existing.urgency,
        "advice": bool(rec),
        "evidence_hash": evidence_hash(doc_ids, [existing.finding_key]),
        "finding_key": new.finding_key,
        "correspondent": new.correspondent if new.correspondent is not None else existing.correspondent,
    }
    if not existing.topic_label and new.topic_label:
        fields["topic_label"] = new.topic_label
    if reopen:
        fields.update(status="new", surfaced_at=None, snoozed_until=None)
    body = series_body(existing.lang, summary=summary, doc_ids=doc_ids, evidence=ev,
                       sources=srcs, recommendation=rec)
    done = await update(user, existing.finding_id, lambda _cur: dict(fields), body=lambda _cur: body)
    return done or existing, "reopened" if reopen else "merged"


async def upsert_series(user: str, f: Finding) -> tuple[Finding, str]:
    """`upsert` for insights: a new document of a known series joins it."""
    existing = find_by_key(user, f.finding_key)
    if existing is not None and set(f.doc_ids) - set(existing.doc_ids):
        return await add_to_series(user, existing, f)
    return await upsert(user, f)
