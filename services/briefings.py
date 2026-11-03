"""Expert briefings (docs/experts.md §6): a map, not evidence.

An expert can *study* its documents once and keep a short briefing, so its
first conversation starts oriented — knowing the parties, the vocabulary,
the time range, the recurring matters. What it buys is mostly better
searches; what it must never become is a substitute for reading. So:

- **Two parts.** The *archive map* is computed by code from Paperless
  metadata and is always correct. The *orientation* is written by a model
  from the sidecar summaries (`full_summary_summarized`) — no PDF, no vision.
- **Code checks the orientation** before it is stored: every `(#id)` must be
  a studied document, every number must occur in the material of the
  documents it cites, items that state a fact must cite at least one
  document. What is dropped is kept with its reason and shown to the user.
- **Refs are written `(#241)`, never `[^241]`**, so the citation machinery
  cannot turn a briefing line into a citation. Only tool results are cited.
- **In the prompt it sits under a code header** that calls it a map and
  forbids stating a fact from it without reading the document through a tool.
- Not embedded: the file is under the experts folder (config path).

The study job runs in the app process; progress lives in `_JOBS` and the page
polls it, so it survives the tab. A restart loses a running job — the
briefing file is only written when the job finishes, and nothing restarts on
its own (the user presses "Study" again).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from config.settings import settings
from services import vault_config
from services.experts import SCOPE_KEYS

log = logging.getLogger("briefings")

BRIEFINGS = "Briefings"
#: Above this many documents only a sample is studied (newest + a few per
#: correspondent); the Briefing tab says how many of how many. Every document
#: below it is read: a sample of a 515-document archive looked like an expert
#: that sees only 300 (one model call per 25 summaries, ≈ 21 calls).
MAX_STUDY = 1500
SAMPLE_NEWEST = 200
SAMPLE_PER_CORRESPONDENT = 5
BATCH = 25
SUMMARY_CHARS = 400
MAX_ITEMS = 8
MAX_ITEM_CHARS = 240
MAX_ABOUT_CHARS = 600
#: Hard cap of what goes into the system prompt (≈1 200 tokens).
PROMPT_CHARS = 4800

_DIGITS = re.compile(r"\d[\d.,]*\d|\d")
_REF = re.compile(r"\(#(\d+)\)")

SECTIONS = ("about", "parties", "matters", "events", "open_items", "glossary")


# ── Archive map (code only) ───────────────────────────────────────────────────

def _year(doc) -> int | None:
    try:
        return doc.created.year
    except AttributeError:
        return None


def archive_map(docs: list) -> dict:
    """Counts and series from metadata alone — correct by construction."""
    corr = Counter(d.correspondent for d in docs if d.correspondent)
    types = Counter(d.document_type for d in docs if d.document_type)
    tags = Counter(t for d in docs for t in (d.tags or []))
    years = Counter(y for y in (_year(d) for d in docs) if y)
    dates = sorted(d.created for d in docs if getattr(d, "created", None))
    series = []
    groups: dict[tuple, list] = defaultdict(list)
    for d in docs:
        if d.correspondent and d.document_type:
            groups[(d.correspondent, d.document_type)].append(d)
    for (c, t), ds in groups.items():
        ds_dates = sorted(x.created for x in ds)
        if len(ds) >= 4 and (ds_dates[-1] - ds_dates[0]).days >= 90:
            series.append({"correspondent": c, "type": t, "count": len(ds), "since": ds_dates[0].date().isoformat()})
    series.sort(key=lambda s: -s["count"])
    newest = sorted(docs, key=lambda d: d.created, reverse=True)[:8]
    return {
        "total": len(docs),
        "first": dates[0].date().isoformat() if dates else None,
        "last": dates[-1].date().isoformat() if dates else None,
        # Lists, not tuples: this goes into YAML frontmatter.
        "correspondents": [[k, n] for k, n in corr.most_common(12)],
        "document_types": [[k, n] for k, n in types.most_common(12)],
        "tags": [[k, n] for k, n in tags.most_common(12)],
        "years": [[y, n] for y, n in sorted(years.items())],
        "series": series[:8],
        "newest": [{"id": d.id, "title": d.title, "date": d.created.date().isoformat()} for d in newest],
    }


def map_markdown(m: dict, _=lambda s: s) -> str:
    def counts(pairs) -> str:
        return ", ".join(f"{name} {n}" for name, n in pairs) or "–"

    lines = [
        "- " + _("{n} documents, {first} – {last}").format(n=m["total"], first=m.get("first") or "?", last=m.get("last") or "?"),
        "- " + _("Correspondents: {list}").format(list=counts(m["correspondents"])),
        "- " + _("Document types: {list}").format(list=counts(m["document_types"])),
        "- " + _("Tags: {list}").format(list=counts(m["tags"])),
    ]
    for s in m.get("series") or []:
        lines.append("- " + _("Recurring: {c} · {t} — {n} documents since {since}").format(
            c=s["correspondent"], t=s["type"], n=s["count"], since=s["since"]))
    if m.get("newest"):
        lines.append("- " + _("Newest: {list}").format(
            list="; ".join(f"(#{x['id']}) {x['title']} — {x['date']}" for x in m["newest"])))
    return "\n".join(lines)


# ── Selection ─────────────────────────────────────────────────────────────────

def select_for_study(docs: list) -> list:
    if len(docs) <= MAX_STUDY:
        return list(docs)
    newest = sorted(docs, key=lambda d: d.created, reverse=True)
    picked = {d.id: d for d in newest[:SAMPLE_NEWEST]}
    per: dict[str, int] = defaultdict(int)
    for d in newest:
        key = d.correspondent or ""
        if key and per[key] < SAMPLE_PER_CORRESPONDENT:
            per[key] += 1
            picked.setdefault(d.id, d)
    return sorted(picked.values(), key=lambda d: d.created)


def material(doc, sidecar: dict | None) -> str:
    summary = ""
    if sidecar:
        summary = (sidecar.get("full_summary_summarized") or sidecar.get("full_summary") or "").strip()
    head = " · ".join(x for x in (
        doc.created.date().isoformat() if getattr(doc, "created", None) else "",
        doc.correspondent or "", doc.document_type or "", doc.title or "") if x)
    return f"(#{doc.id}) {head}: {summary[:SUMMARY_CHARS]}"


# ── Checks (not overridable) ─────────────────────────────────────────────────

def _numbers(text: str) -> list[str]:
    return [n.rstrip(".,") for n in _DIGITS.findall(text or "")]


def check_item(text: str, ids: list[int], *, studied: dict[int, str], needs_ref: bool) -> str | None:
    """None if the item may stay, else the reason it is dropped."""
    text = " ".join((text or "").split())
    if not text:
        return "empty"
    unknown = [i for i in ids if i not in studied]
    if unknown:
        return "unknown_id"
    if needs_ref and not ids:
        return "no_source"
    haystack = " ".join(studied[i] for i in ids) if ids else " ".join(studied.values())
    flat = haystack.replace(" ", "")
    for n in _numbers(text):
        if n in {str(i) for i in ids}:
            continue
        if n not in haystack and n.replace(".", "").replace(",", "") not in flat.replace(".", "").replace(",", ""):
            return "unsupported_number"
    return None


def validate(result: dict, studied: dict[int, str]) -> tuple[dict, list[dict]]:
    """Structured model answer → (kept sections, dropped items with reasons)."""
    kept: dict = {k: [] for k in SECTIONS}
    dropped: list[dict] = []

    def ints(v) -> list[int]:
        out = []
        for x in v or []:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                continue
        return out

    about = " ".join(str(result.get("about") or "").split())[:MAX_ABOUT_CHARS]
    about_ids = [int(m) for m in _REF.findall(about)]
    reason = check_item(about, about_ids, studied=studied, needs_ref=False) if about else None
    if about and reason is None:
        kept["about"] = about
    elif about:
        dropped.append({"section": "about", "text": about, "reason": reason})
        kept["about"] = ""
    else:
        kept["about"] = ""
    for key in ("parties", "matters", "events", "open_items"):
        for item in (result.get(key) or [])[: MAX_ITEMS * 2]:
            if not isinstance(item, dict):
                continue
            text = " ".join(str(item.get("text") or "").split())[:MAX_ITEM_CHARS]
            ids = ints(item.get("doc_ids"))
            date = str(item.get("date") or "")[:10] if key == "events" else ""
            if key == "events":
                try:
                    datetime.strptime(date, "%Y-%m-%d")
                except ValueError:
                    dropped.append({"section": key, "text": text, "reason": "bad_date"})
                    continue
            reason = check_item(text, ids, studied=studied, needs_ref=True)
            if reason:
                dropped.append({"section": key, "text": text, "reason": reason})
                continue
            if len(kept[key]) < MAX_ITEMS:
                entry = {"text": text, "doc_ids": sorted(set(ids))}
                if date:
                    entry["date"] = date
                kept[key].append(entry)
    for item in (result.get("glossary") or [])[: MAX_ITEMS * 2]:
        if not isinstance(item, dict):
            continue
        term = " ".join(str(item.get("term") or "").split())[:60]
        meaning = " ".join(str(item.get("meaning") or "").split())[:MAX_ITEM_CHARS]
        reason = check_item(f"{term} {meaning}", [], studied=studied, needs_ref=False) if term and meaning else "empty"
        if reason:
            dropped.append({"section": "glossary", "text": f"{term}: {meaning}", "reason": reason})
        elif len(kept["glossary"]) < MAX_ITEMS:
            kept["glossary"].append({"term": term, "meaning": meaning})
    kept["events"].sort(key=lambda e: e.get("date", ""))
    return kept, dropped


def orientation_markdown(o: dict, _=lambda s: s) -> str:
    def refs(ids) -> str:
        return " " + " ".join(f"(#{i})" for i in ids) if ids else ""

    out = []
    if o.get("about"):
        out += [f"### {_('What this is about')}", o["about"], ""]
    titles = {"parties": _("Key parties"), "matters": _("Recurring matters"),
              "events": _("Notable events"), "open_items": _("Open items")}
    for key, title in titles.items():
        items = o.get(key) or []
        if not items:
            continue
        out.append(f"### {title}")
        for it in items:
            prefix = f"{it['date']} — " if it.get("date") else ""
            out.append(f"- {prefix}{it['text']}{refs(it.get('doc_ids'))}")
        out.append("")
    if o.get("glossary"):
        out.append(f"### {_('Glossary')}")
        out += [f"- **{g['term']}** — {g['meaning']}" for g in o["glossary"]]
        out.append("")
    return "\n".join(out).strip()


# ── Model prompts ─────────────────────────────────────────────────────────────

_MAP_SYSTEM = """\
You read short summaries of a person's documents and write orientation notes \
for an assistant who will later help them with these documents. Each note is \
one sentence about a party, a recurring matter, a notable event (with its \
date), an open item or a term. Use only what the summaries say. Every note \
names the documents it comes from in doc_ids (the numbers after #). Copy \
numbers exactly; leave a note out rather than guess. Write in {lang}."""

