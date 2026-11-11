"""A folder of the dossier tree has an overview: what its dossiers are about,
one line per dossier, links to each.

- **The file is `_Overview.md` inside the folder** — Obsidian shows it next to
  the dossiers. It has no `topic`, so it is never read as a dossier, and
  `retrieval.index_meta` (which wants a `topic`) never indexes it: an overview
  is a map of other model text, not evidence.
- **Out of date is computed, never marked**: the note stores a fingerprint of
  what it was built from (each inner dossier's name, state and open
  questions); when the dossiers inside differ, it is stale. A move, a rename,
  a rescan or a new dossier all change it, with no hook in any of them.
- **Built by the worker** (`tasks.topics.folder_overview`) — in the night's
  sweep window for a stale folder, or at once when the user asks for it.
- **The model writes text only.** Which dossiers there are and the links come
  from code; a line for a dossier that is not in the folder is dropped, and so
  is any sentence with a number that is not in what the model was given.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field

from proactive import dossiers as dmod

FILE = "_Overview.md"
MARK = "pbrain_folder_overview"


@dataclass
class Overview:
    built: str = ""
    built_from: str = ""
    lang: str = "en"
    text: str = ""
    lines: dict[str, str] = field(default_factory=dict)  # topic -> one line


def _dir(user: str, folder: str):
    return dmod._root(user) / dmod._safe_folder(folder)


def dossiers_in(user: str, folder: str) -> list[dmod.Dossier]:
    """The dossiers of a folder and its subfolders."""
    return [d for d in dmod.list_dossiers(user) if d.folder == folder or d.folder.startswith(folder + "/")]


def fingerprint(ds: list[dmod.Dossier]) -> str:
    rows = sorted((d.topic, d.display, d.current_state, sorted(d.open_questions)) for d in ds)
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()[:16]


def load(user: str, folder: str) -> Overview | None:
    from vault.note_text import properties, split_note

    try:
        p = _dir(user, folder) / FILE
        text = p.read_text(encoding="utf-8")
    except (OSError, dmod.DossierError):
        return None
    note = split_note(text)
    m = properties(note.fm_raw)
    if not m.get(MARK):
        return None
    lines = m.get("lines") if isinstance(m.get("lines"), dict) else {}
    return Overview(
        built=dmod._day(m.get("built")), built_from=str(m.get("built_from") or ""),
        lang=str(m.get("lang") or "en"), text=str(m.get("overview") or ""),
        lines={str(k): str(v) for k, v in lines.items()},
    )


def is_stale(user: str, folder: str) -> bool:
    """Has dossiers, and no overview built from what they say now."""
    ds = dossiers_in(user, folder)
    if not ds:
        return False
    ov = load(user, folder)
    return ov is None or ov.built_from != fingerprint(ds)


def stale_folders(user: str) -> list[str]:
    return [f for f in dmod.folders(user) if is_stale(user, f)]


def job_key(user: str, folder: str) -> str:
    return f"folder_overview:{user}:{folder}"


def _link(d: dmod.Dossier) -> str:
    target = d.rel[:-3] if d.rel.endswith(".md") else d.rel
    return f"[[{target}|{d.display}]]"


def render_file(folder: str, ov: Overview, ds: list[dmod.Dossier], subfolders: list[str]) -> str:
    """The note as Obsidian shows it; the app reads only the frontmatter."""
    from vault.note_text import dump_mapping

    meta = {MARK: True, "built": ov.built, "built_from": ov.built_from, "lang": ov.lang,
            "overview": ov.text, "lines": dict(ov.lines)}
    body = [f"# {folder.rsplit('/', 1)[-1]}", "", ov.text, ""]
    if ds:
        body += ["## Dossiers", ""]
        for d in sorted(ds, key=lambda x: x.display.casefold()):
            line = ov.lines.get(d.topic, "")
            body.append(f"- {_link(d)}" + (f" — {line}" if line else ""))
        body.append("")
    if subfolders:
        body += ["## Folders", ""] + [f"- {s.rsplit('/', 1)[-1]}" for s in subfolders] + [""]
    body.append("<!-- Written by PaperlessBrain; rebuilt when its dossiers change. -->")
    return f"---\n{dump_mapping(meta)}---\n" + "\n".join(body) + "\n"


async def write(user: str, folder: str, ov: Overview) -> None:
    from vault.locks import get_user_lock
    from vault.paths import atomic_write_text

    folder = dmod._safe_folder(folder)
    ds = dossiers_in(user, folder)
    subs = [f for f in dmod.folders(user) if f.rpartition("/")[0] == folder]
    text = render_file(folder, ov, ds, subs)
    path = _dir(user, folder) / FILE

    async with get_user_lock(user):
        if path.parent.is_dir():
            await asyncio.to_thread(atomic_write_text, path, text)


def remove_files(target) -> None:
    """Overviews under a folder about to be deleted (they are ours, not the
    user's — `delete_folder` keeps a folder holding anything of the user's)."""
    for p in target.rglob(FILE):
        try:
            p.unlink()
        except OSError:
            pass


# ── The model's part ─────────────────────────────────────────────────────

SCHEMA = {
    "type": "object",
    "properties": {
        "overview": {"type": "string"},
        "dossiers": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "line": {"type": "string"}},
            "required": ["id", "line"],
        }},
    },
    "required": ["overview", "dossiers"],
}

SYSTEM = """\
A person keeps dossiers on matters in their documents, sorted into folders. \
Below are the dossiers of one folder. Write: overview — two to five sentences \
on what the folder holds as a whole and what is open or coming up across it; \
dossiers — for each dossier (by its id) one short line saying where it stands. \
Use only what the dossiers say; no advice, no numbers or dates that are not \
in them. Write in {language}.\
"""


def prompt(ds: list[dmod.Dossier]) -> str:
    parts = []
    for d in sorted(ds, key=lambda x: x.display.casefold()):
        last = d.timeline[0] if d.timeline else None
        parts.append(
            f"id: {d.topic}\nname: {d.display}\nfolder: {d.folder}\n"
            f"state: {d.current_state[:1200] or '-'}\n"
            f"open questions: {'; '.join(d.open_questions[:6]) or '-'}\n"
            + (f"latest: {last['date']} — {last['event']}\n" if last else "")
        )
    return "\n".join(parts)


_SENT = re.compile(r"(?<=[.!?])\s+")


def validated(raw: dict, ds: list[dmod.Dossier], given: str) -> tuple[str, dict[str, str]]:
    """The model's text with every sentence dropped that carries a number not
    in `given`, and lines only for dossiers that are in the folder."""
    from services.dossier_merge import numbers_missing

    def clean(text: str) -> str:
        return " ".join(s for s in _SENT.split(str(text or "").strip()) if s and not numbers_missing(s, given))

    topics = {d.topic for d in ds}
    lines = {}
    for item in dmod._as_list(raw.get("dossiers")):
        if isinstance(item, dict) and str(item.get("id")) in topics:
            line = clean(item.get("line"))
            if line:
                lines[str(item["id"])] = line[:300]
    return clean(raw.get("overview"))[:2000], lines
