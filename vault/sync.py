import json
import logging
import time
from datetime import datetime
from pathlib import Path

from config.settings import settings
from vault.chunker import chunk_vault_file
from vault.frontmatter import (
    ensure_pbrain_id,
    get_id as fm_get_id,
    read as fm_read,
    sanitize_tags as _sanitize_tags,
    write as fm_write,
)
from vault.git_wrapper import FileChange, _git, commit, diff_name_status, ensure_repo
from vault.locks import get_user_lock
from vault.paths import ensure_user_dirs, git_dir_path, vault_path
from vault.router import is_brain_path

_log = logging.getLogger(__name__)
_last_sync: dict[str, float] = {}


def _to_str(v, default: str = "") -> str:
    """Convert any value (incl. PyYAML datetime objects) to a plain string."""
    if v is None:
        return default
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def _truthy(v) -> bool:
    """Interpret a frontmatter flag (bool or string) as truthy. Default False."""
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "yes", "1", "ja")


async def sync_user(username: str, force: bool = False) -> None:
    """Sync vault for username.

    At most once per vault_sync_cooldown_s unless force=True. Serialized per user
    via asyncio.Lock so concurrent calls for the same user wait rather than
    producing index.lock errors.
    """
    lock = get_user_lock(username)
    # NOTE: do NOT early-return when the lock is held. A colliding caller (e.g.
    # dashboard + chat both syncing) must WAIT for the in-flight sync so it sees
    # a fresh index, not skip and retrieve stale data. The `async with lock`
    # below serializes; the inner cooldown re-check makes the queued caller a
    # no-op if the sync it waited on already covered this window.
    if not force:
        now = time.monotonic()
        if now - _last_sync.get(username, 0) < settings.vault_sync_cooldown_s:
            return
    async with lock:
        if not force and time.monotonic() - _last_sync.get(username, 0) < settings.vault_sync_cooldown_s:
            return
        await _do_sync(username)
        _last_sync[username] = time.monotonic()


def _backfill_dont_ingest(username: str, vp: Path) -> None:
    """One-time: write `dont_ingest: false` into every vault note's frontmatter.

    Makes the flag a visible, toggleable property (checkbox in Obsidian) across
    all existing notes. Runs once per user (guarded by a marker file in the local
    git dir). Commits its writes so the subsequent diff does NOT re-embed them.
    Brain files are skipped (vault notes only).
    """
    flag = git_dir_path(username) / ".dont_ingest_backfilled"
    if flag.exists():
        return
    changed = False
    try:
        for p in vp.rglob("*.md"):
            rel = p.relative_to(vp)
            if is_brain_path(rel, settings.brain_subfolder):
                continue
            if p.name.endswith(".conflict.md"):
                continue
            try:
                meta, body = fm_read(p)
                if "dont_ingest" not in meta:
                    meta["dont_ingest"] = False
                    fm_write(p, meta, body)
                    changed = True
            except Exception:
                continue
        if changed:
            _git(vp, "add", "-A")
            commit(vp, "pbrain: backfill dont_ingest flag")
    except Exception as e:
        _log.warning("dont_ingest backfill failed for %s: %s", username, e)
        return
    try:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("done", encoding="utf-8")
    except Exception:
        pass


_PBRAIN_MIGRATION_MARKER = ".pbrain_id_migrated"


def _needs_pbrain_migration(username: str) -> bool:
    return not (git_dir_path(username) / _PBRAIN_MIGRATION_MARKER).exists()


def _mark_pbrain_migrated(username: str) -> None:
    marker = git_dir_path(username) / _PBRAIN_MIGRATION_MARKER
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("done", encoding="utf-8")
    except Exception:
        pass


