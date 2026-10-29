"""What the model hosts are doing right now — for the dashboard.

Ollama says which models are loaded (`/api/ps`), never whether one of them is
generating. So every request this app or the proactive worker sends is
bracketed here (`track`): it is listed while it runs, and when it ends it
leaves a speed sample. The state is a small file next to the power state
(`data/inference_activity.json`) because the worker is a second process; it
is written twice per request, under an `flock`.

Speed is the generation rate, tokens per second:

- exact when the server says how long generating took (Ollama's native
  `eval_count` / `eval_duration`, the vision ingest);
- from the stream otherwise — tokens counted from the first one to the last,
  so reading the prompt does not count (the chat);
- a request answered in one piece without timings (Werkbank, proactive jobs)
  counts its tokens over the whole request, prompt reading included, and is
  marked as an estimate.

Nothing here may fail a request: every error is swallowed.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

#: What the request is for, shown on the dashboard. Callers that know set it
#: (`purpose.set("werkbank")`); otherwise the process decides (app / worker).
purpose: contextvars.ContextVar[str] = contextvars.ContextVar("inference_purpose", default="")

#: A request older than this is a leftover of a process that died mid-call.
STALE_S = 30 * 60
#: How long a speed sample stays worth showing.
SAMPLE_MAX_AGE_S = 24 * 3600


def _path() -> Path:
    from config.settings import settings

    return Path(settings.app_path) / "data" / "inference_activity.json"


def host_of(base_url: str) -> str:
    """`http://10.0.0.5:11434/v1` → `10.0.0.5:11434` — what entries are matched on."""
    try:
        parts = urlsplit(base_url if "://" in base_url else f"http://{base_url}")
        return f"{parts.hostname or ''}:{parts.port or (443 if parts.scheme == 'https' else 80)}"
    except ValueError:
        return ""


@contextmanager
def _locked():
    import fcntl

    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p.with_suffix(".lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            try:
                state = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {}
            state.setdefault("inflight", {})
            state.setdefault("last", {})
            yield state
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(state), encoding="utf-8")
            tmp.replace(p)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _default_purpose() -> str:
    try:
        from services.ollama_watchdog import _role

        return "proactive" if _role == "worker" else "background"
    except Exception:  # noqa: BLE001
        return "background"


class Request:
    """One tracked request. `token()` per streamed token; `usage(body)` with
    the response body when it is not streamed."""

    def __init__(self, rid: str, host: str, model: str, what: str) -> None:
        self.rid, self.host, self.model, self.what = rid, host, model, what
        self.started = time.monotonic()
        self.first: float | None = None
        self.tokens = 0
        self.exact_s: float | None = None  # generation time the server measured

    def token(self, n: int = 1) -> None:
        if self.first is None:
            self.first = time.monotonic()
        self.tokens += n

    def usage(self, body: dict | None) -> None:
        """OpenAI `usage.completion_tokens`, or Ollama's own eval timings."""
        if not isinstance(body, dict):
            return
        if body.get("eval_count") and body.get("eval_duration"):
            self.tokens = int(body["eval_count"])
            self.exact_s = int(body["eval_duration"]) / 1e9
            return
        n = (body.get("usage") or {}).get("completion_tokens")
        if n:
            self.tokens = int(n)

    def sample(self, ended: float) -> dict | None:
        if self.tokens < 8:  # too short to say anything about speed
            return None
        if self.exact_s:
            secs, kind = self.exact_s, "exact"
        elif self.first is not None and ended - self.first > 0.5:
            secs, kind = ended - self.first, "stream"
        else:
            secs, kind = ended - self.started, "estimate"
        if secs <= 0:
            return None
        return {"model": self.model, "purpose": self.what, "tps": round(self.tokens / secs, 1),
                "tokens": self.tokens, "kind": kind, "at": time.time()}


@asynccontextmanager
async def track(base_url: str, model: str, what: str = ""):
    """Bracket one request to `base_url`. Yields a `Request`."""
    rid = uuid.uuid4().hex[:12]
    req = Request(rid, host_of(base_url), model, what or purpose.get() or _default_purpose())
    try:
        with _locked() as st:
            st["inflight"][rid] = {"host": req.host, "model": model, "purpose": req.what,
                                   "since": time.time(), "pid": os.getpid()}
    except Exception:  # noqa: BLE001
        log.debug("inference activity not recorded", exc_info=True)
    try:
        yield req
    finally:
        ended = time.monotonic()
        try:
            with _locked() as st:
                st["inflight"].pop(rid, None)
                s = req.sample(ended)
                if s:
                    st["last"][req.host] = s
        except Exception:  # noqa: BLE001
            log.debug("inference activity not recorded", exc_info=True)


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def snapshot(host: str, now: float | None = None) -> dict:
    """`{"running": [entry, …] oldest first, "last": sample | None}` for one host."""
    now = now or time.time()
    try:
        st = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"running": [], "last": None}
    running = sorted(
        (e for e in (st.get("inflight") or {}).values()
         if e.get("host") == host and now - float(e.get("since") or 0) < STALE_S and _alive(e.get("pid"))),
        key=lambda e: e.get("since") or 0,
    )
    last = (st.get("last") or {}).get(host)
    if last and now - float(last.get("at") or 0) > SAMPLE_MAX_AGE_S:
        last = None
    return {"running": running, "last": last}


async def loaded_models(base_url: str) -> list[dict] | None:
    """Ollama `/api/ps`: what sits in memory. None when the host does not answer."""
    import httpx

    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{root}/api/ps")
            resp.raise_for_status()
            return list(resp.json().get("models") or [])
    except Exception:  # noqa: BLE001
        return None
