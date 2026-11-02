"""Topic dossiers (architecture §4.4): one Markdown file per matter in the
user's `Dossiers/` folder — current state, a timeline with a document id per
entry, open questions, saved insights, research links.

Built once from the whole thread (app side, `deep_dive_app.dossier`), then
updated incrementally: a new document in the topic plus the existing dossier
gives the updated dossier, without re-reading everything. Bumping
`PROACTIVE_ANALYSIS_VERSION` rebuilds it. Code checks every timeline entry —
a date that does not parse or a document id outside the dossier's documents is
dropped — and sets `revisit_after` from the topic's interest weight.

Mission control (docs/findings-mission-control.md):

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


def _files(user: str) -> list[Path]:
    d = _root(user)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.rglob("*.md")):
        if p.name.endswith(".conflict.md") or p.is_symlink():
            continue
        if len(p.relative_to(d).parts) > MAX_DEPTH + 1:
            continue
        out.append(p)
    return out


def _path(user: str, topic: str) -> Path:
    """The file of a topic: wherever it sits in the tree; a new one at the root."""
    for p in _files(user):
        try:
            head = p.read_text(encoding="utf-8")[:2000]
        except OSError:
            continue
        if re.search(rf"^topic:\s*['\"]?{re.escape(topic)}['\"]?\s*$", head, re.M):
            return p
    return _root(user) / f"{topic}.md"


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
        out.append(f"- ~~{r['question']}~~ → {r.get('insight', '')}")
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
        pbrain_id=get_id(m),
        rel=rel,
        sha=sha_of(text),
    )


def _load_path(user: str, p: Path) -> Dossier | None:
    from vault.paths import vault_path

    try:
        return parse(p.read_text(encoding="utf-8"), rel=p.relative_to(vault_path(user)).as_posix())
    except OSError:
        return None


def load(user: str, topic: str) -> Dossier | None:
    return _load_path(user, _path(user, topic))


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
    return meta


async def write(user: str, d: Dossier) -> Dossier:
    """Create or replace (hash-checked, like findings) the dossier file."""
    from vault.locks import get_user_lock
    from vault.note_text import apply_edits, dump_mapping, join_note, split_note
    from vault.notes import NoteConflict, read_note, save_note
    from vault.paths import atomic_write_text, ensure_vault_dir, vault_path

    meta = _meta(d)
    body = render_body(d)
    for _attempt in range(2):
        path = _path(user, d.topic)
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
                    return load(user, d.topic)
            continue
        snap = await read_note(user, rel)
        note = split_note(snap.text)
        meta.pop("pbrain_id", None)  # keep the file's own identity
        remove = tuple(k for k in ("research", "resolved", "label", "expert") if k not in meta)
        text = join_note(type(note)(apply_edits(note.fm_raw, set_=meta, remove=remove), body,
                                    note.open_fence, note.close_fence))
        try:
            await save_note(user, rel, text, expected_sha=snap.sha, base_text=snap.text)
            return load(user, d.topic)
        except NoteConflict:
            continue
    raise RuntimeError(f"{user}: dossier {d.topic} kept changing")


def from_result(user: str, topic: str, result: dict, *, lang: str, version: int, existing: Dossier | None) -> Dossier:
    ids = {int(x) for x in result.get("doc_ids") or []}
    tl, qs = validated(result, ids)
    if existing is not None:
        ids |= set(existing.doc_ids) if existing.origin == "user" else set()
        done = {r["question"] for r in existing.resolved}
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


# ── Mission control: hand-made dossiers, tree, insights ──────────────────────

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


def findings_of(user: str, topic: str) -> list:
    from proactive import findings

    return [f for f in findings.list_findings(user) if f.topic == topic]
