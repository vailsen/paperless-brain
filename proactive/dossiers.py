"""Topic dossiers (architecture §4.4): one Markdown file per matter in the
user's `Dossiers/` folder — current state, a timeline with a document id per
entry, open questions, saved insights, research links.

Built once from the whole thread (app side, `deep_dive_app.dossier`), then
updated incrementally: a new document in the topic plus the existing dossier
gives the updated dossier, without re-reading everything. Bumping
`PROACTIVE_ANALYSIS_VERSION` rebuilds it. Code checks every timeline entry —
a date that does not parse or a document id outside the dossier's documents is
dropped — and sets `revisit_after` from the topic's interest weight.

Findings workspace (docs/findings-workspace.md):

- **The folder tree under `Dossiers/` is the dossier tree.** A dossier may sit
  in a subfolder; it is found by its `topic` key, never by its path.
- **Sections carry invisible anchors** (`<!-- pb:state -->` …) because the
  headings are translated; a file without anchors is read by position once and
  gets anchors on the next write.
- **Insights are code-owned**, like `research`: saved from a chat answer by
  the user, never edited or dropped by a rebuild. Their provenance comes from
  the turn's tool log (`app_ui/dossier_save.py`), never from a model.
- A dossier the user created by hand has `origin: user`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from proactive import clock
from proactive.paths import DOSSIERS, findings_root

log = logging.getLogger("proactive.dossiers")

_TL_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2}) — (.*) — \[#(\d+)\]")
_ANCHOR_RE = re.compile(r"^<!-- pb:(\w+) -->\s*$")
_INSIGHT_RE = re.compile(r"^<!-- pb:insight (\{.*\}) -->\s*$")
_RESOLVED_RE = re.compile(r"^~~(.*)~~")
#: Folder depth shown in the tree (a binder shelf, not a file system).
MAX_DEPTH = 3
#: Shelves next to the tree, inside `Dossiers/`: archived dossiers, and the
#: record of deleted ones. Never part of the tree, never found by `load`.
ARCHIVE = "_Archive"
TRASH = "_Deleted"
SHELVES = (ARCHIVE, TRASH)
SECTION_KEYS = ("state", "timeline", "questions", "insights", "research")


@dataclass
class Insight:
    """One saved chat excerpt. `text`/`title` are the user's (after the merge
    round's preview); everything else is written by code."""

    insight_id: str
    created: str  # local ISO minute, "2026-11-02T14:03"
    title: str
    text: str
    evidence: str = "reasoning"  # documents | web | documents+web | reasoning
    conv_id: str = ""
    msg_index: int = -1
    model: str = ""
    expert: str = ""
    docs: list[dict] = field(default_factory=list)  # [{id, page}]
    web: list[dict] = field(default_factory=list)   # [{url, title}]
    merged_by: str = ""  # model of the merge round; "" = saved verbatim
    unsupported: list[str] = field(default_factory=list)  # numbers the check could not find
    answers: list[str] = field(default_factory=list)  # open questions this answered

    def meta(self) -> dict:
        d = {
            "id": self.insight_id, "created": self.created, "evidence": self.evidence,
            "conv": self.conv_id, "msg": self.msg_index, "model": self.model, "expert": self.expert,
            "docs": self.docs, "web": self.web, "merged_by": self.merged_by,
        }
        if self.unsupported:
            d["unsupported"] = self.unsupported
        if self.answers:
            d["answers"] = self.answers
        return d


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
    #: Saved insights, newest first — code-owned (see module docstring).
    insights: list[Insight] = field(default_factory=list)
    #: Open questions an insight answered: `{question, insight}` — code-owned.
    resolved: list[dict] = field(default_factory=list)
    origin: str = "proactive"  # proactive | user
    label: str = ""  # display name of a hand-made dossier
    expert: str = ""  # default expert of the chat dock
    #: Documents the user added by hand — code-owned: a rebuild reads them and
    #: never drops them.
    added_docs: list[int] = field(default_factory=list)
    #: Questions the user removed — code-owned, so a rebuild cannot bring
    #: them back.
    dropped_questions: list[str] = field(default_factory=list)
    #: Lifecycle (see "Archive and trash" below) — code-owned.
    archived: str = ""          # date it went into the archive
    archived_from: str = ""     # its folder before
    closed_findings: dict = field(default_factory=dict)  # finding id → status the archive closed
    new_since_archive: list[int] = field(default_factory=list)
    deleted: str = ""           # date it went into the trash
    reset: str = ""             # … by "Reset": its documents were handed back to the worker
    pbrain_id: str = ""
    rel: str = ""
    sha: str = ""

    def existing_payload(self) -> dict:
        out = {
            "doc_ids": self.doc_ids, "current_state": self.current_state,
            "timeline": self.timeline, "open_questions": self.open_questions, "lang": self.lang,
        }
        if self.insights:
            # Read-only context for the rebuild: the state may reflect what the
            # user saved, but the model cannot edit or drop an insight.
            out["saved_insights"] = [f"{i.created[:10]} {i.title}: {i.text[:300]}" for i in self.insights[:10]]
        return out

    @property
    def display(self) -> str:
        if self.label:
            return self.label
        from proactive.findings import topic_display

        return topic_display(self.topic, "", self.lang)

    @property
    def shelf(self) -> str:
        """Where the file lives: "" (the tree), `ARCHIVE` or `TRASH`."""
        if self.deleted or self.reset:
            return TRASH
        return ARCHIVE if self.archived else ""

    @property
    def folder(self) -> str:
        """Folder inside `Dossiers/` ("" = root), from the file's path."""
        parts = Path(self.rel).parts
        try:
            i = parts.index(DOSSIERS)
        except ValueError:
            return ""
        return "/".join(parts[i + 1:-1])


