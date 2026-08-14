"""Filesystem CRUD for the vault note editor.

Deliberately narrow: this module reads and writes files, and nothing else. It
never touches Chroma and never commits. Indexing stays deferred to
`vault.sync.sync_user()`, which runs at the start of every chat turn — the
dirty working tree *is* the pending queue, and git batches an editing session
into one commit instead of one per keystroke.

Two rules that are easy to break later:

- **Never call `sync_user`, `reindex_user` or a `VaultBrainWriter` method from
  inside `get_user_lock`.** `asyncio.Lock` is not reentrant (vault/locks.py),
  and all of those take the same lock themselves. That is a deadlock, not an
  error message.
- **Mutations take the lock; reads do not.** Writes are temp+rename, so a
  reader sees either the old file or the new one, never a torn one. The lock is
  not only about the save-hash: `diff_name_status` stages everything with
  `git add -A` before diffing and `_do_sync` commits at the end — a write
  landing between those two would be committed without ever being embedded, and
  would then stay invisible to the index until the file changed again.
"""

import asyncio
import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from vault import frontmatter as fm
from vault.git_wrapper import status_porcelain
from vault.locks import get_user_lock
from vault.paths import (
    VAULT_DIR_MODE,
    align_vault_perms,
    atomic_write_text,
    ensure_vault_dir,
    vault_path,
)

NOTE_SUFFIX = ".md"
# Hidden because they are noise the user never edits: git's own file, macOS
# droppings, crash leftovers from atomic_write_text, and Remotely Save's
# conflict copies (which vault/sync.py also refuses to index).
HIDDEN_NAMES = {".gitignore", ".DS_Store"}
HIDDEN_SUFFIXES = (".tmp", ".conflict.md")
_ILLEGAL_NAME_CHARS = set('\\/:*?"<>|#^[]\0')
_MAX_NAME_BYTES = 200


class VaultPathError(ValueError):
    """A path or name the editor refuses to act on."""


class NoteConflict(Exception):
    """The file on disk is not what the editor last saw."""

    def __init__(
        self,
        kind: Literal["modified", "deleted"],
        disk_text: str | None = None,
        disk_sha: str | None = None,
    ) -> None:
        super().__init__(kind)
        self.kind = kind
        self.disk_text = disk_text
        self.disk_sha = disk_sha


@dataclass(frozen=True)
class NoteSnapshot:
    rel: str
    text: str
    sha: str
    sig: tuple[float, int] | None
    merged: bool = False


# ── Path safety ───────────────────────────────────────────────────────────────


def check_name(name: str) -> str:
    """Validate a single path component (no separators). Returns it trimmed."""
    name = (name or "").strip().rstrip(". ")
    if not name or name in (".", ".."):
        raise VaultPathError("empty name")
    if name.startswith("."):
        raise VaultPathError("names starting with a dot are reserved")
    if _ILLEGAL_NAME_CHARS & set(name):
        raise VaultPathError("name contains an illegal character")
    if len(name.encode("utf-8")) > _MAX_NAME_BYTES:
        raise VaultPathError("name too long")
    return name


def resolve(username: str, rel: str, *, must_exist: bool = False) -> Path:
    """Map a user-supplied relative path to an absolute path inside the vault.

    The single choke point for path safety — every public function here starts
    with it, so the UI layer never builds a Path itself. `resolve()` collapses
    `..` *and* follows symlinks, so one `is_relative_to` check covers traversal
    and symlink escape both. The root is resolved too because VAULT_ROOT is
    itself a mount and may be a link.
    """
    root = vault_path(username).resolve()
    rel = (rel or "").replace("\\", "/").strip("/")
    if "\0" in rel:
        raise VaultPathError("illegal path")
    p = root / rel
    if p.is_symlink():
        # Writing through a link would escape the temp+rename atomicity and
        # surprise the user about which file actually changed.
        raise VaultPathError("symlinks are not editable")
    if must_exist:
        target = p.resolve(strict=True)
    elif p.exists():
        target = p.resolve()
    else:
        try:
            target = p.parent.resolve(strict=True) / p.name
        except (FileNotFoundError, OSError) as exc:
            raise VaultPathError(f"no such folder: {rel}") from exc
    if target != root and not target.is_relative_to(root):
        raise VaultPathError("path escapes the vault")
    return target


def to_rel(username: str, path: Path) -> str:
    return path.relative_to(vault_path(username).resolve()).as_posix()


