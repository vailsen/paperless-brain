import json
import re
import uuid
from datetime import datetime
from pathlib import Path

from vault.frontmatter import read, sanitize_tags as _sanitize_tags, write
from vault.git_wrapper import _git, commit, ensure_repo
from vault.locks import get_user_lock
from vault.paths import brain_path, ensure_user_dirs, vault_path


def _slug(text: str) -> str:
    slug = re.sub(r'[\\/:*?"<>|#^\[\]]', "", text)
    slug = re.sub(r"\s+", " ", slug).strip()
    if len(slug) > 64:
        slug = slug[:64].rsplit(" ", 1)[0].rstrip(" .,")
    return slug or "Erinnerung"


def _unique_path(directory: Path, slug: str) -> Path:
    candidate = directory / f"{slug}.md"
    if not candidate.exists():
        return candidate
    i = 1
    while True:
        candidate = directory / f"{slug}-{i}.md"
        if not candidate.exists():
            return candidate
        i += 1


class VaultBrainWriter:
    """File-backed brain write primitives.

    Each write: file operation → Chroma upsert/update/delete → git commit.
    All operations take the per-user lock so they serialize with sync_user().
    """

    def __init__(self, brain_chroma) -> None:
        self._c = brain_chroma

    async def create_memory(
        self,
        text: str,
        tags: list[str],
        user: str,
        source_doc_id: int | None = None,
        source_page: int | None = None,
        confidence: float = 1.0,
        filename_topic: str | None = None,
    ) -> str:
        """Write .md → upsert Chroma → commit. Returns pbrain_id."""
        async with get_user_lock(user):
            pbrain_id = str(uuid.uuid4())
            ensure_user_dirs(user)
            vp = vault_path(user)
            ensure_repo(vp)
            bp = brain_path(user)
            now = datetime.utcnow().isoformat()

            clean_tags = _sanitize_tags(tags)
            base = _slug(filename_topic) if filename_topic else _slug(text)
            filename = _unique_path(bp, base)
            fm_meta: dict = {
                "pbrain_id": pbrain_id,
                "created": now,
                "updated": now,
                "source": "conversation",
                "tags": clean_tags,
            }
            if source_doc_id:
                fm_meta["source_doc_id"] = source_doc_id
            if source_page:
                fm_meta["source_page"] = source_page
            if confidence < 1.0:
                fm_meta["confidence"] = confidence

            write(filename, fm_meta, text)

            rel = filename.relative_to(vp)
            await self._c.upsert(
                ids=[pbrain_id],
                documents=[text],
                metadatas=[{
                    "pbrain_id": pbrain_id,
                    "path": str(rel),
                    "user": user,
                    "common": False,
                    "source_doc_id": source_doc_id or 0,
                    "source_page": source_page or 0,
                    "confidence": max(0.0, min(1.0, confidence)),
                    "created_at": now,
                    "tags": json.dumps(clean_tags),
                    "source": "conversation",
                    "updated": now,
                }],
            )

            _git(vp, "add", str(filename))
            commit(vp, f"pbrain brain: add {pbrain_id[:8]}")
            return pbrain_id

    async def create_deadline(
        self,
        text: str,
        due: str,
        user: str,
        tags: list[str] | None = None,
    ) -> str:
        """Write a manual due-date as a brain note (kind=deadline). Returns pbrain_id.

        Stored like any brain fact (file + embed + commit) so it is recallable
        via brain_search, but carries `kind: deadline` + `due: YYYY-MM-DD` in
        frontmatter and Chroma metadata so the dashboard and get_actions can
        surface it as a deadline.
        """
        async with get_user_lock(user):
            pbrain_id = str(uuid.uuid4())
            ensure_user_dirs(user)
            vp = vault_path(user)
            ensure_repo(vp)
            bp = brain_path(user)
            now = datetime.utcnow().isoformat()

            clean_tags = _sanitize_tags(tags or ["frist"])
            filename = _unique_path(bp, _slug(f"Frist {due} {text}"))
            fm_meta: dict = {
                "pbrain_id": pbrain_id,
                "kind": "deadline",
                "due": due,
                "created": now,
                "updated": now,
                "source": "manual",
                "tags": clean_tags,
            }
            write(filename, fm_meta, text)

            rel = filename.relative_to(vp)
            await self._c.upsert(
                ids=[pbrain_id],
                documents=[text],
                metadatas=[{
                    "pbrain_id": pbrain_id,
                    "path": str(rel),
                    "user": user,
                    "common": False,
                    "kind": "deadline",
                    "due": due,
                    "source_doc_id": 0,
                    "source_page": 0,
                    "confidence": 1.0,
                    "created_at": now,
                    "tags": json.dumps(clean_tags),
                    "source": "manual",
                    "updated": now,
                }],
            )

            _git(vp, "add", str(filename))
            commit(vp, f"pbrain brain: add deadline {pbrain_id[:8]}")
            return pbrain_id

    async def update_deadline(
        self, pbrain_id: str, text: str | None = None, due: str | None = None
    ) -> None:
        """Update a manual deadline's text and/or due date in file + Chroma + commit."""
        items = await self._c.get(ids=[pbrain_id])
        if not items:
            raise ValueError(f"pbrain_id not found: {pbrain_id}")
        meta_stored = items[0].get("metadata") or {}
        path_str = meta_stored["path"]
        user = meta_stored["user"]

        async with get_user_lock(user):
            vp = vault_path(user)
            ensure_repo(vp)
            abs_path = vp / path_str
            fm, body = read(abs_path)
            if text is not None:
                body = text
            if due is not None:
                fm["due"] = due
            fm["updated"] = datetime.utcnow().isoformat()
            write(abs_path, fm, body)

            new_meta: dict = {"updated": fm["updated"]}
            if due is not None:
                new_meta["due"] = due
            update_kwargs: dict = {"ids": [pbrain_id], "metadatas": [new_meta]}
            if text is not None:
                update_kwargs["documents"] = [body]
            await self._c.update(**update_kwargs)

            _git(vp, "add", str(abs_path))
            commit(vp, f"pbrain brain: update deadline {pbrain_id[:8]}")

    async def update_memory(self, pbrain_id: str, new_text: str) -> None:
        """Resolve path via Chroma → rewrite body + updated → re-embed → commit."""
        items = await self._c.get(ids=[pbrain_id])
        if not items:
            raise ValueError(f"pbrain_id not found: {pbrain_id}")
        meta_stored = items[0].get("metadata") or {}
        path_str = meta_stored["path"]
        user = meta_stored["user"]

        async with get_user_lock(user):
            vp = vault_path(user)
            ensure_repo(vp)
            abs_path = vp / path_str
            fm, _ = read(abs_path)
            fm["updated"] = datetime.utcnow().isoformat()
            write(abs_path, fm, new_text)
            await self._c.update(
                ids=[pbrain_id],
                documents=[new_text],
                metadatas=[{"updated": fm["updated"]}],
            )
            _git(vp, "add", str(abs_path))
            commit(vp, f"pbrain brain: update {pbrain_id[:8]}")

    async def update_tags(self, pbrain_id: str, tags: list[str]) -> None:
        """Update tags in frontmatter + Chroma + commit."""
        items = await self._c.get(ids=[pbrain_id])
        if not items:
            raise ValueError(f"pbrain_id not found: {pbrain_id}")
        meta_stored = items[0].get("metadata") or {}
        path_str = meta_stored["path"]
        user = meta_stored["user"]

        async with get_user_lock(user):
            vp = vault_path(user)
            ensure_repo(vp)
            abs_path = vp / path_str
            fm, body = read(abs_path)
            clean_tags = _sanitize_tags(tags)
            fm["tags"] = clean_tags
            fm["updated"] = datetime.utcnow().isoformat()
            write(abs_path, fm, body)
            await self._c.update(
                ids=[pbrain_id],
                metadatas=[{"tags": json.dumps(clean_tags), "updated": fm["updated"]}],
            )
            _git(vp, "add", str(abs_path))
            commit(vp, f"pbrain brain: update_tags {pbrain_id[:8]}")

    async def set_common(self, pbrain_id: str, value: bool) -> None:
        """Update common flag in frontmatter + Chroma + commit."""
        items = await self._c.get(ids=[pbrain_id])
        if not items:
            raise ValueError(f"pbrain_id not found: {pbrain_id}")
        meta_stored = items[0].get("metadata") or {}
        path_str = meta_stored["path"]
        user = meta_stored["user"]

        async with get_user_lock(user):
            vp = vault_path(user)
            ensure_repo(vp)
            abs_path = vp / path_str
            fm, body = read(abs_path)
            fm["common"] = value
            fm["updated"] = datetime.utcnow().isoformat()
            write(abs_path, fm, body)
            await self._c.update(
                ids=[pbrain_id],
                metadatas=[{"common": value, "updated": fm["updated"]}],
            )
            _git(vp, "add", str(abs_path))
            commit(vp, f"pbrain brain: set_common {pbrain_id[:8]}")

    async def delete_memory(self, pbrain_id: str) -> None:
        """Resolve path → rm file → delete Chroma entry → commit."""
        items = await self._c.get(ids=[pbrain_id])
        if not items:
            raise ValueError(f"pbrain_id not found: {pbrain_id}")
        meta_stored = items[0].get("metadata") or {}
        path_str = meta_stored["path"]
        user = meta_stored["user"]

        async with get_user_lock(user):
            vp = vault_path(user)
            ensure_repo(vp)
            abs_path = vp / path_str
            if abs_path.exists():
                abs_path.unlink()
            await self._c.delete(ids=[pbrain_id])
            _git(vp, "add", "-A")
            commit(vp, f"pbrain brain: delete {pbrain_id[:8]}")
