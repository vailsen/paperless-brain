"""The LLM gate: may local LLM jobs run now?

Before a local LLM job runs, the worker asks `local_ready(may_wake)`.
paperless-brain wakes nothing itself; a sleeping GPU host sits behind a
wake-on-request proxy (docs/operations.md, "Sleeping GPU host").

- Behind such a proxy (`GET /proxy/status` answers), its view decides:
  - `may_wake` (inside a sweep window, or something the user just asked
    for): `up`, `waking` and also `asleep` count as ready — the job's request
    wakes the host through the proxy. `shutting_down` waits a few minutes.
  - otherwise: only `up` is ready. The worker's requests then also carry
    `X-No-Wake: 1` (`services.model_host.no_wake`), so a host that fell
    asleep in between is not woken for them.
- A plain server (no `/proxy/status`): ready when it answers the probe.
- Not ready → the next attempt waits (exponential backoff; outside a window a
  flat recheck). Local LLM jobs stay queued; jobs without LLM keep running.

The worker's requests carry `X-Idle-Profile: background`, so a proxy shuts a
host it woke for them down sooner. There is no power logic here.

The gate also enforces that `PROACTIVE_MODEL_FIXED` exists on the host the
first time it can ask (`OperatorModelMissing` stops the worker).
"""

from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable

import httpx

log = logging.getLogger("proactive.gate")

READY_CACHE_S = 60.0
BACKOFF_START_S = 300.0
BACKOFF_MAX_S = 3600.0
#: Outside a window the host is only checked for being on already.
ASLEEP_RECHECK_S = 300.0


async def _proxy_status(server: str) -> dict | None:
    from services import model_host

    return await model_host.proxy_status(server)


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
        operator_models: list[str] | None = None,
        probe_fn: Callable[[str], Awaitable[bool]] = probe,
        status_fn: Callable[[str], Awaitable[dict | None]] | None = None,
        list_models_fn: Callable[[str], Awaitable[set[str]]] = list_models,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.server = server
        self.operator_models = [m for m in (operator_models or []) if m]
        self._probe = probe_fn
        self._status = status_fn or _proxy_status
        self._list_models = list_models_fn
        self._now = monotonic
        self._ready_until = 0.0
        self._retry_at = 0.0
        self._backoff = BACKOFF_START_S
        self._verified = False
        self.woke_host = False  # for window_log: was the host asleep when work began
        self._waiting_for_window = False
        #: True while the ready state came from a sleeping host (`may_wake`).
        self.host_asleep = False

    @classmethod
    def from_settings(cls) -> "InferenceGate":
        from config.proactive import proactive_settings
        from config.settings import settings

        cfg = proactive_settings()
        # Looked up at call time (not as defaults), so tests can patch them.
        return cls(
            server=settings.ollama_server,
            operator_models=[cfg.proactive_model_fixed, cfg.proactive_model_default],
            probe_fn=probe,
            status_fn=_proxy_status,
            list_models_fn=list_models,
        )

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
        if now < self._ready_until and (may_wake or not self.host_asleep):
            return True
        if now < self._retry_at and not (may_wake and self._waiting_for_window):
            # (A window opening or a fresh request ends the asleep-recheck wait.)
            return False
        st = await self._status(self.server)
        if st is not None:
            host = st.get("host")
            if host == "up" or (may_wake and host in ("asleep", "waking")):
                if host != "up":
                    log.info("inference host is %s — the proxy wakes it for the first request", host)
                    self.woke_host = True
                self._waiting_for_window = False
                self.host_asleep = host != "up"
                await self._mark_ready()
                return True
            if host == "shutting_down":
                self._retry_at = now + ASLEEP_RECHECK_S
                log.info("inference host is shutting down — LLM jobs wait")
                return False
            return self._asleep_outside_window(now)
        if await self._probe(self.server):
            self.host_asleep = False
            await self._mark_ready()
            return True
        if not may_wake:
            return self._asleep_outside_window(now)
        self._waiting_for_window = False
        self._fail("inference host unreachable")
        return False

    def _asleep_outside_window(self, now: float) -> bool:
        """Asleep, and not ours to wake now. Look again in a few minutes (the
        user may switch it on); no backoff growth, one log line."""
        self._retry_at = now + ASLEEP_RECHECK_S
        if not self._waiting_for_window:
            log.info("inference host is off and no sweep window is open — LLM jobs wait")
        self._waiting_for_window = True
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
