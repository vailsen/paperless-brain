"""`paperlessbrain-worker` — the proactive system's one background process.

Owns all background work: the tick loop (schedules → jobs), the job runner,
and in later phases the Telegram poller (the only process polling the bot
token), the IMAP poller and the Paperless new-document poller.

- `PROACTIVE_ENABLED=false` → one log line, exit 0, nothing touched (no DB).
- SIGTERM / SIGINT → the running job finishes, then the worker exits 0. A
  second signal stops it immediately.
- Never imports `services.clients` (embedding model + Chroma); `main()` refuses
  to run if anything pulled it in.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger("proactive.worker")

TICK_SECONDS = 60
HEARTBEAT_SCHEDULE = "heartbeat"


def heartbeat_path() -> Path:
    from config.settings import settings

    return Path(settings.app_path) / "data" / "proactive_worker.heartbeat"


class Worker:
    """Tick loop + sequential job runner. One job at a time: the limiting
    resource is the one inference host, and nothing else here is heavy."""

    def __init__(
        self,
        *,
        tick_seconds: float = TICK_SECONDS,
        llm_ok: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        from config.proactive import proactive_settings

        self.cfg = proactive_settings()
        self.tick_seconds = tick_seconds
        self.stopping = asyncio.Event()
        self._llm_ok = llm_ok
        self._background: list[asyncio.Task] = []
        self.current_job_id: int | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    def request_stop(self) -> None:
        if not self.stopping.is_set():
            log.info("stop requested — finishing the running job, then exiting")
        self.stopping.set()

    def add_background(self, coro: Awaitable[None], name: str) -> None:
        """Long-running companions (pollers). Cancelled when the loop exits."""
        self._background.append(asyncio.create_task(coro, name=name))

    async def startup(self) -> None:
        from proactive import jobs, scheduler

        n = jobs.requeue_running()
        if n:
            log.info("re-queued %d job(s) left running by the previous process", n)
        scheduler.ensure(HEARTBEAT_SCHEDULE, f"every:{int(self.tick_seconds) or 60}", run_now=True)

    async def run(self) -> None:
        await self.startup()
        log.info("worker started (tick %ss)", self.tick_seconds)
        try:
            while not self.stopping.is_set():
                self._beat()
                await self.tick_once()
                if self.stopping.is_set():
                    break
                try:
                    await asyncio.wait_for(self.stopping.wait(), timeout=self.tick_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            for t in self._background:
                t.cancel()
            await asyncio.gather(*self._background, return_exceptions=True)
            log.info("worker stopped")

    # ── one tick ─────────────────────────────────────────────────────────────

    async def tick_once(self) -> int:
        """Fire due schedules, then run due jobs until none is left or a stop
        was requested. Returns the number of jobs run."""
        from proactive import jobs, scheduler

        scheduler.tick()
        ran = 0
        while not self.stopping.is_set():
            llm_ok = await self._llm_ok() if self._llm_ok else False
            job = jobs.claim_next(llm_ok=llm_ok)
            if job is None:
                break
            self.current_job_id = job.id
            try:
                await jobs.run_one(
                    job,
                    max_attempts=self.cfg.proactive_job_max_attempts,
                    base_seconds=self.cfg.proactive_job_backoff_seconds,
                )
            finally:
                self.current_job_id = None
            ran += 1
        return ran

    def _beat(self) -> None:
        """mtime = liveness, for the container health check."""
        try:
            p = heartbeat_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch()
        except OSError:
            pass


# ── entrypoint ──────────────────────────────────────────────────────────────


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def _install_signals(loop: asyncio.AbstractEventLoop, worker: Worker, main_task: asyncio.Task) -> None:
    def _on_signal(sig: signal.Signals) -> None:
        if worker.stopping.is_set():
            log.warning("second %s — stopping now", sig.name)
            main_task.cancel()
        else:
            worker.request_stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal, sig)
        except (NotImplementedError, RuntimeError):  # pragma: no cover — non-POSIX
            pass


async def _amain() -> int:
    import proactive.tasks  # noqa: F401 — registers every job handler

    if "services.clients" in sys.modules:
        log.critical(
            "services.clients was imported into the worker — it loads the embedding "
            "model and opens Chroma, which must stay in the app process"
        )
        return 3

    worker = Worker()
    proactive.tasks.attach(worker)
    main_task = asyncio.current_task()
    assert main_task is not None
    _install_signals(asyncio.get_running_loop(), worker, main_task)
    try:
        await worker.run()
    except asyncio.CancelledError:
        return 1
    return 0


def main() -> int:
    _setup_logging()
    try:
        from config.proactive import proactive_settings

        cfg = proactive_settings()
    except Exception as exc:  # pydantic ValidationError: say which key, then stop
        log.critical("invalid proactive configuration: %s", exc)
        return 2
    if not cfg.proactive_enabled:
        log.info("PROACTIVE_ENABLED=false — the proactive worker has nothing to do, exiting")
        return 0
    try:
        return asyncio.run(_amain())
    except SystemExit as exc:
        return int(exc.code or 0)


if __name__ == "__main__":
    sys.exit(main())