def _root(user: str) -> Path:
    return findings_root(user) / DOSSIERS


def _files(user: str, shelf: str = "") -> list[Path]:
    """The tree's dossier files — or, with `shelf`, the archive's / trash's."""
    d = _root(user) / shelf if shelf else _root(user)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.rglob("*.md")):
        if p.name.endswith(".conflict.md") or p.is_symlink():
            continue
        parts = p.relative_to(d).parts
        if len(parts) > (1 if shelf else MAX_DEPTH + 1) or (not shelf and parts[0] in SHELVES):
            continue
        out.append(p)
    return out


def _topic_of(p: Path) -> str:
    try:
        head = p.read_text(encoding="utf-8")[:2000]
    except OSError:
        return ""
    m = re.search(r"^topic:\s*['\"]?([^'\"\n]+?)['\"]?\s*$", head, re.M)
    return m.group(1) if m else ""


def _path(user: str, topic: str, shelf: str = "") -> Path:
    """The file of a topic: wherever it sits in the tree (or on `shelf`); a
    new one at the root."""
    for p in _files(user, shelf):
        if _topic_of(p) == topic:
            return p
    return (_root(user) / shelf if shelf else _root(user)) / f"{topic}.md"


def _ref(doc: dict) -> str:
    from proactive.findings import paperless_link

    page = f" p{doc['page']}" if doc.get("page") else ""
    return f"[#{doc['id']}{page}]({paperless_link(doc['id'])})"


def render_insight(i: Insight, _=lambda s: s) -> str:
    when = i.created.replace("T", " ")[:16]
    lines = [f"### {when} — {i.title}", f"<!-- pb:insight {json.dumps(i.meta(), ensure_ascii=False)} -->",
             i.text.strip(), ""]
    src = [f"> pb-source: chat · {_('conversation')} `{i.conv_id[:8] or '-'}` · {when}"]
    who = " · ".join(x for x in (f"model: {i.model}" if i.model else "", f"expert: {i.expert}" if i.expert else "") if x)
    if who:
        src.append(f"> {who}")
    if i.docs:
        src.append(f"> {_('documents')}: " + " · ".join(_ref(d) for d in i.docs))
    if i.web:
        src.append("> web: " + " · ".join(w["url"] for w in i.web))
    check = _("evidence: {e}").format(e=i.evidence)
    if i.merged_by:
        check += " · " + _("merged by {m}").format(m=i.merged_by)
    if i.unsupported:
        check += " · " + _("unconfirmed numbers: {n}").format(n=", ".join(i.unsupported))
    src.append(f"> {check}")
    return "\n".join(lines + src)


def render_body(d: Dossier) -> str:
    from i18n import translator_for
    from proactive.findings import paperless_link

    _ = translator_for(d.lang)
    out = ["<!-- pb:state -->", f"## {_('Current state')}", "", d.current_state.strip(), "",
           "<!-- pb:timeline -->", f"## {_('Timeline')}", ""]
    for t in d.timeline:
        out.append(f"- {t['date']} — {t['event']} — [#{t['doc_id']}]({paperless_link(t['doc_id'])})")
    out += ["", "<!-- pb:questions -->", f"## {_('Open questions')}", ""]
    out += [f"- {q}" for q in d.open_questions] or ["-"]
    for r in d.resolved:
        link = r.get("insight") or ""
        out.append(f"- ~~{r['question']}~~" + (f" → {link}" if link else ""))
    if d.insights:
        out += ["", "<!-- pb:insights -->", f"## {_('Insights')}", ""]
        out.append("\n\n".join(render_insight(i, _) for i in d.insights))
    if d.research:
        out += ["", "<!-- pb:research -->", f"## {_('Research')}", ""]
        out += [f"- {link}" for link in d.research]
    return "\n".join(out) + "\n"


def _sections(body: str) -> dict[str, list[str]]:
    """Anchored sections; a body without anchors is read by position (the
    pre-anchor layout: state, timeline, questions, research)."""
    lines = body.splitlines()
    if any(_ANCHOR_RE.match(ln) for ln in lines):
        out: dict[str, list[str]] = {}
        cur = None
        for ln in lines:
            m = _ANCHOR_RE.match(ln)
            if m and m.group(1) in SECTION_KEYS:
                cur = m.group(1)
                out[cur] = []
            elif cur is not None and not (ln.startswith("## ") and not out[cur]):
                out[cur].append(ln)
        return out
    positional: list[list[str]] = []
    for ln in lines:
        if ln.startswith("## "):
            positional.append([])
        elif positional:
            positional[-1].append(ln)
    keys = ("state", "timeline", "questions", "research")
    return {k: positional[i] for i, k in enumerate(keys) if i < len(positional)}


