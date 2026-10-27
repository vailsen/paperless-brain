"""Index the worker's own vault writes once its batch is done.

Findings, dossiers and `_interests.md` are files in the user's vault; the
normal sync (`vault.sync.sync_user`) runs at the start of a chat turn. After a
night of sweeping, that first turn had to commit and embed everything the
worker wrote — the user waited for a sync they never asked for.

So the worker asks the app to sync (it cannot itself: no embedding model in
this process), but only when
- its own folder (`proactive.paths.findings_root`) has uncommitted changes,
- no LLM job is due — the batch is over, and
- the app is not using the model — nobody is chatting right now.

This is the one sync not started by the user. It is triggered by the worker's
own writes, never by the clock alone, and it syncs nothing while the
findings folder is clean.
"""

from __future__ import annotations

import logging

from proactive import jobs, users
from proactive.jobs import Job, handler

log = logging.getLogger("proactive.vault_sync")

VAULT_SYNC_CHECK = "vault_sync_check"


def _findings_prefix(user: str) -> str:
    from proactive.paths import findings_root
    from vault.paths import vault_path

    return findings_root(user).relative_to(vault_path(user)).as_posix().rstrip("/") + "/"


@handler(VAULT_SYNC_CHECK)
async def vault_sync_check(job: Job) -> None:
    from proactive.internal_client import InternalUnavailable, vault_sync
    from services import ollama_watchdog
    from vault.notes import pending_rel_paths

    if jobs.pending_count(needs_llm=True, due_only=True):
        return  # the batch is still running
    if ollama_watchdog.app_busy():
        return  # a user is chatting; their turn syncs anyway
    for user, _tok in users.eligible():
        try:
            prefix = _findings_prefix(user)
            pending = await pending_rel_paths(user)
        except Exception as exc:                                  # noqa: BLE001
            log.info("%s: vault status unavailable: %s", user, exc)
            continue
        if not any(p.startswith(prefix) for p in pending):
            continue
        try:
            await vault_sync(user)
        except InternalUnavailable as exc:
            log.info("%s: app not reachable for the vault sync (%s) — next check", user, exc)
            return
        log.info("%s: vault synced after the worker's writes (%d pending path(s))", user, len(pending))
