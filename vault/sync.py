import json
import logging
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from config.settings import settings
from vault.chunker import chunk_vault_file
from vault.context import EMBEDDING_SCHEMA_VERSION, embed_text, path_metadata
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


# The old psage_id -> pbrain_id migration marker. Its mere existence meant
# "schema is current", which only worked for exactly one schema change. Kept so
# an installation that already ran it is treated as schema version 1 rather than
# reindexing again for no reason.
_PBRAIN_MIGRATION_MARKER = ".pbrain_id_migrated"
_SCHEMA_MARKER = ".embedding_schema_version"


def _recorded_schema_version(username: str) -> int:
    """Schema version the user's index was last built with. 0 = never."""
    marker = git_dir_path(username) / _SCHEMA_MARKER
    try:
        return int(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pass
    return 1 if (git_dir_path(username) / _PBRAIN_MIGRATION_MARKER).exists() else 0


def _needs_reindex(username: str) -> bool:
    return _recorded_schema_version(username) < EMBEDDING_SCHEMA_VERSION


def _mark_schema_current(username: str) -> None:
    """Record the schema version *and* the legacy marker.

    Writing both keeps a downgrade to an older build from re-running the
    pbrain_id migration on an index that has long since been migrated.
    """
    try:
        d = git_dir_path(username)
        d.mkdir(parents=True, exist_ok=True)
        (d / _SCHEMA_MARKER).write_text(str(EMBEDDING_SCHEMA_VERSION), encoding="utf-8")
        (d / _PBRAIN_MIGRATION_MARKER).write_text("done", encoding="utf-8")
    except Exception:
        pass


async def _reindex_user(
    username: str,
    vp: Path,
    brain_chroma,
    vault_chroma,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    """Wipe the user's Chroma entries and re-embed every .md from scratch.

    Runs whenever the recorded embedding schema is older than
    EMBEDDING_SCHEMA_VERSION: both the embedded text and the metadata change
    with it, so old and new vectors are not comparable and an incremental update
    would leave the index half in each schema. Reprocessing each file also
    lazily migrates its frontmatter id key via ensure_pbrain_id(). Committing
    afterwards records those rewrites so the next diff-based sync does not
    re-embed them.

    Nothing else has to be reset for the change detection: it diffs against
    HEAD and commits last, so this commit *is* the new bookmark.

    Returns the number of files processed. `progress(done, total)` is called
    after each file so a UI can show how far along a full reindex is.
    """
    for col in (brain_chroma, vault_chroma):
        try:
            await col.delete(where={"user": {"$eq": username}})
        except Exception as e:
            _log.warning("reindex wipe failed for %s: %s", username, e)

    files = [
        p for p in sorted(vp.rglob("*.md")) if not p.name.endswith(".conflict.md")
    ]
    total = len(files)
    for done, p in enumerate(files, 1):
        rel = p.relative_to(vp)
        try:
            await _embed_file(username, vp, rel, brain_chroma, vault_chroma)
        except Exception as e:
            _log.warning("reindex embed failed for %s: %s", rel, e)
        if progress:
            try:
                progress(done, total)
            except Exception:  # a UI callback must never break the reindex
                _log.debug("reindex progress callback failed", exc_info=True)

    _git(vp, "add", "-A")
    commit(vp, f"pbrain: reindex (embedding schema v{EMBEDDING_SCHEMA_VERSION})")
    return total


async def reindex_user(
    username: str, progress: Callable[[int, int], None] | None = None
) -> int:
    """Force a full reindex for one user. Returns the number of files processed.

    The manual counterpart to the automatic schema-version trigger — for the
    "Reindex vault" action in settings, and for when the index is suspect.
    Takes the same per-user lock as sync_user(), so it cannot interleave with a
    sync or an agent brain write.
    """
    from services.clients import brain as brain_svc, vault_chroma

    async with get_user_lock(username):
        vp = vault_path(username)
        ensure_user_dirs(username)
        ensure_repo(vp)
        count = await _reindex_user(username, vp, brain_svc._chroma, vault_chroma, progress)
        _mark_schema_current(username)
        _last_sync[username] = time.monotonic()
        return count


async def _do_sync(username: str) -> None:
    from services.clients import brain as brain_svc, vault_chroma

    vp = vault_path(username)
    ensure_user_dirs(username)
    is_fresh = ensure_repo(vp)

    _backfill_dont_ingest(username, vp)

    brain_chroma = brain_svc._chroma

    # Embedding schema changed since this user's index was built (or it predates
    # the version marker) → rebuild it from scratch. Guarded by a marker in the
    # local git dir; runs a full reindex, so it supersedes the diff path for
    # this turn. Idempotent: the frontmatter rewrites it performs are no-ops the
    # second time around.
    if not is_fresh and _needs_reindex(username):
        _log.info(
            "vault: embedding schema v%s < v%s for %s — full reindex",
            _recorded_schema_version(username), EMBEDDING_SCHEMA_VERSION, username,
        )
        await _reindex_user(username, vp, brain_chroma, vault_chroma)
        _mark_schema_current(username)
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

    # Everything just processed was embedded with the current schema (fresh
    # install, or a vault whose files all changed), so no reindex is owed.
    _mark_schema_current(username)


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
                # The fact's own filename is its title ("Torque wrench VANPO"),
                # and it is often the only place the subject is named outright.
                # The brain subfolder is stripped: it is on every single fact, so
                # keeping it would add a token that cannot discriminate anything.
                embed_documents=[
                    embed_text(body, rel_path, strip_prefix=settings.brain_subfolder)
                ],
                metadatas=[{
                    **path_metadata(rel_path, strip_prefix=settings.brain_subfolder),
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
                # Embed the folder, note title and heading breadcrumb alongside
                # the body: the raw chunk.text carries none of them, and the
                # folder is what separates "Bremsen" under To-Dos from the same
                # word in an archived invoice. The stored snippet stays clean
                # (chunk.text); only the vector sees the header. See
                # ChromaClient.upsert(embed_documents=...) and vault/context.py.
                await vault_chroma.upsert(
                    ids=[f"{pbrain_id}:{chunk.chunk_index}"],
                    documents=[chunk.text],
                    embed_documents=[
                        embed_text(chunk.text, rel_path, chunk.heading_path)
                    ],
                    metadatas=[{
                        **path_metadata(rel_path),
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
