"""Where a note lives is part of what it means.

A chunk reading "change the brake pads" is ambiguous on its own and unambiguous
under `To-Dos/Car.md`, so the folder, the filename and the heading breadcrumb are
embedded alongside the body text. Same schema for both collections, otherwise
`brain` and `vault` drift apart and a distance from one stops being comparable
to a distance from the other.

Two rules the format follows:

* **One short line.** `multilingual-e5-large-instruct` embeds the whole input,
  so a long header would dominate the vector of a short chunk and make every
  note in the same folder look alike — the opposite of the point.
* **Header for the vector, path for the metadata.** The stored document stays
  the clean chunk text; `folder`/`filename`/`title` go to metadata, where they
  can be filtered and cited without touching the embedding.
"""

from pathlib import Path

# Bump when the embedded text or its metadata schema changes in a way that makes
# old vectors incomparable to new ones. `vault/sync.py` compares this against the
# value recorded per user and reindexes from scratch on a mismatch.
EMBEDDING_SCHEMA_VERSION = 2


def note_title(rel_path: Path | str) -> str:
    """Filename without the .md suffix — the note's human name."""
    return Path(rel_path).stem


def note_folder(rel_path: Path | str, strip_prefix: str = "") -> str:
    """Folder of the note relative to the vault root (or to `strip_prefix`).

    `strip_prefix` exists for brain files: they all sit under the same brain
    subfolder, so keeping it would put an identical token on every single fact —
    pure noise that makes unrelated facts look similar.
    """
    parent = Path(rel_path).parent
    folder = "" if str(parent) == "." else parent.as_posix()
    if strip_prefix:
        prefix = Path(strip_prefix).as_posix().strip("/")
        if folder == prefix:
            folder = ""
        elif folder.startswith(f"{prefix}/"):
            folder = folder[len(prefix) + 1 :]
    return folder


def embed_context(
    rel_path: Path | str, heading_path: str = "", strip_prefix: str = ""
) -> str:
    """The one-line header prepended to a chunk before embedding.

    `To-Dos/Auto.md` + heading "Bremsen" → `[To-Dos] Auto › Bremsen`.
    A note in the vault root has no bracket at all.
    """
    folder = note_folder(rel_path, strip_prefix)
    head = f"[{folder}] " if folder else ""
    trail = f" › {heading_path}" if heading_path else ""
    return f"{head}{note_title(rel_path)}{trail}"


def embed_text(
    body: str, rel_path: Path | str, heading_path: str = "", strip_prefix: str = ""
) -> str:
    """Chunk text with its context header — what actually gets embedded."""
    context = embed_context(rel_path, heading_path, strip_prefix)
    return f"{context}\n\n{body}" if context else body


def path_metadata(rel_path: Path | str, strip_prefix: str = "") -> dict:
    """Location fields stored on every entry of both collections.

    `rel_path` duplicates the existing `path` key on purpose: `path` is load-
    bearing for deletions (git reports a path, the frontmatter is already gone),
    and overloading it with a second meaning invites someone to "clean it up".
    """
    rel = Path(rel_path).as_posix()
    return {
        "rel_path": rel,
        "folder": note_folder(rel_path, strip_prefix),
        "filename": Path(rel_path).name,
        "title": note_title(rel_path),
        "schema_version": EMBEDDING_SCHEMA_VERSION,
    }