def _parse_insights(lines: list[str]) -> list[Insight]:
    out: list[Insight] = []
    cur: dict | None = None
    body: list[str] = []

    def flush() -> None:
        if cur is None:
            return
        meta = cur.get("meta") or {}
        text = "\n".join(body).strip()
        out.append(Insight(
            insight_id=str(meta.get("id") or uuid.uuid4().hex[:8]), created=str(meta.get("created") or cur["when"]),
            title=cur["title"], text=text, evidence=str(meta.get("evidence") or "reasoning"),
            conv_id=str(meta.get("conv") or ""), msg_index=int(meta.get("msg") if meta.get("msg") is not None else -1),
            model=str(meta.get("model") or ""), expert=str(meta.get("expert") or ""),
            docs=[d for d in meta.get("docs") or [] if isinstance(d, dict) and str(d.get("id", "")).isdigit()],
            web=[w for w in meta.get("web") or [] if isinstance(w, dict) and w.get("url")],
            merged_by=str(meta.get("merged_by") or ""),
            unsupported=[str(x) for x in meta.get("unsupported") or []],
            answers=[str(x) for x in meta.get("answers") or []],
        ))

    for ln in lines:
        if ln.startswith("### "):
            flush()
            head = ln[4:]
            when, _sep, title = head.partition(" — ")
            cur, body = {"when": when.strip().replace(" ", "T"), "title": title.strip() or head.strip()}, []
            continue
        if cur is None:
            continue
        m = _INSIGHT_RE.match(ln)
        if m:
            try:
                cur["meta"] = json.loads(m.group(1))
            except ValueError:
                cur["meta"] = {}
            continue
        if ln.startswith("> pb-source") or cur.get("footer"):
            cur["footer"] = True  # the footer is regenerated from the metadata
            continue
        body.append(ln)
    flush()
    return out


def parse(text: str, *, rel: str = "") -> Dossier | None:
    from vault.frontmatter import get_id
    from vault.note_text import properties, split_note
    from vault.notes import sha_of

    note = split_note(text)
    m = properties(note.fm_raw)
    if not m.get("topic"):
        return None
    sec = _sections(note.body)
    timeline = []
    for line in sec.get("timeline", []):
        mt = _TL_RE.match(line.strip())
        if mt:
            timeline.append({"date": mt.group(1), "event": mt.group(2), "doc_id": int(mt.group(3))})
    questions = []
    for ln in sec.get("questions", []):
        if ln.startswith("- ") and ln[2:].strip() and ln[2:].strip() != "-" and not _RESOLVED_RE.match(ln[2:].strip()):
            questions.append(ln[2:].strip())
    questions = heal_questions(questions)
    research = [ln[2:].strip() for ln in sec.get("research", []) if ln.startswith("- ") and ln[2:].strip()]
    rev = m.get("revisit_after")
    resolved = [r for r in m.get("resolved") or [] if isinstance(r, dict) and r.get("question")]
    return Dossier(
        topic=str(m["topic"]),
        doc_ids=[int(x) for x in m.get("doc_ids") or [] if str(x).isdigit()],
        current_state="\n".join(sec.get("state", [])).strip(),
        timeline=timeline,
        open_questions=questions,
        last_doc_id=int(m.get("last_doc_id") or 0),
        analysis_version=int(m.get("analysis_version") or 1),
        revisit_after=rev.isoformat() if hasattr(rev, "isoformat") else (str(rev) if rev else None),
        lang=str(m.get("lang") or "en"),
        research=[str(x) for x in m.get("research") or [] if str(x).strip()] or research,
        insights=_parse_insights(sec.get("insights", [])),
        resolved=resolved,
        origin=str(m.get("origin") or "proactive"),
        label=str(m.get("label") or ""),
        expert=str(m.get("expert") or ""),
        added_docs=[int(x) for x in m.get("added_docs") or [] if str(x).isdigit()],
        dropped_questions=[str(x) for x in m.get("dropped_questions") or [] if str(x).strip()],
        archived=_day(m.get("archived")),
        archived_from=str(m.get("archived_from") or ""),
        closed_findings={str(k): str(v) for k, v in (m.get("closed_findings") or {}).items()}
        if isinstance(m.get("closed_findings"), dict) else {},
        new_since_archive=[int(x) for x in m.get("new_since_archive") or [] if str(x).isdigit()],
        deleted=_day(m.get("deleted")),
        reset=_day(m.get("reset")),
        pbrain_id=get_id(m),
        rel=rel,
        sha=sha_of(text),
    )


def _day(v) -> str:
    return v.isoformat() if hasattr(v, "isoformat") else (str(v) if v else "")


def _load_path(user: str, p: Path) -> Dossier | None:
    from vault.paths import vault_path

    try:
        return parse(p.read_text(encoding="utf-8"), rel=p.relative_to(vault_path(user)).as_posix())
    except OSError:
        return None


def load(user: str, topic: str, shelf: str = "") -> Dossier | None:
    return _load_path(user, _path(user, topic, shelf))


def list_dossiers(user: str) -> list[Dossier]:
    out, seen = [], set()
    for p in _files(user):
        x = _load_path(user, p)
        if x is not None and x.topic not in seen:
            seen.add(x.topic)
            out.append(x)
    return out


