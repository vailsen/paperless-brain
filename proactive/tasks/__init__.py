"""Every job handler of the worker, registered on import.

`attach(worker)` adds the schedules and long-running pollers. Each phase adds
its module here; nothing outside this package registers handlers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from proactive.tasks import daily, documents, heartbeat, insights, ledger, notify, sweep, topics, vault_sync  # noqa: F401

documents.install()

if TYPE_CHECKING:
    from proactive.worker import Worker


def attach(worker: "Worker") -> None:
    """Schedules and background tasks for this worker."""
    from proactive import scheduler

    scheduler.ensure(sweep.SWEEP_ROUND, "every:60")
    scheduler.ensure(ledger.LEDGER_SCAN, "every:300", run_now=True)
    scheduler.ensure(documents.NEWDOC_POLL, f"every:{worker.cfg.proactive_newdoc_poll_seconds}", run_now=True)
    scheduler.ensure(daily.DAILY_RUN, f"daily:{worker.cfg.daily_run_time:%H:%M}")
    # Email is send-only now; drop the reply poller's schedule from older DBs.
    scheduler.remove("imap_poll")
    scheduler.ensure(vault_sync.VAULT_SYNC_CHECK, "every:300")