_REDUCE_SYSTEM = """\
You turn orientation notes about a person's documents into a short briefing \
for an assistant. It is a map for finding documents later, not a source of \
facts: be brief and concrete. Use only the notes and the archive map. Every \
party, matter, event and open item lists the doc_ids it comes from (only ids \
that appear in the notes). Events need an exact date (YYYY-MM-DD) from the \
notes. "about" is 2–4 sentences on what this collection is; you may write \
document references there as (#123). Glossary: terms and abbreviations that \
occur in the notes, with their meaning there. At most {n} items per list. \
Copy numbers exactly; drop anything you are unsure of. Write in {lang}."""

_NOTE_ITEM = {
    "type": "object",
    "properties": {"text": {"type": "string"}, "doc_ids": {"type": "array", "items": {"type": "integer"}}},
    "required": ["text", "doc_ids"],
}
MAP_SCHEMA = {"type": "object", "properties": {"notes": {"type": "array", "items": _NOTE_ITEM}}, "required": ["notes"]}
REDUCE_SCHEMA = {
    "type": "object",
    "properties": {
        "about": {"type": "string"},
        "parties": {"type": "array", "items": _NOTE_ITEM},
        "matters": {"type": "array", "items": _NOTE_ITEM},
        "events": {"type": "array", "items": {
            "type": "object",
            "properties": {"date": {"type": "string"}, "text": {"type": "string"},
                           "doc_ids": {"type": "array", "items": {"type": "integer"}}},
            "required": ["date", "text", "doc_ids"]}},
        "open_items": {"type": "array", "items": _NOTE_ITEM},
        "glossary": {"type": "array", "items": {
            "type": "object", "properties": {"term": {"type": "string"}, "meaning": {"type": "string"}},
            "required": ["term", "meaning"]}},
    },
    "required": ["about", "parties", "matters", "events", "open_items", "glossary"],
}