def by_id(user: str, pbrain_id: str) -> Dossier | None:
    if not pbrain_id:
        return None
    return next((d for d in list_dossiers(user) if d.pbrain_id == pbrain_id), None)


def folders(user: str) -> list[str]:
    """Every folder inside `Dossiers/` (relative, POSIX), shallow first."""
    d = _root(user)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.rglob("*")):
        if p.is_dir() and not p.is_symlink() and not p.name.startswith("."):
            rel = p.relative_to(d)
            if rel.parts[0] in SHELVES:
                continue
            if len(rel.parts) <= MAX_DEPTH:
                out.append(rel.as_posix())
    return sorted(out, key=lambda s: (s.count("/"), s.casefold()))


def revisit_after(user: str, topic: str, today: date) -> str | None:
    """By interest weight: high 30 days, normal 90, low 180, muted never."""
    from proactive import interests

    i = interests.load(user)
    if topic in i.muted:
        return None
    w = i.weight(topic)
    days = 30 if w >= 1.5 else (180 if w <= 0.6 else 90)
    return (today + timedelta(days=days)).isoformat()


def _as_list(value) -> list:
    """A list field as the model sent it. Local models sometimes send the
    list as a JSON string (`'["Wer zahlt?", …]'`); iterating that string
    filed every character as an open question of its own."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text[:1] in "[{":
            from werkbank.v2.models import repair_json

            for candidate in (text, repair_json(text)):
                try:
                    parsed = json.loads(candidate)
                except ValueError:
                    continue
                return parsed if isinstance(parsed, list) else [parsed]
        return [ln.strip().lstrip("-*•").strip() for ln in text.splitlines() if ln.strip().lstrip("-*•").strip()]
    return []


def heal_questions(qs: list[str]) -> list[str]:
    """Questions already broken into single characters (written before
    `_as_list`): unrecoverable — the spaces are gone — so they are dropped
    and the next rescan writes them again."""
    if len(qs) >= 4 and sum(len(q) <= 1 for q in qs) >= 0.8 * len(qs):
        return []
    return qs


def validated(result: dict, allowed_ids: set[int]) -> tuple[list[dict], list[str]]:
    """Timeline entries with a real date and a document of this dossier,
    oldest first; non-empty open questions."""
    tl = []
    for t in _as_list(result.get("timeline")):
        if not isinstance(t, dict):
            continue
        try:
            day = date.fromisoformat(str(t.get("date", ""))[:10])
            doc = int(t.get("doc_id"))
        except (TypeError, ValueError, AttributeError):
            continue
        event = " ".join(str(t.get("event") or "").split()).replace(" — ", " - ")
        if doc in allowed_ids and event:
            tl.append({"date": day.isoformat(), "event": event[:300], "doc_id": doc})
    tl.sort(key=lambda t: (t["date"], t["doc_id"]))
    qs = [" ".join(str(q).split()) for q in _as_list(result.get("open_questions")) if str(q).strip()]
    return tl, heal_questions(qs)[:20]


def _meta(d: Dossier) -> dict:
    meta = {
        "pbrain_id": d.pbrain_id or str(uuid.uuid4()),
        "topic": d.topic,
        "doc_ids": sorted(set(d.doc_ids)),
        "last_doc_id": max(d.doc_ids) if d.doc_ids else 0,
        "analysis_version": d.analysis_version,
        "revisit_after": d.revisit_after,
        "lang": d.lang,
        "origin": d.origin,
    }
    if d.research:
        meta["research"] = list(d.research)
    if d.resolved:
        meta["resolved"] = list(d.resolved)
    if d.label:
        meta["label"] = d.label
    if d.expert:
        meta["expert"] = d.expert
    if d.added_docs:
        meta["added_docs"] = sorted(set(d.added_docs))
    if d.dropped_questions:
        meta["dropped_questions"] = list(d.dropped_questions)
    for k in _LIFECYCLE:
        v = getattr(d, k)
        if v:
            meta[k] = v if not isinstance(v, list) else sorted(set(v))
    return meta


_LIFECYCLE = ("archived", "archived_from", "closed_findings", "new_since_archive", "deleted", "reset")


def _file_of(user: str, d: Dossier) -> Path:
    """The file `d` is written to: on a shelf (archive, trash) the very file
    it was read from — the trash may hold several records of one topic."""
    if d.shelf and d.rel:
        from vault.paths import vault_path

        p = vault_path(user) / d.rel
        if p.exists():
            return p
    return _path(user, d.topic, d.shelf)


async def write(user: str, d: Dossier) -> Dossier:
    """Create or replace (hash-checked, like findings) the dossier file."""
    from vault.locks import get_user_lock
    from vault.note_text import apply_edits, dump_mapping, join_note, split_note
    from vault.notes import NoteConflict, read_note, save_note
    from vault.paths import atomic_write_text, ensure_vault_dir, vault_path

    meta = _meta(d)
    body = render_body(d)
    for _attempt in range(2):
        path = _file_of(user, d)
        rel = path.relative_to(vault_path(user)).as_posix()
        if not path.exists():
            def _create(path=path) -> bool:
                if path.exists():
                    return False
                ensure_vault_dir(path.parent.parent)
                ensure_vault_dir(path.parent)
                atomic_write_text(path, f"---\n{dump_mapping(meta)}---\n{body}")
                return True

            async with get_user_lock(user):
                if await asyncio.to_thread(_create):
                    return _load_path(user, path)
            continue
        snap = await read_note(user, rel)
        note = split_note(snap.text)
        meta.pop("pbrain_id", None)  # keep the file's own identity
        remove = tuple(k for k in ("research", "resolved", "label", "expert", "added_docs", "dropped_questions",
                                   *_LIFECYCLE) if k not in meta)
        text = join_note(type(note)(apply_edits(note.fm_raw, set_=meta, remove=remove), body,
                                    note.open_fence, note.close_fence))
        try:
            await save_note(user, rel, text, expected_sha=snap.sha, base_text=snap.text)
            return _load_path(user, path)
        except NoteConflict:
            continue
    raise RuntimeError(f"{user}: dossier {d.topic} kept changing")


def from_result(user: str, topic: str, result: dict, *, lang: str, version: int, existing: Dossier | None) -> Dossier:
    ids = {int(x) for x in result.get("doc_ids") or []}
    tl, qs = validated(result, ids)
    if existing is not None:
        ids |= set(existing.doc_ids) if existing.origin == "user" else set()
        ids |= set(existing.added_docs)
        done = {r["question"] for r in existing.resolved} | set(existing.dropped_questions)
        qs = [q for q in qs if q not in done]
    return Dossier(
        topic=topic,
        doc_ids=sorted(ids),
        current_state=str(result.get("current_state") or "").strip(),
        timeline=tl,
        open_questions=qs,
        analysis_version=version,
        revisit_after=revisit_after(user, topic, clock.local_now().date()),
        lang=lang,
        # Code-owned parts the model never edits: research links, insights,
        # resolved questions, and what the user set by hand.
        research=list(existing.research) if existing else [],
        insights=list(existing.insights) if existing else [],
        resolved=list(existing.resolved) if existing else [],
        origin=existing.origin if existing else "proactive",
        label=existing.label if existing else "",
        expert=existing.expert if existing else "",
        added_docs=list(existing.added_docs) if existing else [],
        dropped_questions=list(existing.dropped_questions) if existing else [],
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


# ── Workspace: hand-made dossiers, tree, insights ──────────────────────

class DossierError(ValueError):
    pass


async def create_manual(user: str, label: str, folder: str = "", lang: str = "en") -> Dossier:
    """A dossier the user creates by hand. Its topic key comes from the label
    (`interests.slugify`), so findings of that topic land in it as well."""
    from proactive import interests

    label = " ".join((label or "").split())[:120]
    topic = interests.slugify(label)
    if not topic:
        raise DossierError("name")
    if load(user, topic) is not None:
        raise DossierError("exists")
    from proactive import analysis

    # Current analysis version: a version bump rebuilds proactive dossiers
    # from their thread; a hand-made one has no thread to rebuild from.
    d = Dossier(topic=topic, origin="user", label=label, lang=lang,
                revisit_after=None, analysis_version=analysis.version())
    if folder:
        await ensure_folder(user, folder)
        await write(user, d)
        await move(user, topic, folder)
        return load(user, topic)
    return await write(user, d)


def _safe_folder(folder: str) -> str:
    parts = [p.strip() for p in (folder or "").replace("\\", "/").split("/") if p.strip()]
    if any(p in (".", "..") or p.startswith(".") for p in parts) or len(parts) > MAX_DEPTH:
        raise DossierError("folder")
    from services.vault_config import safe_filename

    return "/".join(safe_filename(p) for p in parts)


async def ensure_folder(user: str, folder: str) -> str:
    from vault.locks import get_user_lock
    from vault.paths import ensure_vault_dir

    folder = _safe_folder(folder)
    target = _root(user) / folder

    def _mk() -> None:
        ensure_vault_dir(findings_root(user))
        ensure_vault_dir(_root(user))
        cur = _root(user)
        for part in Path(folder).parts:
            cur = cur / part
            ensure_vault_dir(cur)

    async with get_user_lock(user):
        await asyncio.to_thread(_mk)
    return target.relative_to(_root(user)).as_posix()


async def move(user: str, topic: str, folder: str) -> Dossier | None:
    """Move a dossier file into another folder of the tree ("" = root). The
    identity (`pbrain_id`, `topic`) is in the file and survives."""
    from vault.locks import get_user_lock

    folder = _safe_folder(folder)
    src = _path(user, topic)
    if not src.exists():
        return None
    dest_dir = _root(user) / folder if folder else _root(user)
    if folder:
        await ensure_folder(user, folder)

    def _mv() -> None:
        dest = dest_dir / src.name
        if dest == src:
            return
        if dest.exists():
            raise DossierError("exists")
        src.rename(dest)

    async with get_user_lock(user):
        await asyncio.to_thread(_mv)
    return load(user, topic)


async def rename(user: str, topic: str, label: str) -> Dossier | None:
    d = load(user, topic)
    if d is None:
        return None
    d.label = " ".join((label or "").split())[:120]
    return await write(user, d)


async def set_expert(user: str, topic: str, expert_id: str) -> Dossier | None:
    d = load(user, topic)
    if d is None:
        return None
    d.expert = expert_id or ""
    return await write(user, d)


async def add_insight(user: str, topic: str, insight: Insight, *, answered: list[str] | None = None,
                      new_questions: list[str] | None = None) -> Dossier:
    """Add a saved insight (newest first); tick the questions it answered and
    append the new ones. Hash-checked through `write`."""
    d = load(user, topic)
    if d is None:
        raise DossierError("missing")
    if not insight.insight_id:
        insight.insight_id = uuid.uuid4().hex[:8]
    d.insights.insert(0, insight)
    link = f"[[#{insight.created.replace('T', ' ')[:16]} — {insight.title}]]"
    for q in answered or []:
        if q in d.open_questions:
            d.open_questions.remove(q)
            d.resolved.append({"question": q, "insight": link})
    for q in new_questions or []:
        q = " ".join(q.split())[:300]
        if q and q not in d.open_questions:
            d.open_questions.append(q)
    return await write(user, d)


async def delete_insight(user: str, topic: str, insight_id: str) -> Dossier | None:
    d = load(user, topic)
    if d is None:
        return None
    d.insights = [i for i in d.insights if i.insight_id != insight_id]
    return await write(user, d)


async def add_documents(user: str, topic: str, doc_ids: list[int]) -> Dossier | None:
    """The user puts documents into a dossier. They count from now on (the
    Documents tab, the next relook); the model reads them on the relook."""
    d = load(user, topic)
    if d is None:
        return None
    d.added_docs = sorted(set(d.added_docs) | {int(i) for i in doc_ids})
    return await write(user, d)


async def remove_document(user: str, topic: str, doc_id: int) -> Dossier | None:
    """Only a hand-added document can be taken out; the rest belongs to the
    matter's thread and comes back on every rebuild."""
    d = load(user, topic)
    if d is None or doc_id not in d.added_docs:
        return d
    d.added_docs = [i for i in d.added_docs if i != doc_id]
    d.doc_ids = [i for i in d.doc_ids if i != doc_id]
    return await write(user, d)