def is_hidden(name: str) -> bool:
    return (
        name.startswith(".")
        or name in HIDDEN_NAMES
        or name.endswith(HIDDEN_SUFFIXES)
    )


def is_note(rel: str) -> bool:
    return rel.endswith(NOTE_SUFFIX)


# ── Hashing / stat ────────────────────────────────────────────────────────────


def sha_of(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _sig(path: Path) -> tuple[float, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime, st.st_size)


def stat_sig(username: str, rel: str) -> tuple[float, int] | None:
    """Cheap change probe (mtime, size). None when the file is gone.

    Early warning only: a same-second rewrite of identical length is invisible
    here. The hash comparison in save_note() under the lock is the authority.
    """
    try:
        return _sig(resolve(username, rel))
    except VaultPathError:
        return None


# ── Reads (lock-free) ─────────────────────────────────────────────────────────


def _read_sync(username: str, rel: str) -> NoteSnapshot:
    path = resolve(username, rel)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise NoteConflict("deleted") from exc
    return NoteSnapshot(rel=rel, text=raw.decode("utf-8"), sha=sha_of(raw), sig=_sig(path))


async def read_note(username: str, rel: str) -> NoteSnapshot:
    return await asyncio.to_thread(_read_sync, username, rel)


def _node(rel: str, name: str, is_dir: bool, pending: set[str]) -> dict:
    return {
        "id": rel,
        "label": name,
        "dir": is_dir,
        "note": (not is_dir) and is_note(name),
        "pending": rel in pending,
    }


def _scan_dir(root: Path, directory: Path, pending: set[str]) -> list[dict]:
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return []
    dirs, files = [], []
    for entry in entries:
        if is_hidden(entry.name):
            continue
        rel = Path(entry.path).relative_to(root).as_posix()
        try:
            entry_is_dir = entry.is_dir(follow_symlinks=False)
        except OSError:
            continue
        node = _node(rel, entry.name, entry_is_dir, pending)
        if entry_is_dir:
            node["children"] = _scan_dir(root, Path(entry.path), pending)
            dirs.append(node)
        else:
            files.append(node)
    dirs.sort(key=lambda n: n["label"].casefold())
    files.sort(key=lambda n: n["label"].casefold())
    return dirs + files


def _tree_sync(username: str, pending: set[str] | None) -> list[dict]:
    root = vault_path(username)
    if not root.is_dir():
        return []
    return _scan_dir(root.resolve(), root.resolve(), pending or set())


async def list_tree(username: str, pending: set[str] | None = None) -> list[dict]:
    """Folder/file nodes for ui.tree. Node id is the vault-relative path."""
    return await asyncio.to_thread(_tree_sync, username, pending)


async def pending_rel_paths(username: str) -> set[str]:
    """Paths changed since the last sync commit — i.e. not indexed yet."""
    return await asyncio.to_thread(status_porcelain, vault_path(username))


def _find_by_id_sync(username: str, pbrain_id: str) -> str | None:
    root = vault_path(username).resolve()
    if not pbrain_id or not root.is_dir():
        return None
    for path in root.rglob(f"*{NOTE_SUFFIX}"):
        if is_hidden(path.name):
            continue
        try:
            meta, _body = fm.read(path)
        except OSError:
            continue
        if fm.get_id(meta) == pbrain_id:
            return path.relative_to(root).as_posix()
    return None


async def find_by_pbrain_id(username: str, pbrain_id: str) -> str | None:
    """Locate a note by its stable id — how the editor follows an external move."""
    return await asyncio.to_thread(_find_by_id_sync, username, pbrain_id)


# ── Mutations (each takes the per-user lock) ──────────────────────────────────


def _save_sync(
    username: str,
    rel: str,
    text: str,
    expected_sha: str | None,
    base_text: str | None,
) -> NoteSnapshot:
    from vault.note_text import try_merge  # local: keeps this module import-light

    path = resolve(username, rel)
    try:
        current = path.read_bytes()
    except FileNotFoundError as exc:
        raise NoteConflict("deleted") from exc

    current_sha = sha_of(current)
    if expected_sha is None or current_sha == expected_sha:
        atomic_write_text(path, text)
        return NoteSnapshot(rel, text, sha_of(text), _sig(path))

    disk_text = current.decode("utf-8")
    if base_text is not None:
        merged = try_merge(base_text, text, disk_text)
        if merged is not None:
            atomic_write_text(path, merged)
            return NoteSnapshot(rel, merged, sha_of(merged), _sig(path), merged=True)
    raise NoteConflict("modified", disk_text, current_sha)


async def save_note(
    username: str,
    rel: str,
    text: str,
    *,
    expected_sha: str | None,
    base_text: str | None = None,
) -> NoteSnapshot:
    """Write a note, refusing to clobber an unseen change.

    `expected_sha` is the hash the editor last saw on disk; None forces the
    write (the conflict dialog's "Overwrite"). When the hash no longer matches
    and `base_text` is given, a three-way merge is attempted first — that is
    what makes sync's own `pbrain_id` / `dont_ingest` writes invisible instead
    of alarming.
    """
    async with get_user_lock(username):
        return await asyncio.to_thread(
            _save_sync, username, rel, text, expected_sha, base_text
        )


def _create_note_sync(username: str, parent_rel: str, name: str) -> str:
    name = check_name(name)
    if not name.endswith(NOTE_SUFFIX):
        name += NOTE_SUFFIX
    parent = resolve(username, parent_rel, must_exist=True)
    if not parent.is_dir():
        raise VaultPathError("parent is not a folder")
    path = resolve(username, f"{to_rel(username, parent)}/{name}".lstrip("/"))
    if path.exists():
        raise VaultPathError("a note with that name already exists")
    # Empty on purpose: the next sync assigns pbrain_id and dont_ingest, and
    # that is the only place identity is minted.
    atomic_write_text(path, "")
    return to_rel(username, path)


async def create_note(username: str, parent_rel: str, name: str) -> str:
    async with get_user_lock(username):
        return await asyncio.to_thread(_create_note_sync, username, parent_rel, name)


def _create_folder_sync(username: str, parent_rel: str, name: str) -> str:
    name = check_name(name)
    parent = resolve(username, parent_rel, must_exist=True)
    if not parent.is_dir():
        raise VaultPathError("parent is not a folder")
    path = resolve(username, f"{to_rel(username, parent)}/{name}".lstrip("/"))
    if path.exists():
        raise VaultPathError("a folder with that name already exists")
    ensure_vault_dir(path)
    return to_rel(username, path)


async def create_folder(username: str, parent_rel: str, name: str) -> str:
    async with get_user_lock(username):
        return await asyncio.to_thread(_create_folder_sync, username, parent_rel, name)


def _rename_sync(username: str, rel: str, new_name: str) -> str:
    new_name = check_name(new_name)
    path = resolve(username, rel, must_exist=True)
    if path.is_file() and is_note(path.name) and not new_name.endswith(NOTE_SUFFIX):
        new_name += NOTE_SUFFIX
    target = resolve(username, f"{to_rel(username, path.parent)}/{new_name}".lstrip("/"))
    if target == path:
        return rel
    if target.exists():
        raise VaultPathError("that name is already taken")
    path.rename(target)
    if target.is_dir():
        align_vault_perms(target, mode=VAULT_DIR_MODE)
    else:
        align_vault_perms(target)
    return to_rel(username, target)


async def rename(username: str, rel: str, new_name: str) -> str:
    async with get_user_lock(username):
        return await asyncio.to_thread(_rename_sync, username, rel, new_name)


def _move_sync(username: str, rel: str, new_parent_rel: str) -> str:
    path = resolve(username, rel, must_exist=True)
    parent = resolve(username, new_parent_rel, must_exist=True)
    if not parent.is_dir():
        raise VaultPathError("target is not a folder")
    if path == parent or parent.is_relative_to(path):
        raise VaultPathError("cannot move a folder into itself")
    target = parent / path.name
    if target.exists():
        raise VaultPathError("that name is already taken in the target folder")
    path.rename(target)
    return to_rel(username, target)


async def move(username: str, rel: str, new_parent_rel: str) -> str:
    async with get_user_lock(username):
        return await asyncio.to_thread(_move_sync, username, rel, new_parent_rel)


def _delete_sync(username: str, rel: str, recursive: bool) -> None:
    path = resolve(username, rel, must_exist=True)
    if path == vault_path(username).resolve():
        raise VaultPathError("cannot delete the vault root")
    if path.is_dir():
        if recursive:
            shutil.rmtree(path)
        else:
            path.rmdir()  # raises if not empty — the UI asks before recursing
    else:
        path.unlink()


async def delete(username: str, rel: str, *, recursive: bool = False) -> None:
    """Remove a note or folder. Chroma is untouched: the next sync sees `D`
    and runs its delete-by-path (vault/sync.py)."""
    async with get_user_lock(username):
        await asyncio.to_thread(_delete_sync, username, rel, recursive)
