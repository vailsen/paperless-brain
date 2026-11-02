"""Markdown configuration files in a user's vault (experts, skills).

They live in the vault so the user owns them and can edit them in Obsidian,
but they are settings, not knowledge: `vault.sync._embed_file` skips their
folders (`vault.router.is_config_path`), so no `pbrain_id` is minted and
nothing is embedded. Their identity is a key of their own (`expert_id`,
`skill_id`), written by the code that creates the file.

Writes go through `vault.notes.save_note` (user lock, hash check, three-way
merge), creation through an atomic write under the same lock.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path

from vault.locks import get_user_lock
from vault.note_text import apply_edits, dump_mapping, join_note, properties, split_note
from vault.notes import read_note, save_note, sha_of
from vault.paths import atomic_write_text, ensure_vault_dir, vault_path

_UNSAFE = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]')


@dataclass
class ConfigFile:
    meta: dict
    body: str
    rel: str
    sha: str
    text: str


def safe_filename(name: str, fallback: str = "untitled") -> str:
    clean = " ".join(_UNSAFE.sub(" ", name or "").split()).strip(". ")
    return (clean or fallback)[:80]


def folder(user: str, subfolder: str) -> Path:
    return vault_path(user) / subfolder


def parse(text: str, rel: str) -> ConfigFile:
    note = split_note(text)
    return ConfigFile(properties(note.fm_raw), note.body, rel, sha_of(text), text)


def read_file(user: str, path: Path) -> ConfigFile | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        rel = path.relative_to(vault_path(user)).as_posix()
    except ValueError:
        rel = path.name
    return parse(text, rel)


def list_files(user: str, subfolder: str) -> list[ConfigFile]:
    d = folder(user, subfolder)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.md")):
        if p.name.endswith(".conflict.md") or p.is_symlink():
            continue
        f = read_file(user, p)
        if f is not None:
            out.append(f)
    return out


def render(meta: dict, body: str) -> str:
    fm = dump_mapping(meta) or ""
    body = (body or "").strip("\n")
    return f"---\n{fm}---\n{body}\n" if body else f"---\n{fm}---\n"


def _free_path(d: Path, stem: str) -> Path:
    p = d / f"{stem}.md"
    n = 2
    while p.exists():
        p = d / f"{stem} {n}.md"
        n += 1
    return p


async def create(user: str, subfolder: str, stem: str, meta: dict, body: str) -> ConfigFile:
    d = folder(user, subfolder)

    def _write() -> Path:
        ensure_vault_dir(vault_path(user))
        ensure_vault_dir(d)
        p = _free_path(d, safe_filename(stem))
        atomic_write_text(p, render(meta, body))
        return p

    async with get_user_lock(user):
        p = await asyncio.to_thread(_write)
    f = read_file(user, p)
    assert f is not None
    return f


async def update(
    user: str, rel: str, *, set_: dict, remove: tuple[str, ...] = (), body: str | None = None,
    expected_sha: str | None,
) -> ConfigFile:
    """Change keys (others keep their source lines) and optionally the body.
    Raises `vault.notes.NoteConflict` when the file changed and cannot merge."""
    snap = await read_note(user, rel)
    note = split_note(snap.text)
    fm = apply_edits(note.fm_raw, set_=set_, remove=remove)
    new_body = note.body if body is None else (body.strip("\n") + "\n")
    text = join_note(type(note)(fm, new_body, note.open_fence or "---\n", note.close_fence or "---\n"))
    base = snap.text if expected_sha is not None else None
    await save_note(user, rel, text, expected_sha=expected_sha or snap.sha, base_text=base)
    return parse(text, rel)


async def rename(user: str, rel: str, stem: str) -> str:
    """Move the file to a name matching its display name, if that name is
    free. Identity is the id key, so a failed rename changes nothing."""
    src = vault_path(user) / rel

    def _mv() -> str:
        target = src.parent / f"{safe_filename(stem)}.md"
        if target == src or target.exists() or not src.exists():
            return rel
        src.rename(target)
        return target.relative_to(vault_path(user)).as_posix()

    async with get_user_lock(user):
        return await asyncio.to_thread(_mv)


async def delete(user: str, rel: str) -> None:
    p = vault_path(user) / rel

    def _rm() -> None:
        try:
            p.unlink()
        except FileNotFoundError:
            pass

    async with get_user_lock(user):
        await asyncio.to_thread(_rm)
