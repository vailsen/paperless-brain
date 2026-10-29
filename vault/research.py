"""Research reports as notes in the user's vault — `PaperlessBrain Research/`.

Every finished Werkbank run — started in the Werkbank, handed over from the
chat, or the proactive deep dive behind "Interesting" — is written here as one
Markdown note. The note is the lasting copy of the report: deleting the run in
the Werkbank removes its board and facts, never the note. Nothing goes back
into Paperless.

It is an ordinary vault note, indexed by the same `sync_user` a chat turn
runs — right after it is written (`index`), so the next chat question does
not start by embedding a long report, and `vault_search` finds it at once.
`pbrain_id` is minted by that sync, not here.

`werkbank_run` in the frontmatter is how a run, and a finding, find their note
again — whatever the user renamed or moved it to inside the folder.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path

log = logging.getLogger("vault.research")

RUN_KEY = "werkbank_run"
# Anything illegal in a filename on Windows/macOS/Linux, plus Obsidian's link
# and tag characters (same class as the memo writer's).
_ILLEGAL = re.compile(r'[\\/:*?"<>|#^\[\]]')
_WIKILINK = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


def research_rel() -> str:
    from config.settings import settings

    return settings.research_subfolder


def research_path(user: str) -> Path:
    from vault.paths import vault_path

    return vault_path(user) / research_rel()


def _slug(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"\s+", " ", _ILLEGAL.sub("", text)).strip(" .,-")
    if len(slug) > max_len:
        slug = slug[:max_len].rsplit(" ", 1)[0].rstrip(" .,-")
    return slug or "Research"


def wikilink(rel: str, label: str = "") -> str:
    """`[[PaperlessBrain Research/26-10-29 Riester|label]]` — Obsidian resolves
    a path without `.md` from the vault root."""
    target = rel[:-3] if rel.endswith(".md") else rel
    return f"[[{target}|{label}]]" if label else f"[[{target}]]"


def wikilink_target(target: str) -> str:
    """The vault-relative path a wikilink points to (`.md` added)."""
    target = target.strip()
    return target if target.endswith(".md") else target + ".md"


def strip_wikilinks(text: str) -> str:
    """`[[a/b|label]]` → `label`, `[[a/b]]` → `b` — for the PDF, where a
    vault link leads nowhere."""
    return _WIKILINK.sub(lambda m: m.group(2) or m.group(1).rsplit("/", 1)[-1], text)


def _notes(user: str) -> list[Path]:
    d = research_path(user)
    if not d.is_dir():
        return []
    return sorted(p for p in d.rglob("*.md") if not p.name.endswith(".conflict.md") and not p.name.startswith("."))


def _find_sync(user: str, run_id: str) -> str | None:
    from vault.frontmatter import read
    from vault.paths import vault_path

    for p in _notes(user):
        try:
            meta, _ = read(p)
        except Exception:  # noqa: BLE001 — a broken note is not ours to judge
            continue
        if str(meta.get(RUN_KEY) or "") == run_id:
            return p.relative_to(vault_path(user)).as_posix()
    return None


async def find_by_run(user: str, run_id: str) -> str | None:
    if not run_id:
        return None
    return await asyncio.to_thread(_find_sync, user, run_id)


def render(*, run_id: str, goal: str, request: str, model: str, report_md: str,
           created: datetime, extra: dict | None = None) -> str:
    import yaml

    meta = {
        "source": "research",
        RUN_KEY: run_id,
        "created": created.isoformat(timespec="seconds"),
        "model": model,
        # Like every vault note: opt-out-able from Obsidian (vault/sync.py).
        "dont_ingest": False,
        "tags": ["research"],
        **(extra or {}),
    }
    head = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    body = report_md.strip()
    if not body.lstrip().startswith("# "):
        body = f"# {goal}\n\n{body}"
    if request and request.strip() != goal.strip():
        # The question asked, kept with the answer — the report alone does not say it.
        quoted = "\n".join("> " + line for line in request.strip().splitlines())
        body = body.replace("\n", f"\n\n{quoted}\n", 1) if "\n" in body else f"{body}\n\n{quoted}"
    return f"---\n{head}\n---\n\n{body}\n"


def _save_sync(user: str, run_id: str, text: str, goal: str, created: datetime) -> str:
    from vault.notes import resolve
    from vault.paths import atomic_write_text, ensure_vault_dir, vault_path

    existing = _find_sync(user, run_id)
    if existing:
        path = resolve(user, existing)
        if path.read_text(encoding="utf-8") != text:
            # The same run finished again (resumed after a failure): its note,
            # under the name the user may have given it.
            atomic_write_text(path, text)
        return existing
    directory = research_path(user)
    ensure_vault_dir(directory)
    stem = f"{created.strftime('%y-%m-%d')} {_slug(goal)}"
    path, i = directory / f"{stem}.md", 1
    while path.exists():
        path, i = directory / f"{stem}-{i}.md", i + 1
    atomic_write_text(path, text)
    return path.relative_to(vault_path(user)).as_posix()


async def save(user: str, run_id: str, *, goal: str, request: str, model: str, report_md: str,
               created: datetime | None = None, extra: dict | None = None) -> str:
    """Write (or rewrite) the run's note; returns its vault-relative path.
    Under the user's vault lock, like every vault write."""
    from config.settings import local_tz
    from vault.locks import get_user_lock

    created = created or datetime.now(tz=local_tz())
    text = render(run_id=run_id, goal=goal, request=request, model=model, report_md=report_md,
                  created=created, extra=extra)
    async with get_user_lock(user):
        return await asyncio.to_thread(_save_sync, user, run_id, text, goal, created)


