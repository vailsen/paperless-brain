"""`paperlessbrain-worker` — the proactive system's one background process.

Owns all background work: the tick loop (schedules → jobs), the job runner,
and the Paperless new-document poller.

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
from typing import Awaitable

log = logging.getLogger("proactive.worker")

TICK_SECONDS = 60
HEARTBEAT_SCHEDULE = "heartbeat"


def heartbeat_path() -> Path:
    from config.settings import settings

    return Path(settings.app_path) / "data" / "proactive_worker.heartbeat"


class Worker:
    """Tick loop + sequential job runner. One job at a time: the limiting
    resource is the one inference host, and nothing else here is heavy."""

    def __init__(self, *, tick_seconds: float = TICK_SECONDS, gate=None) -> None:
        from config.proactive import proactive_settings

        self.cfg = proactive_settings()
        self.tick_seconds = tick_seconds
        self.stopping = asyncio.Event()
        #: `proactive.gate.InferenceGate` or None (= no local LLM jobs run).
        self.gate = gate
        self._background: list[asyncio.Task] = []
        self.current_job_id: int | None = None
        #: Grouping key of the model the last LLM job ran on.
        self.current_model = ""
        self.model_swaps = 0
        self._yielding = False

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

        from proactive import windows

        scheduler.tick()
        ran = 0
        while not self.stopping.is_set():
            in_window = windows.current() is not None
            if not in_window:
                self._drop_stale_backlog()
            # Backlog jobs are invisible outside a window: not claimed, and —
            # above all — never a reason to wake the host.
            outside = frozenset() if in_window else windows.WINDOWED_KINDS
            job = jobs.claim_next(
                llm_ok=await self._local_ok(in_window, outside),
                cloud_ok=True,  # resolution never yields a cloud model unless allowed
                prefer_model=self.current_model,
                exclude_kinds=outside,
            )
            if job is None:
                break
            if job.needs_llm and job.model_key and job.model_key != self.current_model:
                self._note_swap(job.model_key)
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

    def _drop_stale_backlog(self) -> None:
        """Backlog documents queued for a window that has closed: dropped
        without running (cursor not advanced — next window starts there)."""
        from proactive import jobs, windows

        for job in jobs.drop_queued({"sweep_doc"}, "its sweep window has ended"):
            start = job.payload.get("window_start")
            if start:
                windows.close(start, "window_end")
            log.info("%s: doc %s dropped — its window has ended", job.user, job.payload.get("doc_id"))

    async def _local_ok(self, in_window: bool = True, outside=frozenset()) -> bool:
        """Ask the gate only when a local LLM job is actually due — the gate may
        wake the host, which must never happen for nothing.

        Waking is for the sweep windows. Outside them the host is only used if
        it is already on — except for what the user asked for minutes ago
        ("Analyse now", a deep dive after "Interesting")."""
        from proactive import clock, jobs, windows

        if self.gate is None:
            return False
        if not jobs.pending_count(needs_llm=True, lane="local", due_only=True, exclude_kinds=outside):
            return False
        from services import ollama_watchdog

        if ollama_watchdog.app_busy():
            # A user is chatting: their answer comes first. Jobs stay queued.
            if not self._yielding:
                log.info("the app is using the model — LLM jobs wait")
            self._yielding = True
            return False
        self._yielding = False
        may_wake = in_window or bool(jobs.pending_count(
            needs_llm=True, lane="local", due_only=True, kinds=windows.USER_REQUESTED_KINDS,
            created_since=clock.iso(clock.utcnow() - windows.USER_WAKE_FRESH),
        ))
        return await self.gate.local_ready(may_wake=may_wake)

    def _note_swap(self, key: str) -> None:
        from proactive import windows

        if self.current_model:
            self.model_swaps += 1
        log.info("model %s → %s", self.current_model or "(none)", key)
        self.current_model = key
        win = windows.current()
        if win is not None:
            windows.note_model(
                win.start_iso, key, woke_host=bool(self.gate and self.gate.woke_host)
            )

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
    # httpx logs every request URL at INFO — one line per poll and per push,
    # and a URL can carry a secret (a push endpoint is a bearer address).
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


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

    from config.proactive import proactive_settings
    from config.settings import settings
    from proactive import llm
    from services import ollama_watchdog

    ollama_watchdog.set_role("worker")
    from proactive.gate import InferenceGate, OperatorModelMissing, probe

    cfg = proactive_settings()
    if (cfg.proactive_model_fixed or cfg.proactive_model_default) and not settings.ollama_server:
        log.critical(
            "PROACTIVE_MODEL_FIXED / PROACTIVE_MODEL_DEFAULT name models on OLLAMA_SERVER, "
            "which is not set"
        )
        return 2
    gate = InferenceGate.from_settings()
    llm.gate = gate
    if settings.ollama_server and await probe(settings.ollama_server):
        try:
            await gate.verify()
        except OperatorModelMissing as exc:
            log.critical("refusing to start: %s", exc)
            return 2

    worker = Worker(gate=gate)
    proactive.tasks.attach(worker)
    main_task = asyncio.current_task()
    assert main_task is not None
    _install_signals(asyncio.get_running_loop(), worker, main_task)
    try:
        await worker.run()
    except asyncio.CancelledError:
        return 1
    except OperatorModelMissing as exc:
        # Found on the first wake instead of at start (the host was asleep).
        log.critical("stopping: %s", exc)
        return 2
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
