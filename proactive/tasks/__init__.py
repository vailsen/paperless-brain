"""Every job handler of the worker, registered on import.

`attach(worker)` adds the schedules and long-running pollers. Each phase adds
its module here; nothing outside this package registers handlers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from proactive.tasks import daily, documents, heartbeat, imap, ledger, notify, sweep  # noqa: F401

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
    if worker.cfg.email_available:
        scheduler.ensure(imap.IMAP_POLL, f"every:{worker.cfg.imap_poll_seconds}", run_now=True)
    else:
        scheduler.remove(imap.IMAP_POLL)  # no connection attempt without config
    if worker.cfg.telegram_available:
        from proactive.notify import telegram

        # The only getUpdates poller of the bot token (architecture §3).
        worker.add_background(telegram.poll_forever(worker.stopping), "telegram")