async def _edit_questions(user: str, topic: str, change) -> Dossier | None:
    d = load(user, topic)
    if d is None:
        return None
    change(d)
    return await write(user, d)


async def resolve_question(user: str, topic: str, question: str) -> Dossier | None:
    """The user ticks a question off: answered, by them."""
    def change(d: Dossier) -> None:
        if question in d.open_questions:
            d.open_questions.remove(question)
            d.resolved.append({"question": question, "insight": "", "by": "user",
                               "at": clock.local_now().date().isoformat()})
    return await _edit_questions(user, topic, change)


async def reopen_question(user: str, topic: str, question: str) -> Dossier | None:
    def change(d: Dossier) -> None:
        d.resolved = [r for r in d.resolved if r.get("question") != question]
        if question not in d.open_questions:
            d.open_questions.append(question)
    return await _edit_questions(user, topic, change)


async def drop_question(user: str, topic: str, question: str) -> Dossier | None:
    """Not a question for this matter: gone, and kept out of every rebuild."""
    def change(d: Dossier) -> None:
        d.open_questions = [q for q in d.open_questions if q != question]
        if question not in d.dropped_questions:
            d.dropped_questions.append(question)
    return await _edit_questions(user, topic, change)


async def add_question(user: str, topic: str, question: str) -> Dossier | None:
    question = " ".join((question or "").split())[:300]

    def change(d: Dossier) -> None:
        if question and question not in d.open_questions:
            d.open_questions.append(question)
            d.dropped_questions = [q for q in d.dropped_questions if q != question]
    return await _edit_questions(user, topic, change)