async def save_run(user: str, run_id: str, extra: dict | None = None) -> str | None:
    """The note for a finished run from the Werkbank store — what the pipeline
    calls after the report, and the backfill for runs older than this folder.
    None when the run has no report."""
    from werkbank.v2 import store

    report = store.load_report(run_id, user)
    state = store.load_state(run_id, user)
    if not report or state is None:
        return None
    brief = state.brief
    goal = (brief.goal if brief else "") or run_id
    created = None
    if getattr(state, "finished_at", None):
        try:
            from config.settings import local_tz

            created = datetime.fromisoformat(state.finished_at).astimezone(local_tz())
        except ValueError:
            created = None
    return await save(user, run_id, goal=goal, request=(brief.original_request if brief else ""),
                      model=state.model or "", report_md=report, created=created, extra=extra)


async def note_for_run(user: str, run_id: str) -> str | None:
    """The run's note — written now from the store if the run predates the
    folder. None when neither exists (the run was deleted before)."""
    rel = await find_by_run(user, run_id)
    if rel:
        return rel
    try:
        rel = await save_run(user, run_id)
    except Exception:  # noqa: BLE001
        log.warning("%s: could not write the note for run %s", user, run_id, exc_info=True)
        return None
    if rel:
        await index(user)
    return rel


async def index(user: str) -> None:
    """Embed what was just written, now — the sync a chat turn would otherwise
    run first, with the user waiting. Never fails the caller: the note is on
    disk, and the next sync indexes it anyway."""
    try:
        from vault.sync import sync_user

        await sync_user(user, force=True)
    except Exception:  # noqa: BLE001
        log.warning("%s: research note not indexed yet", user, exc_info=True)


async def backfill(user: str) -> int:
    """Every finished run of the user that has no note yet gets one. Returns
    how many were written; cheap when there is nothing to do."""
    from werkbank.v2 import store

    done = [r["run_id"] for r in store.list_runs(user) if r.get("status") == "done"]
    if not done:
        return 0
    written = 0
    known = await asyncio.to_thread(_known_runs, user)
    for run_id in done:
        if run_id in known:
            continue
        try:
            if await save_run(user, run_id):
                written += 1
        except Exception:  # noqa: BLE001
            log.warning("%s: could not write the note for run %s", user, run_id, exc_info=True)
    if written:
        await index(user)
    return written


def _known_runs(user: str) -> set[str]:
    from vault.frontmatter import read

    out = set()
    for p in _notes(user):
        try:
            meta, _ = read(p)
        except Exception:  # noqa: BLE001
            continue
        if meta.get(RUN_KEY):
            out.add(str(meta[RUN_KEY]))
    return out


def read_note(user: str, rel: str) -> tuple[dict, str]:
    """(frontmatter, body) of a note in the research folder. Raises
    FileNotFoundError for anything else — the PDF route serves only these."""
    from vault.frontmatter import read
    from vault.notes import resolve

    root = research_rel().strip("/") + "/"
    if not rel.startswith(root):
        raise FileNotFoundError(rel)
    path = resolve(user, rel, must_exist=True)
    return read(path)
