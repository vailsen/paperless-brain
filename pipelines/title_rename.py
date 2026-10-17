# pipelines/title_rename.py
"""Give documents a title built from a user template, after AI analysis.

Paperless-ngx does not let an app set a file name — `original_file_name` and
`archived_file_name` are read-only. What it does is re-derive the stored file
name from its filename format on every save, so with
`PAPERLESS_FILENAME_FORMAT={{ title }}` the title *is* the file name, and
setting it is what renames the file.

Who fills which placeholder is deliberate. Date and correspondent come from
Paperless, so they cannot be wrong and always match what Paperless shows. The
model supplies only what Paperless does not know: the subject, and the document
type when Paperless has none.

A document is renamed once. The record of it lives in its own file, not in the
sidecar: re-ingesting a document rewrites its sidecar, and a record kept there
would make a renamed document eligible again — overwriting whatever the user
has since typed into the title.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable

from services.paperless import PaperlessClient
from services.sidecar_service import SidecarService

PLACEHOLDERS = ("year", "month", "day", "doc_type", "description", "correspondent")
DEFAULT_TEMPLATE = "{year}_{month}_{day}_{doc_type}_{description}_{correspondent}"
#: Paperless' `Document.title` is a CharField(max_length=128).
MAX_TITLE_LENGTH = 128
#: How many documents the dry run names before the user decides.
PREVIEW_SIZE = 10
#: Bumped whenever the naming prompt changes, so answers cached under an older
#: prompt are asked again instead of reused.
DESCRIPTION_VERSION = 2
#: The description is part of a file name, so it is kept short however chatty
#: the model is: at most this many words, at most this many characters.
MAX_DESCRIPTION_WORDS = 3
MAX_DESCRIPTION_CHARS = 40
#: A single word longer than this is cut. High enough that ordinary German
#: compounds ("Nebenkostenabrechnung", 21) survive whole — a word cut in the
#: middle looks broken — and only the 30-character monsters are trimmed.
MAX_WORD_CHARS = 24

# Words that carry no meaning in a file name, in both archive languages.
# Matched case-insensitively against whole words only.
FILLER_WORDS = frozenset("""
    a an and or of the for with to in on at by from per via about as
    und oder der die das des dem den ein eine einer eines einem einen
    für mit von vom zu zum zur im am an auf aus bei über unter nach sowie bzw
