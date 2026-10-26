"""Every job handler of the worker, registered on import.

`attach(worker)` adds the schedules and long-running pollers. Each phase adds
its module here; nothing outside this package registers handlers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from proactive.tasks import heartbeat, notify, sweep  # noqa: F401

if TYPE_CHECKING:
    from proactive.worker import Worker


def attach(worker: "Worker") -> None:
    """Schedules and background tasks for this worker."""
    from proactive import scheduler

    scheduler.ensure(sweep.SWEEP_ROUND, "every:60")
    if worker.cfg.telegram_available:
        from proactive.notify import telegram

        # The only getUpdates poller of the bot token (architecture §3).
        worker.add_background(telegram.poll_forever(worker.stopping), "telegram")