def relook_key(user: str, topic: str) -> str:
    """Dedupe key of the user's "Relook" job (`tasks.topics.dossier_relook`)."""
    return f"dossier_relook:{user}:{topic}"


def all_doc_ids(user: str, d: Dossier) -> list[int]:
    """Everything this matter knows of: what the dossier was built from, what
    the user added, and the documents of its findings."""
    ids = set(d.doc_ids) | set(d.added_docs)
    for f in findings_of(user, d.topic):
        ids |= set(f.doc_ids)
    return sorted(ids)


def pending_docs(user: str, d: Dossier) -> list[int]:
    """Documents of the matter the dossier was not built from yet — what a
    relook would read for the first time."""
    return [i for i in all_doc_ids(user, d) if i not in set(d.doc_ids)]


async def file_finding(user: str, finding_id: str, topic: str) -> bool:
    """Put a finding into a dossier: it takes the dossier's topic (membership
    is the topic key). Its documents become the dossier's pending documents;
    reactions already counted for its old topic stay where they were."""
    from proactive import findings

    d = load(user, topic)
    if d is None:
        raise DossierError("missing")
    out = await findings.update(user, finding_id, lambda f: (
        None if f.topic == topic else {"topic": topic, "topic_label": d.display}))
    return out is not None


def context_text(d: Dossier, limit: int = 6000) -> str:
    """The dossier as the chat sees it: state, open questions and the user's
    saved insights first (they are what the dossier knows beyond its
    documents), then the newest timeline entries. A map, not evidence."""
    parts = [f"Current state:\n{d.current_state.strip() or '(not written yet)'}"]
    if d.open_questions:
        parts.append("Open questions:\n" + "\n".join(f"- {q}" for q in d.open_questions))
    if d.insights:
        parts.append("Insights the user saved (newest first):\n" + "\n".join(
            f"- {i.created[:10]} {i.title}: {' '.join(i.text.split())[:500]}" for i in d.insights[:8]))
    if d.timeline:
        parts.append("Timeline (newest first):\n" + "\n".join(
            f"- {t['date']} {t['event']} [#{t['doc_id']}]" for t in reversed(d.timeline[-25:])))
    ids = sorted(set(d.doc_ids) | set(d.added_docs))
    if ids:
        parts.append("Documents of this matter: " + ", ".join(f"#{i}" for i in ids[:80]))
    return "\n\n".join(parts)[:limit]