""".split())

STATE_FILENAME = "title_renames.json"

# Characters a file system or Paperless' path template would choke on. A "/" in
# a title would turn into a directory under a {{ title }} filename format.
_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
_SEPARATORS = " _.-"
_EMPTY = "\x00"

Describer = Callable[[str], Awaitable[tuple[str, str]]]
Log = Callable[[str], None]


@dataclass(frozen=True)
class RenamePlan:
    doc_id: int
    old_title: str
    new_title: str


# ── Rendering ─────────────────────────────────────────────────────────────────


def clean_value(text: str | None) -> str:
    """One placeholder value, safe inside a file name."""
    text = _ILLEGAL.sub(" ", text or "")
    text = re.sub(r"\s+", " ", text).strip(_SEPARATORS)
    return text


def render_title(
    template: str,
    *,
    created: date | datetime | None,
    doc_type: str = "",
    description: str = "",
    correspondent: str = "",
) -> str:
    """Fill the template. An empty value takes one separator with it.

    `{year}_{month}_{day}_{doc_type}_{description}_{correspondent}` for a
    document without a correspondent gives `2026_10_17_Invoice_CPU`, not
    `2026_10_17_Invoice_CPU_`. Unknown placeholders stay as typed, so a typo
    shows up in the preview instead of vanishing.
    """
    values = {
        "year": f"{created:%Y}" if created else "",
        "month": f"{created:%m}" if created else "",
        "day": f"{created:%d}" if created else "",
        "doc_type": clean_value(doc_type),
        "description": clean_value(description),
        "correspondent": clean_value(correspondent),
    }

    def _fill(match: re.Match) -> str:
        key = match.group(1)
        if key not in values:
            return match.group(0)
        return values[key] or _EMPTY

    out = _PLACEHOLDER_RE.sub(_fill, template or "")
    # "a_<empty>_b" keeps one separator, "<empty>_b" and "a_<empty>" keep none.
    out = re.sub(
        r"([ _.\-]*)\x00([ _.\-]*)",
        lambda m: m.group(1) if (m.group(1) and m.group(2)) else "",
        out,
    )
    out = _ILLEGAL.sub(" ", out)
    out = re.sub(r"\s+", " ", out).strip(_SEPARATORS)
    if len(out) > MAX_TITLE_LENGTH:
        out = out[:MAX_TITLE_LENGTH].rstrip(_SEPARATORS)
    return out


def _words(text: str | None) -> list[str]:
    cleaned = clean_value(text)
    words = [w.strip(_SEPARATORS + ",;") for w in re.split(r"[\s_,;/+&]+", cleaned)]
    return [w for w in words if w]


def compact_description(
    text: str | None,
    max_words: int = MAX_DESCRIPTION_WORDS,
    exclude: str = "",
) -> str:
    """The model's description, made fit for a file name.

    Local models answer "Rechnung für CPU, Grafikkarte und RAM" to a request for
    three words. The prompt asks for less; this enforces it, because a long file
    name is what the user sees and a prompt is only a request:

    - filler words ("and", "of", "für", "und" …) are dropped,
    - at most `max_words` words remain, joined with "_",
    - a word over `MAX_WORD_CHARS` is cut, and trailing words are dropped while
      the whole is longer than `MAX_DESCRIPTION_CHARS`.

    Hyphenated compounds ("Kfz-Versicherung") count as one word.

    Words already in `exclude` — the document type and the correspondent, which
    the template places next to the description — are dropped too: a model
    asked about "Rechnung für CPU" from Amazon otherwise produces
    `Rechnung_Rechnung_CPU_…_Amazon`. If that would leave nothing, they stay.
    """
    words = [w for w in _words(text) if w.casefold() not in FILLER_WORDS]
    taken = {w.casefold() for w in _words(exclude)}
    if taken and (kept := [w for w in words if w.casefold() not in taken]):
        words = kept
    words = [w[:MAX_WORD_CHARS] for w in words[:max_words]]
    while len(words) > 1 and len("_".join(words)) > MAX_DESCRIPTION_CHARS:
        words.pop()
    return "_".join(words)


# ── Rename record ─────────────────────────────────────────────────────────────

_STATE_LOCK = asyncio.Lock()


def default_state_path() -> Path:
    from config.settings import settings

    return Path(settings.app_path) / "data" / STATE_FILENAME


def load_state(path: Path) -> dict:
    """`{"done": {id: {from, to, at}}, "described": {id: {doc_type, subject}}}`."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("done", {})
    data.setdefault("described", {})
    return data


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


async def _record(path: Path, section: str, doc_id: int, value: dict) -> None:
    """Read-modify-write under one lock — a running backfill and the sync's
    own rename step may both be writing."""
    async with _STATE_LOCK:
        state = load_state(path)
        state[section][str(doc_id)] = value
        _save_state(path, state)


# ── Planning and applying ─────────────────────────────────────────────────────


async def pending_documents(
    paperless: PaperlessClient,
    sidecar_service: SidecarService,
    *,
    doc_ids: list[int] | None = None,
    path: Path | None = None,
) -> list:
    """Documents the user can see that were analysed and never renamed."""
    if doc_ids is not None and not doc_ids:
        return []      # an empty id filter would otherwise mean "all documents"
    done = load_state(path or default_state_path())["done"]
    docs = await paperless.list_documents(ids=doc_ids) if doc_ids else await paperless.list_documents()
    pending = [
        d for d in docs
        if str(d.id) not in done and sidecar_service.load_sidecar(d.id)
    ]
    return sorted(pending, key=lambda d: d.id)


