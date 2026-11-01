"""Keep the vault index current in the background.

The normal sync (`vault.sync.sync_user`) runs at the start of a chat turn. A
night of sweeping writes findings and dossiers into the vault, and Remotely
Save brings in the user's own Obsidian edits — the first chat turn then had to
commit and embed all of it while the user waited.

So the worker asks the app to sync (it cannot itself: no embedding model in
this process) every `CHECK_SECONDS`, for every user whose vault has
uncommitted changes — anything, not only the findings folder. The check is a
`git status`; a clean vault costs nothing. It used to wait until the worker's
batch had been quiet for 10 minutes, which on a sweep night meant: not until
morning, exactly when the user starts chatting.

One sync is skipped: while the app is using the model (`app_busy`) a user is
chatting, and their turn syncs anyway — a second sync would only queue on the
same per-user lock in front of their retrieval.
"""

from __future__ import annotations

import logging

from proactive import users
from proactive.jobs import Job, handler

log = logging.getLogger("proactive.vault_sync")

VAULT_SYNC_CHECK = "vault_sync_check"
CHECK_SECONDS = 120


@handler(VAULT_SYNC_CHECK)
async def vault_sync_check(job: Job) -> None:
    from proactive.internal_client import InternalUnavailable, vault_sync
    from services import ollama_watchdog
    from vault.notes import pending_rel_paths

    if ollama_watchdog.app_busy():
        return  # a user is chatting; their turn syncs anyway
    for user, _tok in users.eligible():
        try:
            pending = await pending_rel_paths(user)
        except Exception as exc:                                  # noqa: BLE001
            log.info("%s: vault status unavailable: %s", user, exc)
            continue
        if not pending:
            continue
        try:
            await vault_sync(user)
        except InternalUnavailable as exc:
            log.info("%s: app not reachable for the vault sync (%s) — next check", user, exc)
            return
        log.info("%s: vault synced in the background (%d pending path(s))", user, len(pending))
