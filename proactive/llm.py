"""Proactive LLM calls: the user's resolved model, the cloud cap, and
"host gone" as a deferral rather than a failure.

Calls go through `werkbank.llm_lane` (the existing client: registry lookup,
lanes, the idle-timer `touch()` that keeps the host up while the worker uses
it). Operator models (FIXED / DEFAULT) are model ids on `OLLAMA_SERVER` and are
called without a registry; user and chat models by their registry name with
the user's token.
"""

from __future__ import annotations

import logging

import httpx

from proactive import windows
from proactive.jobs import Defer
from proactive.models import ResolvedModel

log = logging.getLogger("proactive.llm")

#: Set by the worker, so a connection failure can close the gate at once.
gate = None

_UNREACHABLE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.RemoteProtocolError, ConnectionError)


def check_cloud_cap(user: str, model: ResolvedModel) -> None:
    """Before the first cloud call for a document. Over the cap → wait for tomorrow."""
    from config.proactive import proactive_settings

    if model.lane != "cloud":
        return
    cfg = proactive_settings()
    if not cfg.proactive_cloud_allowed:
        raise Defer("cloud models are not allowed for proactive jobs", 3600)
    if windows.cloud_used_today(user) >= cfg.proactive_cloud_max_docs_per_user_day:
        raise Defer(f"{user}: daily cloud document cap reached", windows.seconds_until_tomorrow())


def record_cloud_doc(user: str, model: ResolvedModel) -> None:
    if model.lane == "cloud":
        windows.add_cloud_use(user)


async def structured(
    model: ResolvedModel,
    user: str,
    token: str,
    *,
    system: str,
    messages: list[dict],
    json_schema: dict,
    tool_name: str = "output",
    max_tokens: int = 4000,
) -> dict:
    from werkbank import llm_lane

    who, tok = (user, token) if model.uses_registry else ("", "")
    try:
        return await llm_lane.complete_structured(
            system,
            messages,
            model=model.name,
            user_id=who,
            token=tok,
            json_schema=json_schema,
            tool_name=tool_name,
            max_tokens=max_tokens,
        )
    except _UNREACHABLE as exc:
        if gate is not None and model.lane == "local":
            gate.mark_unreachable(f"{type(exc).__name__} from {model.name}")
        raise Defer(f"model {model.name} unreachable ({type(exc).__name__})", 300) from exc