async def iter_plans(
    documents: list,
    sidecar_service: SidecarService,
    describe: Describer,
    template: str,
    *,
    path: Path | None = None,
    log: Log | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> AsyncIterator[RenamePlan]:
    """One plan per document that could be named; the rest are logged.

    The model's answer is cached before anything is renamed, so the dry run's
    calls are not repeated by the real run, and a stopped run loses nothing.
    """
    path = path or default_state_path()

    def _log(msg: str) -> None:
        if log is not None:
            log(msg)

    described = load_state(path)["described"]
    for doc in documents:
        if should_stop is not None and should_stop():
            return
        cached = described.get(str(doc.id))
        if cached is not None and cached.get("v") != DESCRIPTION_VERSION:
            cached = None          # named under an older prompt — ask again
        if cached is None:
            sidecar = sidecar_service.load_sidecar(doc.id) or {}
            summary = sidecar.get("full_summary_summarized") or sidecar.get("full_summary") or ""
            if not summary.strip():
                _log(f"  – #{doc.id}: no summary to name it from — skipped")
                continue
            try:
                ai_type, subject = await describe(summary)
            except Exception as exc:  # noqa: BLE001 — one bad document must not stop the run
                _log(f"  ✗ #{doc.id}: naming failed: {exc}")
                continue
            cached = {"doc_type": ai_type, "subject": subject, "v": DESCRIPTION_VERSION}
            await _record(path, "described", doc.id, cached)
        # The user's own Paperless type is kept as written; the model's is
        # compacted like the description.
        doc_type = doc.document_type or compact_description(cached.get("doc_type"), 2)
        subject = compact_description(
            cached.get("subject"),
            exclude=f"{doc_type} {doc.correspondent or ''}",
        )
        if not subject:
            # A title without its subject is worse than the one it would
            # replace. Not cached as done, so a later run can try again.
            _log(f"  – #{doc.id}: the model gave no usable subject — skipped")
            continue
        new_title = render_title(
            template,
            created=doc.created,
            doc_type=doc_type,
            description=subject,
            correspondent=doc.correspondent or "",
        )
        if not new_title:
            _log(f"  – #{doc.id}: the template produced an empty title — skipped")
            continue
        yield RenamePlan(doc.id, doc.title, new_title)


async def preview_renames(
    paperless: PaperlessClient,
    sidecar_service: SidecarService,
    describe: Describer,
    template: str,
    *,
    limit: int = PREVIEW_SIZE,
    path: Path | None = None,
    log: Log | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[list[RenamePlan], int]:
    """Dry run: up to `limit` plans and the number of documents pending.

    Nothing is written to Paperless. Only the model's answers are cached.
    """
    pending = await pending_documents(paperless, sidecar_service, path=path)
    sample = pending[:limit]
    plans: list[RenamePlan] = []
    seen = 0
    async for plan in iter_plans(sample, sidecar_service, describe, template, path=path, log=log):
        plans.append(plan)
        seen += 1
        if progress is not None:
            progress(seen, len(sample))
    return plans, len(pending)


async def rename_documents(
    paperless: PaperlessClient,
    sidecar_service: SidecarService,
    describe: Describer,
    template: str,
    *,
    doc_ids: list[int] | None = None,
    path: Path | None = None,
    log: Log | None = None,
    progress: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[int, int]:
    """Name and rename every pending document (or only `doc_ids`).

    Returns (renamed, failed). Each document is written as soon as it is named,
    so a long backfill makes progress that survives a stop or a restart.

    `paperless` must be the client of the user who asked for it: the PATCH needs
    write permission on each document, and the superuser client would rename
    documents that user cannot even see.
    """
    path = path or default_state_path()

    def _log(msg: str) -> None:
        if log is not None:
            log(msg)

    pending = await pending_documents(paperless, sidecar_service, doc_ids=doc_ids, path=path)
    if not pending:
        return 0, 0
    renamed = failed = handled = 0
    async for plan in iter_plans(
        pending, sidecar_service, describe, template,
        path=path, log=log, should_stop=should_stop,
    ):
        if plan.new_title != plan.old_title:
            try:
                await paperless.update_document_title(plan.doc_id, plan.new_title)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                _log(f"  ✗ #{plan.doc_id}: {exc}")
                continue
        await _record(path, "done", plan.doc_id, {
            "from": plan.old_title,
            "to": plan.new_title,
            "at": datetime.now(timezone.utc).isoformat(),
        })
        renamed += 1
        handled += 1
        _log(f"  ✓ #{plan.doc_id}: {plan.old_title} → {plan.new_title}")
        if progress is not None:
            progress(handled, len(pending))
    return renamed, failed