async def _reindex_user(username: str, vp: Path, brain_chroma, vault_chroma) -> None:
    """Wipe the user's Chroma entries and re-embed every .md from scratch.

    Used by the psage_id -> pbrain_id migration: the id metadata key and the
    embedded text both change, so a clean rebuild is simpler and safer than an
    in-place update. Reprocessing each file also lazily migrates its frontmatter
    key via ensure_pbrain_id(). Committing afterwards records those rewrites so
    the next diff-based sync does not re-embed them.
    """
    for col in (brain_chroma, vault_chroma):
        try:
            await col.delete(where={"user": {"$eq": username}})
        except Exception as e:
            _log.warning("reindex wipe failed for %s: %s", username, e)

    for p in sorted(vp.rglob("*.md")):
        rel = p.relative_to(vp)
        if p.name.endswith(".conflict.md"):
            continue
        try:
            await _embed_file(username, vp, rel, brain_chroma, vault_chroma)
        except Exception as e:
            _log.warning("reindex embed failed for %s: %s", rel, e)

    _git(vp, "add", "-A")
    commit(vp, "pbrain: migrate id key + reindex")


async def _do_sync(username: str) -> None:
    from services.clients import brain as brain_svc, vault_chroma

    vp = vault_path(username)
    ensure_user_dirs(username)
    is_fresh = ensure_repo(vp)

    _backfill_dont_ingest(username, vp)

    brain_chroma = brain_svc._chroma

    # One-time migration: rename the frontmatter/Chroma id key psage_id -> pbrain_id
    # and re-embed with title/heading context. Guarded by a marker in the local
    # git dir; idempotent (frontmatter migration is a no-op once done). Runs a
    # full reindex, so it supersedes the diff path for this turn.
    if not is_fresh and _needs_pbrain_migration(username):
        await _reindex_user(username, vp, brain_chroma, vault_chroma)
        _mark_pbrain_migrated(username)
        return

    if is_fresh:
        # Vault was wiped externally — purge stale Chroma entries for this user.
        try:
            await brain_chroma.delete(where={"user": {"$eq": username}})
        except Exception:
            pass
        try:
            await vault_chroma.delete(where={"user": {"$eq": username}})
        except Exception:
            pass

    changes = diff_name_status(vp)
    if not changes:
        return

    failed_paths: list[Path] = []
    for change in changes:
        ok = await _process_change(username, vp, change, brain_chroma, vault_chroma)
        if not ok and change.path:
            failed_paths.append(change.path)

    # Stage pbrain_id backfill writes that happened during processing
    _git(vp, "add", "-A")

    # Unstage files that failed to embed — must happen AFTER the second add
    # so backfill writes are committed but failed embeds stay dirty for retry.
    for p in failed_paths:
        try:
            _git(vp, "restore", "--staged", str(p))
        except Exception:
            pass

    commit(vp, f"pbrain sync {datetime.utcnow().isoformat()[:19]}Z")

    # Files just processed here already carry pbrain_id (fresh installs, or a
    # vault that had no legacy keys), so the one-time reindex is unnecessary.
    _mark_pbrain_migrated(username)


async def _process_change(
    username: str,
    vp: Path,
    change: FileChange,
    brain_chroma,
    vault_chroma,
) -> bool:
    """Process one file change. Returns True on success, False if embed failed."""
    if change.status == "Deleted":
        path_str = str(change.path)
        try:
            await brain_chroma.delete(where={"path": {"$eq": path_str}})
        except Exception:
            pass
        try:
            await vault_chroma.delete(where={"path": {"$eq": path_str}})
        except Exception:
            pass
        return True

    if change.status == "Renamed":
        old_str = str(change.old_path)
        new_str = str(change.path)
        if change.similarity == 100:
            await _update_path_metadata(vp, change.path, old_str, new_str, brain_chroma, vault_chroma)
            return True
        # similarity < 100 → delete old, embed new
        for col in (brain_chroma, vault_chroma):
            try:
                await col.delete(where={"path": {"$eq": old_str}})
            except Exception:
                pass
        return await _embed_file(username, vp, change.path, brain_chroma, vault_chroma)

    return await _embed_file(username, vp, change.path, brain_chroma, vault_chroma)


