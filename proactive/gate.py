"""The LLM gate: is the local inference host up — and may we wake it?

Before a local LLM job runs, the worker asks `local_ready()`:

- host answers → ready (cached for a minute);
- host silent, `PROACTIVE_WOL_AUTO` on, a MAC and `OLLAMA_SERVER` configured →
  wake it through the same code path as the dashboard button
  (`services.wol.wake`, which records it in the shared power state so the
  app's existing idle rule shuts it down again later), then wait for it;
- otherwise → not ready, and the next attempt waits with exponential backoff.
  Local LLM jobs simply stay queued; jobs without LLM keep running.

There is no shutdown logic here, by design. When the work is done the worker
stops sending requests and the existing idle rule takes over.

The gate also enforces that `PROACTIVE_MODEL_FIXED` exists on the host the
first time it can ask (`OperatorModelMissing` stops the worker).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

import httpx

log = logging.getLogger("proactive.gate")

READY_CACHE_S = 60.0
WAKE_TIMEOUT_S = 240.0
WAKE_POLL_S = 5.0
BACKOFF_START_S = 300.0
BACKOFF_MAX_S = 3600.0
#: Outside a window the host is only checked for being on already.
ASLEEP_RECHECK_S = 300.0


class OperatorModelMissing(RuntimeError):
    """PROACTIVE_MODEL_FIXED / _DEFAULT names a model the host does not offer."""


async def probe(server: str, timeout: float = 3.0) -> bool:
    """Ollama `/api/version`, else any OpenAI-compatible `/v1/models`."""
    base = server.rstrip("/").removesuffix("/v1")
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            for path in ("/api/version", "/v1/models"):
                r = await c.get(base + path)
                if r.status_code == 200:
                    return True
    except (httpx.HTTPError, OSError):
        pass  # connection-level failure: the host is not there
    return False


async def list_models(server: str, timeout: float = 5.0) -> set[str]:
    """Model ids the host offers (Ollama `/api/tags`, else `/v1/models`)."""
    base = server.rstrip("/").removesuffix("/v1")
    async with httpx.AsyncClient(timeout=timeout) as c:
        try:
            r = await c.get(base + "/api/tags")
            if r.status_code == 200:
                return {m.get("name", "") for m in r.json().get("models", [])} - {""}
        except (httpx.HTTPError, ValueError):
            pass
        r = await c.get(base + "/v1/models")
        r.raise_for_status()
        return {m.get("id", "") for m in r.json().get("data", [])} - {""}


def _same_model(wanted: str, offered: set[str]) -> bool:
    # Ollama lists "qwen3:32b"; "qwen3" means "qwen3:latest".
    return wanted in offered or (":" not in wanted and f"{wanted}:latest" in offered)


class InferenceGate:
    def __init__(
        self,
        *,
        server: str,
        mac: str,
        wol_auto: bool,
        operator_models: list[str] | None = None,
        probe_fn: Callable[[str], Awaitable[bool]] = probe,
        wake_fn: Callable[[str, str], object] | None = None,
        list_models_fn: Callable[[str], Awaitable[set[str]]] = list_models,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.server = server
        self.mac = mac
        self.wol_auto = wol_auto
        self.operator_models = [m for m in (operator_models or []) if m]
        self._probe = probe_fn
        self._wake = wake_fn
        self._list_models = list_models_fn
        self._sleep = sleep
        self._now = monotonic
        self._ready_until = 0.0
        self._retry_at = 0.0
        self._backoff = BACKOFF_START_S
        self._verified = False
        self.woke_host = False  # for window_log: did this worker wake it
        self._waiting_for_window = False

    @classmethod
    def from_settings(cls) -> "InferenceGate":
        from config.proactive import proactive_settings
        from config.settings import settings

        cfg = proactive_settings()
        from services.wol import wake

        return cls(
            server=settings.ollama_server,
            mac=settings.ollama_host_lan_mac_address_wol,
            wol_auto=cfg.proactive_wol_auto,
            operator_models=[cfg.proactive_model_fixed, cfg.proactive_model_default],
            probe_fn=probe,
            wake_fn=wake,
            list_models_fn=list_models,
        )

    @property
    def can_wake(self) -> bool:
        return bool(self.wol_auto and self.mac and self.server and self._wake)

    async def verify(self) -> None:
        """Raise `OperatorModelMissing` if the host is up and lacks an operator model."""
        if self._verified or not self.operator_models or not self.server:
            return
        offered = await self._list_models(self.server)
        missing = [m for m in self.operator_models if not _same_model(m, offered)]
        if missing:
            raise OperatorModelMissing(
                f"{', '.join(missing)} is not offered by {self.server} "
                f"(it has: {', '.join(sorted(offered)) or 'nothing'}) — fix "
                "PROACTIVE_MODEL_FIXED / PROACTIVE_MODEL_DEFAULT or pull the model"
            )
        self._verified = True

    async def local_ready(self, may_wake: bool = True) -> bool:
        """True if local LLM jobs may run now. `may_wake=False` (outside the
        sweep windows, nothing the user just asked for): only a host that is
        already up counts — it is never woken."""
        if not self.server:
            # No operator host: local registry models are tried directly and a
            # connection error defers the job (`proactive.llm`).
            return True
        now = self._now()
        if now < self._ready_until:
            return True
        if now < self._retry_at and not (may_wake and self._waiting_for_window):
            # (A window opening or a fresh request ends the asleep-recheck wait.)
            return False
        if await self._probe(self.server):
            await self._mark_ready()
            return True
        if not may_wake:
            # Asleep, and not ours to wake now. Look again in a few minutes
            # (the user may switch it on); no backoff growth, one log line.
            self._retry_at = now + ASLEEP_RECHECK_S
            if not self._waiting_for_window:
                log.info("inference host is off and no sweep window is open — LLM jobs wait")
            self._waiting_for_window = True
            return False
        self._waiting_for_window = False
        if not self.can_wake:
            self._fail("inference host unreachable and waking is off or not configured")
            return False
        log.info("inference host asleep — sending Wake-on-LAN to %s", self.server)
        try:
            self._wake(self.mac, self.server)
        except Exception as exc:
            self._fail(f"Wake-on-LAN failed: {exc}")
            return False
        self.woke_host = True
        waited = 0.0
        while waited < WAKE_TIMEOUT_S:
            await self._sleep(WAKE_POLL_S)
            waited += WAKE_POLL_S
            if await self._probe(self.server):
                log.info("inference host awake after %ss", int(waited))
                await self._mark_ready()
                return True
        self._fail(f"inference host did not answer within {int(WAKE_TIMEOUT_S)}s of waking")
        return False

    async def _mark_ready(self) -> None:
        await self.verify()
        self._ready_until = self._now() + READY_CACHE_S
        self._backoff = BACKOFF_START_S
        self._retry_at = 0.0

    def mark_unreachable(self, reason: str) -> None:
        """A job's request failed at connection level: stop trusting the cache."""
        self._ready_until = 0.0
        self._fail(reason)

    def _fail(self, reason: str) -> None:
        self._retry_at = self._now() + self._backoff
        log.info("LLM jobs wait %ss: %s", int(self._backoff), reason)
        self._backoff = min(self._backoff * 2, BACKOFF_MAX_S)