def findings_of(user: str, topic: str) -> list:
    from proactive import findings

    return [f for f in findings.list_findings(user) if f.topic == topic]


# ── Archive and trash ────────────────────────────────────────────────────
#
# A matter can be closed for good (archive) or taken away (delete, reset).
# Both shelves are folders in `Dossiers/` next to the tree, so the files stay
# in the user's vault — the vault is the source of truth, and what was put
# away can be brought back.
#
# - **Archive** closes the matter: the dossier moves to `_Archive/`, its open
#   findings are set to done (`closed_findings` remembers what each was), and
#   it is out of the tree, the chat's picker and every rebuild. A new document
#   of the matter is only noted (`new_since_archive`) — no model work, and a
#   finding of an archived topic is written as done, so nothing is sent.
#   "Unarchive" puts it back, reopens what the archive closed (a deadline that
#   has passed meanwhile stays done) and lists the new documents as pending.
# - **Delete** moves the file to `_Deleted/` — the record: name, documents,
#   insights. Its findings stay (they show as "not in a dossier").
# - **Reset** deletes the dossier *and* its findings, and hands its documents
#   back to the worker (`doc_reprocess`), which reads them as if new. The
#   record in `_Deleted/` keeps the list, so a reset can be repeated later.
# - From the trash: restore, reset, or remove the record for good.


class Shelved(DossierError):
    pass


def list_shelf(user: str, shelf: str) -> list[Dossier]:
    out = [x for p in _files(user, shelf) if (x := _load_path(user, p)) is not None]
    key = (lambda d: d.archived) if shelf == ARCHIVE else (lambda d: d.deleted or d.reset)
    return sorted(out, key=key, reverse=True)


def shelved_by_id(user: str, pbrain_id: str) -> Dossier | None:
    """A dossier of the archive or the trash, by its id."""
    for shelf in SHELVES:
        for d in list_shelf(user, shelf):
            if d.pbrain_id == pbrain_id:
                return d
    return None


def is_archived(user: str, topic: str) -> bool:
    """The topic's matter is closed: archived, and no live dossier of it
    (one made again by hand wins)."""
    if not topic or not any(_topic_of(p) == topic for p in _files(user, ARCHIVE)):
        return False
    return not any(_topic_of(p) == topic for p in _files(user))


async def _move_file(user: str, d: Dossier, dest_dir: Path) -> Path:
    """Move `d`'s file into `dest_dir` under a free name; returns the new path."""
    from vault.locks import get_user_lock
    from vault.paths import ensure_vault_dir, vault_path

    src = vault_path(user) / d.rel

    def _mv() -> Path:
        if not src.exists():
            raise DossierError("missing")
        ensure_vault_dir(_root(user))
        ensure_vault_dir(dest_dir)
        dest, n = dest_dir / src.name, 2
        while dest.exists() and dest != src:
            dest = dest_dir / f"{src.stem} ({n}){src.suffix}"
            n += 1
        if dest != src:
            src.rename(dest)
        return dest

    async with get_user_lock(user):
        return await asyncio.to_thread(_mv)


def _open_status(status: str) -> bool:
    from proactive.states import display_status

    return display_status(status) in ("open", "relevant")


async def archive(user: str, topic: str) -> Dossier:
    """Close the matter: the dossier into the archive, its open findings done."""
    from proactive import findings

    d = load(user, topic)
    if d is None:
        raise DossierError("missing")
    closed: dict[str, str] = {}
    for f in findings_of(user, topic):
        if _open_status(f.status):
            done = await findings.update(user, f.finding_id, lambda x: (
                {"status": "done", "snoozed_until": None} if _open_status(x.status) else None))
            if done is not None:
                closed[f.finding_id] = f.status
    folder = d.folder
    path = await _move_file(user, d, _root(user) / ARCHIVE)
    a = _load_path(user, path)
    a.archived = clock.local_now().date().isoformat()
    a.archived_from = folder
    a.closed_findings = closed
    log.info("%s: dossier %s archived, %d finding(s) closed", user, topic, len(closed))
    return await write(user, a)