async def _update_path_metadata(
    vp: Path, new_rel: Path, old_str: str, new_str: str, brain_chroma, vault_chroma
) -> None:
    abs_path = vp / new_rel
    if not abs_path.exists():
        return
    try:
        meta, _ = fm_read(abs_path)
        pbrain_id = fm_get_id(meta)
        if not pbrain_id:
            return
        brain_items = await brain_chroma.get(ids=[pbrain_id])
        if brain_items:
            await brain_chroma.update(ids=[pbrain_id], metadatas=[{"path": new_str}])
        vault_items = await vault_chroma.get(where={"pbrain_id": {"$eq": pbrain_id}})
        if vault_items:
            await vault_chroma.update(
                ids=[item["id"] for item in vault_items],
                metadatas=[{"path": new_str} for _ in vault_items],
            )
    except Exception as e:
        _log.warning("path-metadata update failed for %s: %s", new_str, e)


async def _embed_file(
    username: str, vp: Path, rel_path: Path, brain_chroma, vault_chroma
) -> bool:
    """Embed a file into Chroma. Returns True on success, False on failure."""
    abs_path = vp / rel_path
    if not abs_path.exists():
        return True  # race: file deleted between diff and now — not an error
    if rel_path.name.endswith(".conflict.md"):
        return True
    if rel_path.suffix != ".md":
        return True

    try:
        meta, body = fm_read(abs_path)
        now = datetime.utcnow().isoformat()

        if is_brain_path(rel_path, settings.brain_subfolder):
            pbrain_id = ensure_pbrain_id(abs_path, meta)
            meta, body = fm_read(abs_path)  # re-read after potential id write/migration
            await brain_chroma.upsert(
                ids=[pbrain_id],
                documents=[body],
                metadatas=[{
                    "pbrain_id": pbrain_id,
                    "path": str(rel_path),
                    "user": username,
                    "common": bool(meta.get("common", False)),
                    "source_doc_id": meta.get("source_doc_id") or 0,
                    "source_page": meta.get("source_page") or 0,
                    "confidence": float(meta.get("confidence", 1.0)),
                    "created_at": _to_str(meta.get("created"), now),
                    "tags": json.dumps(_sanitize_tags(meta.get("tags"))),
                    "source": _to_str(meta.get("source"), "conversation"),
                    "updated": _to_str(meta.get("updated"), now),
                    "kind": _to_str(meta.get("kind"), "fact"),
                    "due": _to_str(meta.get("due"), ""),
                }],
            )
        else:
            pbrain_id = ensure_pbrain_id(abs_path, meta)
            meta, body = fm_read(abs_path)
            # Ensure the flag exists so it stays toggleable in Obsidian (new notes).
            if "dont_ingest" not in meta:
                meta["dont_ingest"] = False
                fm_write(abs_path, meta, body)
                meta, body = fm_read(abs_path)
            # dont_ingest: user opt-out per note. Drop any existing chunks so
            # toggling it on removes the note from the index; keep the pbrain_id.
            if _truthy(meta.get("dont_ingest")):
                try:
                    await vault_chroma.delete(where={"pbrain_id": {"$eq": pbrain_id}})
                except Exception:
                    pass
                return True
            chunks = chunk_vault_file(body)
            if not chunks:
                return True
            note_name = rel_path.name[:-3] if rel_path.name.endswith(".md") else rel_path.name
            try:
                await vault_chroma.delete(where={"pbrain_id": {"$eq": pbrain_id}})
            except Exception:
                pass
            for chunk in chunks:
                # Embed the note title + heading breadcrumb alongside the body so
                # a query on a title word (e.g. "backlog") matches — the raw
                # chunk.text alone never carries the filename. The stored snippet
                # stays clean (chunk.text); only the embedded vector sees the
                # prefix. See ChromaClient.upsert(embed_documents=...).
                context = " › ".join(filter(None, [note_name, chunk.heading_path]))
                embed_text = f"{context}\n\n{chunk.text}" if context else chunk.text
                await vault_chroma.upsert(
                    ids=[f"{pbrain_id}:{chunk.chunk_index}"],
                    documents=[chunk.text],
                    embed_documents=[embed_text],
                    metadatas=[{
                        "pbrain_id": pbrain_id,
                        "path": str(rel_path),
                        "note_name": note_name,
                        "user": username,
                        "chunk_index": chunk.chunk_index,
                        "heading_path": chunk.heading_path,
                        "updated": _to_str(meta.get("updated"), now),
                    }],
                )
        return True
    except Exception as e:
        _log.warning("embed failed for %s: %s", rel_path, e)
        return False