def estimate_calls(n_docs: int) -> int:
    n = n_docs
    return max(1, -(-n // BATCH)) + 1 if n else 0


# ── Briefing file ─────────────────────────────────────────────────────────────

@dataclass
class Briefing:
    expert_id: str
    built: str = ""
    model: str = ""
    studied_ids: list[int] = field(default_factory=list)
    last_doc_id: int = 0
    total_docs: int = 0
    skipped: int = 0
    scope_hash: str = ""
    map: dict = field(default_factory=dict)
    orientation: dict = field(default_factory=dict)
    dropped: list[dict] = field(default_factory=list)
    built_sha: str = ""
    body: str = ""
    rel: str = ""
    sha: str = ""

    @property
    def edited(self) -> bool:
        return bool(self.built_sha) and _body_sha(self.body) != self.built_sha


def _body_sha(body: str) -> str:
    return hashlib.sha256((body or "").strip().encode("utf-8")).hexdigest()[:16]


def scope_hash(scope_meta: dict) -> str:
    ids = {k: sorted(int(x.get("id") if isinstance(x, dict) else x) for x in (scope_meta or {}).get(k) or []
                     if str(x.get("id") if isinstance(x, dict) else x).lstrip("-").isdigit())
           for k in SCOPE_KEYS if (scope_meta or {}).get(k) or k in SCOPE_KEYS[:3]}
    return hashlib.sha256(json.dumps(ids, sort_keys=True).encode()).hexdigest()[:16]


def _subfolder() -> str:
    return f"{settings.experts_subfolder}/{BRIEFINGS}"


def load(user: str, expert_id: str) -> Briefing | None:
    for f in vault_config.list_files(user, _subfolder()):
        m = f.meta
        if m.get("kind") == "briefing" and str(m.get("expert_id")) == expert_id:
            return Briefing(
                expert_id=expert_id, built=str(m.get("built") or ""), model=str(m.get("model") or ""),
                studied_ids=[int(x) for x in m.get("studied_ids") or [] if str(x).isdigit()],
                last_doc_id=int(m.get("last_doc_id") or 0), total_docs=int(m.get("total_docs") or 0),
                skipped=int(m.get("skipped") or 0), scope_hash=str(m.get("scope_hash") or ""),
                map=m.get("map") if isinstance(m.get("map"), dict) else {},
                orientation=m.get("orientation") if isinstance(m.get("orientation"), dict) else {},
                dropped=[d for d in m.get("dropped") or [] if isinstance(d, dict)],
                built_sha=str(m.get("built_sha") or ""), body=f.body or "", rel=f.rel, sha=f.sha,
            )
    return None


def render_body(b: Briefing, _=lambda s: s) -> str:
    parts = [f"<!-- pb:map -->\n## {_('Archive map')}\n\n{map_markdown(b.map, _)}"]
    orient = orientation_markdown(b.orientation, _)
    if orient:
        parts.append(f"<!-- pb:orientation -->\n## {_('Orientation')}\n\n{orient}")
    return "\n\n".join(parts) + "\n"


async def save(user: str, b: Briefing, expert_name: str, _=lambda s: s) -> Briefing:
    body = render_body(b, _)
    b.built_sha = _body_sha(body)
    meta = {
        "kind": "briefing", "expert_id": b.expert_id, "built": b.built, "model": b.model,
        "total_docs": b.total_docs, "skipped": b.skipped, "last_doc_id": b.last_doc_id,
        "scope_hash": b.scope_hash, "built_sha": b.built_sha, "studied_ids": b.studied_ids,
        "map": b.map, "orientation": b.orientation, "dropped": b.dropped[:50],
    }
    old = load(user, b.expert_id)
    if old is None:
        await vault_config.create(user, _subfolder(), expert_name, meta, body)
    else:
        await vault_config.update(user, old.rel, set_=meta, body=body, expected_sha=None)
    return load(user, b.expert_id) or b


async def delete(user: str, expert_id: str) -> None:
    b = load(user, expert_id)
    if b is not None:
        await vault_config.delete(user, b.rel)


def staleness(b: Briefing | None, docs: list, scope_meta: dict) -> dict:
    """What changed since the briefing: new in-scope documents, scope edited."""
    if b is None:
        return {"state": "none", "new": len(docs)}
    new = [d for d in docs if d.id > b.last_doc_id and d.id not in set(b.studied_ids)]
    if b.scope_hash != scope_hash(scope_meta):
        return {"state": "scope_changed", "new": len(new)}
    return {"state": "outdated" if new else "current", "new": len(new)}


# ── Prompt block ──────────────────────────────────────────────────────────────

BRIEFING_HEADER = (
    "Your orientation from studying the documents you can see, earlier. It is a "
    "MAP, not evidence: use it to know the parties, terms and time range and to "
    "write better searches. Never state a fact from it without reading the "
    "document through a tool in this conversation and citing that tool result. "
    "References here are written (#123) and are not citations."
)


def prompt_block(b: Briefing | None, *, ctx_window: int = 0) -> str:
    """The briefing for the system prompt. With a small context window the
    orientation goes first, the map last (it is code-made and short)."""
    if b is None or not b.body.strip():
        return ""
    body = b.body
    if ctx_window and ctx_window < 12_000:
        body = body.split("<!-- pb:orientation -->")[0]
    body = re.sub(r"<!-- pb:\w+ -->\n?", "", body).strip()[:PROMPT_CHARS]
    return f"{BRIEFING_HEADER}\n\n{body}"


def map_only_block(docs_map: dict, _=lambda s: s) -> str:
    """Universal expert without a studied briefing: the code-made map only."""
    if not docs_map or not docs_map.get("total"):
        return ""
    return f"{BRIEFING_HEADER}\n\n{map_markdown(docs_map, _)}"[:PROMPT_CHARS]


# ── Study job ─────────────────────────────────────────────────────────────────

@dataclass
class Job:
    user: str
    expert_id: str
    status: str = "running"  # running | done | failed | stopped
    phase: str = "listing"   # listing | reading | writing | checking
    done: int = 0
    total: int = 0
    error: str = ""
    stop: bool = False
    task: asyncio.Task | None = None


_JOBS: dict[tuple[str, str], Job] = {}


def job(user: str, expert_id: str) -> Job | None:
    return _JOBS.get((user, expert_id))


def stop(user: str, expert_id: str) -> None:
    j = job(user, expert_id)
    if j and j.status == "running":
        j.stop = True


def start(user: str, token: str, expert, model: str, lang: str, *, incremental: bool = False) -> Job:
    """Start (or return the running) study job for an expert."""
    key = (user, expert.expert_id)
    j = _JOBS.get(key)
    if j and j.status == "running":
        return j
    j = Job(user=user, expert_id=expert.expert_id)
    _JOBS[key] = j
    j.task = asyncio.create_task(_run(j, token, expert, model, lang, incremental))
    return j


async def _call(model: str, user: str, token: str, system: str, content: str, schema: dict) -> dict:
    from werkbank import llm_lane

    return await llm_lane.complete_structured(
        system, [{"role": "user", "content": content}], model=model, user_id=user, token=token,
        json_schema=schema, tool_name="output", max_tokens=4000, temperature=0.1,
    )


async def _ensure_host(model: str, user: str, token: str, j: Job) -> None:
    from services.model_registry import get_by_name

    cfg = get_by_name(model, user, token) or {}
    if cfg.get("backend") == "anthropic" or cfg.get("lane") != "local":
        return
    from services.wol import ensure_awake

    outcome = await ensure_awake(cfg.get("base_url", ""), on_wait=lambda s: None, should_stop=lambda: j.stop)
    if outcome == "timeout":
        raise RuntimeError("model host did not wake up")


async def _run(j: Job, token: str, expert, model: str, lang: str, incremental: bool) -> None:
    from i18n import translator_for
    from services.clients import sidecar_service
    from services.expert_scope import scoped_client
    from werkbank import llm_lane

    _ = translator_for(lang)
    llm_lane.yield_to_chat.set(True)  # a chatting user gets the GPU first
    try:
        pl = scoped_client(settings.paperless_url, token, expert.scope)
        docs = await pl.list_documents()
        from pipelines.search import _drop_no_ingest

        docs = _drop_no_ingest(docs)
        old = load(j.user, expert.expert_id) if incremental else None
        dmap = archive_map(docs)
        chosen = select_for_study(docs)
        if old is not None:
            chosen = [d for d in chosen if d.id not in set(old.studied_ids)]
        studied: dict[int, str] = {}
        skipped = 0
        for d in chosen:
            sc = sidecar_service.load_sidecar(d.id)
            if not sc:
                skipped += 1
                continue
            studied[d.id] = material(d, sc)
        batches = [list(studied.items())[i:i + BATCH] for i in range(0, len(studied), BATCH)]
        j.total, j.phase = len(batches) + 1, "reading"
        await _ensure_host(model, j.user, token, j)
        notes: list[dict] = []
        if old is not None:
            # Incremental: the old orientation is the starting point.
            for key in ("parties", "matters", "events", "open_items"):
                for it in old.orientation.get(key) or []:
                    notes.append({"text": (f"{it['date']}: " if it.get("date") else "") + it["text"],
                                  "doc_ids": it.get("doc_ids") or []})
        for batch in batches:
            if j.stop:
                j.status = "stopped"
                return
            content = "\n".join(text for _id, text in batch)
            res = await _call(model, j.user, token, _MAP_SYSTEM.format(lang=lang), content, MAP_SCHEMA)
            for n in res.get("notes") or []:
                if isinstance(n, dict) and n.get("text"):
                    notes.append({"text": str(n["text"])[:300], "doc_ids": n.get("doc_ids") or []})
            j.done += 1
        if j.stop:
            j.status = "stopped"
            return
        j.phase = "writing"
        all_material = dict(studied)
        if old is not None:
            # Old studied documents stay citable: re-read their material.
            for did in old.studied_ids:
                if did not in all_material:
                    d = next((x for x in docs if x.id == did), None)
                    sc = sidecar_service.load_sidecar(did) if d else None
                    if d is not None and sc:
                        all_material[did] = material(d, sc)
        if notes:
            content = (
                "Archive map:\n" + map_markdown(dmap) + "\n\nNotes:\n"
                + "\n".join(f"- {n['text']} " + " ".join(f"#{i}" for i in n["doc_ids"]) for n in notes[:400])
            )
            res = await _call(model, j.user, token, _REDUCE_SYSTEM.format(lang=lang, n=MAX_ITEMS),
                              content, REDUCE_SCHEMA)
        else:
            res = {}
        j.done += 1
        j.phase = "checking"
        orientation, dropped = validate(res, all_material)
        studied_ids = sorted(set(all_material))
        b = Briefing(
            expert_id=expert.expert_id,
            built=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            model=model, studied_ids=studied_ids,
            last_doc_id=max((d.id for d in docs), default=0), total_docs=len(docs),
            skipped=skipped + (old.skipped if old else 0), scope_hash=scope_hash(expert.scope_meta),
            map=dmap, orientation=orientation, dropped=dropped,
        )
        await save(j.user, b, expert.name, _)
        j.status = "done"
    except Exception as exc:  # shown on the page; the old briefing stays
        log.warning("briefing for %s/%s failed", j.user, expert.expert_id, exc_info=True)
        j.status, j.error = "failed", str(exc)[:300]