async def unarchive(user: str, pbrain_id: str) -> Dossier:
    """Back into the tree (its old folder), with what the archive closed."""
    from proactive import findings

    d = shelved_by_id(user, pbrain_id)
    if d is None or d.shelf != ARCHIVE:
        raise DossierError("missing")
    if load(user, d.topic) is not None:
        raise DossierError("exists")
    today = clock.local_now().date().isoformat()
    for fid, status in d.closed_findings.items():
        f = findings.get(user, fid)
        if f is None or f.status != "done" or (f.deadline and str(f.deadline)[:10] < today):
            continue
        back = "surfaced" if status in ("new", "held", "snoozed") else status
        await findings.update(user, fid, lambda x, s=back: {"status": s} if x.status == "done" else None)
    try:
        folder = _safe_folder(d.archived_from)
    except DossierError:
        folder = ""
    if folder:
        await ensure_folder(user, folder)
    path = await _move_file(user, d, _root(user) / folder if folder else _root(user))
    out = _load_path(user, path)
    out.added_docs = sorted(set(out.added_docs) | set(out.new_since_archive))
    out.archived = out.archived_from = ""
    out.closed_findings, out.new_since_archive = {}, []
    return await write(user, out)


async def note_archived_doc(user: str, topic: str, doc_id: int) -> bool:
    """A new document of an archived matter: noted, nothing else."""
    d = load(user, topic, ARCHIVE)
    if d is None or doc_id in d.new_since_archive or doc_id in d.doc_ids:
        return False
    d.new_since_archive.append(int(doc_id))
    await write(user, d)
    return True


def _record_doc_ids(user: str, d: Dossier) -> list[int]:
    return sorted(set(all_doc_ids(user, d)) | set(d.new_since_archive))


async def delete_dossier(user: str, pbrain_id: str) -> Dossier:
    """Into the trash, as a record; its findings stay."""
    d = by_id(user, pbrain_id) or shelved_by_id(user, pbrain_id)
    if d is None or d.shelf == TRASH:
        raise DossierError("missing")
    path = await _move_file(user, d, _root(user) / TRASH)
    rec = _load_path(user, path)
    rec.deleted = clock.local_now().date().isoformat()
    rec.added_docs = sorted(set(rec.added_docs) | set(rec.new_since_archive))
    log.info("%s: dossier %s deleted (record kept)", user, d.topic)
    return await write(user, rec)


async def reset_dossier(user: str, pbrain_id: str) -> list[int]:
    """Delete the dossier and every finding of its topic; returns the
    documents to hand back to the worker. Also from the trash (a record)."""
    from proactive import findings
    from vault.notes import delete

    d = by_id(user, pbrain_id) or shelved_by_id(user, pbrain_id)
    if d is None:
        raise DossierError("missing")
    if d.shelf == TRASH and load(user, d.topic) is not None:
        raise DossierError("exists")  # a new dossier of that topic: reset that one
    ids = _record_doc_ids(user, d)
    for f in findings_of(user, d.topic):
        try:
            await delete(user, f.rel)
        except FileNotFoundError:
            pass
        findings._index.pop(f.finding_id, None)
    if d.shelf != TRASH:
        path = await _move_file(user, d, _root(user) / TRASH)
        d = _load_path(user, path)
    d.reset = clock.local_now().date().isoformat()
    d.added_docs = sorted(set(d.added_docs) | set(ids))
    await write(user, d)
    log.info("%s: dossier %s reset — %d document(s) back to the worker", user, d.topic, len(ids))
    return ids


async def restore(user: str, pbrain_id: str) -> Dossier:
    """A record from the trash back into the tree, at the top level."""
    d = shelved_by_id(user, pbrain_id)
    if d is None or d.shelf != TRASH:
        raise DossierError("missing")
    if load(user, d.topic) is not None:
        raise DossierError("exists")
    path = await _move_file(user, d, _root(user))
    out = _load_path(user, path)
    out.deleted = out.reset = out.archived = out.archived_from = ""
    out.closed_findings, out.new_since_archive = {}, []
    return await write(user, out)


async def purge(user: str, pbrain_id: str) -> bool:
    """Remove a record from the trash for good."""
    from vault.notes import delete

    d = shelved_by_id(user, pbrain_id)
    if d is None or d.shelf != TRASH:
        return False
    await delete(user, d.rel)
    return True


async def delete_folder(user: str, folder: str) -> int:
    """Remove a folder of the tree. Its dossiers (and subfolders' dossiers)
    move up to the folder's parent first, so no dossier is lost; returns how
    many moved."""
    from vault.locks import get_user_lock

    folder = _safe_folder(folder)
    if not folder or folder.split("/")[0] in SHELVES:
        raise DossierError("folder")
    parent = folder.rpartition("/")[0]
    target = _root(user) / folder
    moved = 0
    for d in list_dossiers(user):
        if d.folder == folder or d.folder.startswith(folder + "/"):
            await _move_file(user, d, _root(user) / parent if parent else _root(user))
            moved += 1

    def _rm() -> None:
        # Empty folders only, deepest first: anything else in there (a note
        # that is not a dossier, an attachment) is the user's and stays.
        if not target.is_dir():
            return
        for p in sorted((x for x in target.rglob("*") if x.is_dir()), key=lambda x: -len(x.parts)) + [target]:
            try:
                p.rmdir()
            except OSError:
                pass
        if target.exists():
            raise DossierError("not_empty")

    async with get_user_lock(user):
        await asyncio.to_thread(_rm)
    return moved
