"""Voice-memo write primitive.

Same shape as ``vault/brain_writer.py`` — file → embed → commit under the
per-user lock — but targets the memo subfolder and the **vault** collection.
Memos are the user's own words, so they are chunked like any other vault note
and are found by ``vault_search``, not ``brain_search``.

Indexing is done **inline**, not delegated to ``vault.sync``. ``sync_user()``
detects work via ``git diff --name-status HEAD`` and commits last, so a file
that is already committed is invisible to it. Writing, embedding and committing
here in one step keeps a fresh memo searchable immediately and leaves nothing
for the next sync to redo.
"""

import re
import uuid
from datetime import datetime
from pathlib import Path

from vault.chunker import chunk_vault_file
from vault.frontmatter import write
from vault.git_wrapper import _git, commit, ensure_repo
from vault.locks import get_user_lock
from vault.paths import ensure_memo_dir, memo_path, vault_path

# Same character class brain_writer strips — anything illegal in a filename on
# Windows/macOS/Linux, plus the Obsidian link/tag characters.
_ILLEGAL = re.compile(r'[\\/:*?"<>|#^\[\]]')


def _slug(text: str, max_len: int = 48) -> str:
    """Filename-safe topic. Shorter cap than brain_writer's because the date and
    time prefix already consumes 15 characters."""
    slug = _ILLEGAL.sub("", text)
    slug = re.sub(r"\s+", " ", slug).strip(" .,-")
    if len(slug) > max_len:
        slug = slug[:max_len].rsplit(" ", 1)[0].rstrip(" .,-")
    return slug


def _unique_path(directory: Path, stem: str) -> Path:
    """`stem`.md, or `stem`-N.md if taken. Two memos in the same minute on the
    same topic must not overwrite each other."""
    candidate = directory / f"{stem}.md"
    if not candidate.exists():
        return candidate
    i = 1
    while True:
        candidate = directory / f"{stem}-{i}.md"
        if not candidate.exists():
            return candidate
        i += 1


class VaultMemoWriter:
    """File-backed voice-memo writes: .md → chunk → embed → git commit."""

    def __init__(self, vault_chroma) -> None:
        self._c = vault_chroma

    async def create_memo(
        self,
        text: str,
        user: str,
        topic: str = "",
        tags: list[str] | None = None,
        when: datetime | None = None,
    ) -> tuple[str, str]:
        """Write a memo. Returns (pbrain_id, vault-relative path).

        `when` is injectable so tests do not depend on the wall clock.
        """
        if not text.strip():
            raise ValueError("refusing to write an empty memo")

        ts = when or datetime.now()
        async with get_user_lock(user):
            pbrain_id = str(uuid.uuid4())
            # Memo only — the brain subfolder is not needed and must not be able
            # to fail this write (see vault/paths.ensure_memo_dir).
            ensure_memo_dir(user)
            vp = vault_path(user)
            ensure_repo(vp)
            now_iso = datetime.utcnow().isoformat()

            stem = ts.strftime("%Y-%m-%d %H%M")
            if slug := _slug(topic or text):
                stem = f"{stem} {slug}"
            filename = _unique_path(memo_path(user), stem)

            fm_meta = {
                "pbrain_id": pbrain_id,
                "created": now_iso,
                "updated": now_iso,
                "source": "memo",
                # Must be present so the note stays opt-out-able from Obsidian,
                # exactly like every other vault note (see vault/sync.py).
                "dont_ingest": False,
                "tags": tags or [],
            }
            write(filename, fm_meta, text)

            rel = filename.relative_to(vp)
            note_name = filename.stem
            for chunk in chunk_vault_file(text):
                # Mirrors vault/sync.py: the filename and heading breadcrumb are
                # embedded alongside the body so a query on the topic matches,
                # while the stored snippet stays clean.
                context = " › ".join(filter(None, [note_name, chunk.heading_path]))
                await self._c.upsert(
                    ids=[f"{pbrain_id}:{chunk.chunk_index}"],
                    documents=[chunk.text],
                    embed_documents=[f"{context}\n\n{chunk.text}" if context else chunk.text],
                    metadatas=[{
                        "pbrain_id": pbrain_id,
                        "path": str(rel),
                        "note_name": note_name,
                        "user": user,
                        "chunk_index": chunk.chunk_index,
                        "heading_path": chunk.heading_path,
                        "updated": now_iso,
                    }],
                )

            _git(vp, "add", str(filename))
            commit(vp, f"pbrain memo: add {pbrain_id[:8]}")
            return pbrain_id, str(rel)
