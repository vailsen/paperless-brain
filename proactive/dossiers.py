"""Topic dossiers (architecture §4.4): one Markdown file per matter in the
user's `Dossiers/` folder — current state, a timeline with a document id per
entry, open questions.

Built once from the whole thread (app side, `deep_dive_app.dossier`), then
updated incrementally: a new document in the topic plus the existing dossier
gives the updated dossier, without re-reading everything. Bumping
`PROACTIVE_ANALYSIS_VERSION` rebuilds it. Code checks every timeline entry —
a date that does not parse or a document id outside the dossier's documents is
dropped — and sets `revisit_after` from the topic's interest weight.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from proactive import clock
from proactive.paths import DOSSIERS, findings_root

log = logging.getLogger("proactive.dossiers")

_TL_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2}) — (.*) — \[#(\d+)\]")


@dataclass
class Dossier:
    topic: str
    doc_ids: list[int] = field(default_factory=list)
    current_state: str = ""
    timeline: list[dict] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    last_doc_id: int = 0
    analysis_version: int = 1
    revisit_after: str | None = None
    lang: str = "en"
    #: Research notes on this matter (Obsidian links into `PaperlessBrain
    #: Research/`), newest last — written by the deep dive, not by the model.
    research: list[str] = field(default_factory=list)
    pbrain_id: str = ""
    rel: str = ""
    sha: str = ""

    def existing_payload(self) -> dict:
        return {
            "doc_ids": self.doc_ids, "current_state": self.current_state,
            "timeline": self.timeline, "open_questions": self.open_questions, "lang": self.lang,
        }


def _path(user: str, topic: str):
    return findings_root(user) / DOSSIERS / f"{topic}.md"


def render_body(d: Dossier) -> str:
    from i18n import translator_for
    from proactive.findings import paperless_link

    _ = translator_for(d.lang)
    out = [f"## {_('Current state')}", "", d.current_state.strip(), "", f"## {_('Timeline')}", ""]
    for t in d.timeline:
        out.append(f"- {t['date']} — {t['event']} — [#{t['doc_id']}]({paperless_link(t['doc_id'])})")
    out += ["", f"## {_('Open questions')}", ""]
    out += [f"- {q}" for q in d.open_questions] or ["-"]
    if d.research:
        out += ["", f"## {_('Research')}", ""]
        out += [f"- {link}" for link in d.research]
    return "\n".join(out) + "\n"


def parse(text: str, *, rel: str = "") -> Dossier | None:
    from vault.frontmatter import get_id
    from vault.note_text import properties, split_note
    from vault.notes import sha_of

    note = split_note(text)
    m = properties(note.fm_raw)
    if not m.get("topic"):
        return None
    sections: dict[str, list[str]] = {}
    cur = None
    for line in note.body.splitlines():
        if line.startswith("## "):
            cur = len(sections)
            sections[cur] = []
        elif cur is not None:
            sections[cur].append(line)
    parts = [sections.get(i, []) for i in range(3)]
    timeline = []
    for line in parts[1]:
        mt = _TL_RE.match(line.strip())
        if mt:
            timeline.append({"date": mt.group(1), "event": mt.group(2), "doc_id": int(mt.group(3))})
    rev = m.get("revisit_after")
    return Dossier(
        topic=str(m["topic"]),
        doc_ids=[int(x) for x in m.get("doc_ids") or [] if str(x).isdigit()],
        current_state="\n".join(parts[0]).strip(),
        timeline=timeline,
        open_questions=[ln[2:].strip() for ln in parts[2] if ln.startswith("- ") and ln[2:].strip()],
        last_doc_id=int(m.get("last_doc_id") or 0),
        analysis_version=int(m.get("analysis_version") or 1),
        revisit_after=rev.isoformat() if hasattr(rev, "isoformat") else (str(rev) if rev else None),
        lang=str(m.get("lang") or "en"),
        research=[str(x) for x in m.get("research") or [] if str(x).strip()],
        pbrain_id=get_id(m),
        rel=rel,
        sha=sha_of(text),
    )


def load(user: str, topic: str) -> Dossier | None:
    from vault.paths import vault_path

    p = _path(user, topic)
    try:
        return parse(p.read_text(encoding="utf-8"), rel=p.relative_to(vault_path(user)).as_posix())
    except OSError:
        return None


def list_dossiers(user: str) -> list[Dossier]:
    d = findings_root(user) / DOSSIERS
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.md")):
        x = load(user, p.stem)
        if x is not None:
            out.append(x)
    return out


def revisit_after(user: str, topic: str, today: date) -> str | None:
    """By interest weight: high 30 days, normal 90, low 180, muted never."""
    from proactive import interests

    i = interests.load(user)
    if topic in i.muted:
        return None
    w = i.weight(topic)
    days = 30 if w >= 1.5 else (180 if w <= 0.6 else 90)
    return (today + timedelta(days=days)).isoformat()


def validated(result: dict, allowed_ids: set[int]) -> tuple[list[dict], list[str]]:
    """Timeline entries with a real date and a document of this dossier,
    oldest first; non-empty open questions."""
    tl = []
    for t in result.get("timeline") or []:
        try:
            day = date.fromisoformat(str(t.get("date", ""))[:10])
            doc = int(t.get("doc_id"))
        except (TypeError, ValueError, AttributeError):
            continue
        event = " ".join(str(t.get("event") or "").split()).replace(" — ", " - ")
        if doc in allowed_ids and event:
            tl.append({"date": day.isoformat(), "event": event[:300], "doc_id": doc})
    tl.sort(key=lambda t: (t["date"], t["doc_id"]))
    qs = [" ".join(str(q).split()) for q in result.get("open_questions") or [] if str(q).strip()]
    return tl, qs[:20]


async def write(user: str, d: Dossier) -> Dossier:
    """Create or replace (hash-checked, like findings) the dossier file."""
    from vault.locks import get_user_lock
    from vault.note_text import apply_edits, dump_mapping, join_note, split_note
    from vault.notes import NoteConflict, read_note, save_note
    from vault.paths import atomic_write_text, ensure_vault_dir, vault_path

    meta = {
        "pbrain_id": d.pbrain_id or str(uuid.uuid4()),
        "topic": d.topic,
        "doc_ids": sorted(set(d.doc_ids)),
        "last_doc_id": max(d.doc_ids) if d.doc_ids else 0,
        "analysis_version": d.analysis_version,
        "revisit_after": d.revisit_after,
        "lang": d.lang,
    }
    if d.research:
        meta["research"] = list(d.research)
    body = render_body(d)
    path = _path(user, d.topic)
    rel = path.relative_to(vault_path(user)).as_posix()
    for _attempt in range(2):
        if not path.exists():
            def _create() -> bool:
                if path.exists():
                    return False
                ensure_vault_dir(path.parent.parent)
                ensure_vault_dir(path.parent)
                atomic_write_text(path, f"---\n{dump_mapping(meta)}---\n{body}")
                return True

            async with get_user_lock(user):
                if await asyncio.to_thread(_create):
                    return load(user, d.topic)
            continue
        snap = await read_note(user, rel)
        note = split_note(snap.text)
        meta.pop("pbrain_id", None)  # keep the file's own identity
        text = join_note(type(note)(apply_edits(note.fm_raw, set_=meta), body, note.open_fence, note.close_fence))
        try:
            await save_note(user, rel, text, expected_sha=snap.sha, base_text=snap.text)
            return load(user, d.topic)
        except NoteConflict:
            continue
    raise RuntimeError(f"{user}: dossier {d.topic} kept changing")


def from_result(user: str, topic: str, result: dict, *, lang: str, version: int, existing: Dossier | None) -> Dossier:
    ids = {int(x) for x in result.get("doc_ids") or []}
    tl, qs = validated(result, ids)
    return Dossier(
        topic=topic,
        doc_ids=sorted(ids),
        current_state=str(result.get("current_state") or "").strip(),
        timeline=tl,
        open_questions=qs,
        analysis_version=version,
        revisit_after=revisit_after(user, topic, clock.local_now().date()),
        lang=lang,
        research=list(existing.research) if existing else [],  # links the model never sees
        pbrain_id=existing.pbrain_id if existing else "",
    )


async def add_research(user: str, topic: str, note_rel: str) -> bool:
    """Link a research note into the topic's dossier, if there is one. A
    matter without a dossier keeps the link in its finding only — building a
    dossier is a model job of its own."""
    from vault.research import wikilink

    if not topic:
        return False
    d = load(user, topic)
    if d is None:
        return False
    link = wikilink(note_rel)
    if link in d.research:
        return False
    d.research.append(link)
    await write(user, d)
    return True
