"""Every note of a vault, read once and kept while unchanged.

Content search (`vault/search.py`), backlinks and link rewriting
(`vault/links.py`) all read every note. Vaults are hundreds to low thousands
of small files, so a linear scan is fine — with the text cached per file by
(mtime, size), a repeat scan costs one `stat` per file. No index, no manifest:
the vault's do-not list forbids a second change detector, and this is only a
read cache that a changed stat invalidates.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from vault.notes import NOTE_SUFFIX, is_hidden
from vault.paths import vault_path

_CACHE: dict[str, tuple[tuple[int, int], str]] = {}  # abs path → (stat sig, text)
_LOCK = threading.Lock()
MAX_BYTES = 2_000_000  # a "note" larger than this is not read


def note_paths(username: str) -> list[str]:
    """Vault-relative paths of every note, hidden folders skipped, sorted."""
    root = vault_path(username)
    if not root.is_dir():
        return []
    root = root.resolve()
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not is_hidden(d)]
        for name in filenames:
            if name.endswith(NOTE_SUFFIX) and not is_hidden(name):
                out.append(Path(dirpath, name).relative_to(root).as_posix())
    return sorted(out, key=str.casefold)


def other_files(username: str) -> dict[str, str]:
    """Attachments by lower-case file name → vault-relative path (embeds)."""
    root = vault_path(username)
    if not root.is_dir():
        return {}
    root = root.resolve()
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not is_hidden(d)]
        for name in filenames:
            if not name.endswith(NOTE_SUFFIX) and not is_hidden(name):
                out.setdefault(name.lower(), Path(dirpath, name).relative_to(root).as_posix())
    return out


def read_text(username: str, rel: str) -> str:
    path = vault_path(username).resolve() / rel
    try:
        st = path.stat()
    except OSError:
        return ""
    sig = (st.st_mtime_ns, st.st_size)
    key = str(path)
    with _LOCK:
        hit = _CACHE.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    if st.st_size > MAX_BYTES:
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    with _LOCK:
        _CACHE[key] = (sig, text)
    return text


def iter_notes(username: str):
    """(rel, text) for every note."""
    for rel in note_paths(username):
        yield rel, read_text(username, rel)
