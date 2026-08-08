"""werkbank/llm_lane.py — LLM backends with per-lane concurrency control.

Two asyncio.Semaphores serialise access by the limiting resource:
  _LOCAL_SEM (1)  — one GPU call at a time on the local Ollama instance
  _API_SEM   (3)  — a few concurrent Anthropic API calls

Every LLM call in the Werkbank goes through complete() or complete_turn(),
which acquire the right semaphore before touching the network.

Public API
----------
complete(system, messages, *, model, user_id, token, ...) -> str
    One-shot call. Returns assistant text. Used by Planner, Critic, Synthesizer,
    Compaction. Semaphore is held for the duration of the network call only.

complete_turn(system, messages, *, model, user_id, token, tools, ...) -> tuple
    One-shot call that also returns tool-call info and stop_reason.
    Used by the Worker to drive its own agentic loop (Phase 4).
    Returns (text: str, tool_calls: list[dict], stop_reason: str)

create_llm(model, user_id, token) -> Callable
    Returns a partial-applied complete() with model/user/token bound.
    Inject into roles that only need one-shot completion.

complete_structured(system, messages, *, model, user_id, token, json_schema, tool_name, ...) -> dict
    Structured-output call. Returns parsed dict.
    Ollama: uses format=json_schema.  Claude: forced tool-use with schema.
    Used by Splitter.

make_tool_result_message(tool_call_id, result_text, model) -> dict
    Build the correctly-formatted tool-result message for the next LLM turn
    (Claude vs OpenAI-compat format differs).

is_api_model(model) -> bool
    True for "claude-*" models.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from config.settings import settings
from services.credential_store import load_credentials

# ── Concurrency lanes ─────────────────────────────────────────────────────────

_API_CONCURRENCY = 3  # max parallel Anthropic calls; becomes a Setting in Phase 7

_LOCAL_SEM: asyncio.Semaphore = asyncio.Semaphore(1)
_API_SEM: asyncio.Semaphore = asyncio.Semaphore(_API_CONCURRENCY)


# ── Helpers ───────────────────────────────────────────────────────────────────

_THINK_RE = re.compile(
    r"<think(?:ing)?>(.*?)</think(?:ing)?>", re.DOTALL | re.IGNORECASE
)


def _strip_thinking(text: str, reasoning_content: str = "") -> str:
    """Remove thinking blocks from LLM output. Discards reasoning_content entirely."""
    return _THINK_RE.sub("", text).strip()


def is_api_model(model: str) -> bool:
    return model.startswith("claude-")


def _get_registry_cfg(model: str, user_id: str, token: str) -> dict | None:
    """Return model_registry config for model name, or None if not found."""
    try:
        from services.model_registry import get_by_name

        return get_by_name(model, user_id, token)
    except Exception:
        return None


def is_openai_compat_model(model: str, user_id: str, token: str) -> bool:
    """True for registry models with openai_compatible backend (not claude-*, not Ollama native)."""
    cfg = _get_registry_cfg(model, user_id, token)
    if cfg:
        return cfg.get("backend") == "openai_compatible"
    return False


def is_anthropic_backend(model: str, user_id: str, token: str) -> bool:
    """True if this model should use Anthropic message format.

    Covers both bare claude-* model names and registry models with backend='anthropic'
    (e.g. MiniMax or any third-party Anthropic-API-compatible endpoint).
    """
    if is_api_model(model):
        return True
    cfg = _get_registry_cfg(model, user_id, token)
    return bool(cfg and cfg.get("backend") == "anthropic")


def _sem_for(model: str, user_id: str, token: str) -> asyncio.Semaphore:
    """Pick semaphore: registry lane field → fallback is_api_model."""
    cfg = _get_registry_cfg(model, user_id, token)
    if cfg:
        return _API_SEM if cfg.get("lane") == "api" else _LOCAL_SEM
    return _API_SEM if is_api_model(model) else _LOCAL_SEM


def _touch_watchdog(sem: asyncio.Semaphore) -> None:
    """Reset the Ollama idle-shutdown timer for local-lane calls.

    Without this, a long werkbank run with no chat activity looks idle to
    services.ollama_watchdog, which then SSH-shuts-down the Ollama box
    mid-task. Call before AND after each local LLM call (generations can
    outlast the idle window)."""
    if sem is _LOCAL_SEM:
        try:
            from services.ollama_watchdog import touch
            touch()
        except Exception:
            pass


def _get_api_key(user_id: str, token: str) -> str:
    """Resolve the Anthropic API key from the user's stored credentials.

    Only a fallback for registry entries that carry no key of their own; keys
    belong on the model in Settings > AI Models.
    """
    if not token:
        return ""
    creds = load_credentials(user_id, token)
    return creds.get("llm", {}).get("anthropic_api_key", "")


def make_tool_result_message(tool_call_id: str, result_text: str, model: str) -> dict:
    """Build the correctly-formatted tool-result message for the next LLM turn."""
    if is_api_model(model):
        return {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_call_id,
                    "content": result_text,
                }
            ],
        }
    else:
        return {"role": "tool", "tool_call_id": tool_call_id, "content": result_text}


# ── Backend implementations ───────────────────────────────────────────────────


async def _call_openai_compatible(
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    messages: list[dict],
    tools: list[dict] | None,
    max_tokens: int,
    temperature: float,
    think: bool | None = None,
) -> tuple[str, list[dict], str]:
    """OpenAI-compatible API (Ollama /v1, MiniMax, Moonshot, etc.)."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    oai_messages = [{"role": "system", "content": system}] + messages
    payload: dict[str, Any] = {
        "model": model,
        "messages": oai_messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }
    if tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": dict(t["input_schema"]),
                },
            }
            for t in tools
        ]
    if think is not None:
        payload["think"] = think  # Ollama/qwen3 specific

    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()

    choice = resp.json().get("choices", [{}])[0]
    msg = choice.get("message", {})
    _raw_content = msg.get("content") or ""
    _raw_thinking = msg.get("reasoning_content") or msg.get("thinking") or ""
    if _raw_content.strip():
        text = _strip_thinking(_raw_content, _raw_thinking)
    elif _raw_thinking:
        # Model (e.g. Qwen3) put actual answer inside thinking block, content empty —
        # extract the post-think answer from reasoning_content.
        text = _strip_thinking(_raw_thinking)
    else:
        text = ""

    raw_tool_calls: list[dict] = msg.get("tool_calls") or []
    tool_calls = []
    for i, tc in enumerate(raw_tool_calls):
        fn = tc.get("function", {})
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        tool_calls.append(
            {
                "name": fn.get("name", ""),
                "id": tc.get("id", f"oai_{i}"),
                "input": args,
            }
        )

    return text, tool_calls, "tool_use" if tool_calls else "end_turn"


async def _call_claude(
    api_key: str,
    model: str,
    system: str,
    messages: list[dict],
    tools: list[dict] | None,
    max_tokens: int,
    temperature: float,
    base_url: str = "",
) -> tuple[str, list[dict], str]:
    """One Claude call. Returns (text, tool_calls, stop_reason)."""
    import anthropic

    client_kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url
    client = anthropic.AsyncAnthropic(**client_kwargs)

    kwargs: dict[str, Any] = dict(
        model=model,
        system=system,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
    )
    if tools:
        kwargs["tools"] = tools

    response = await client.messages.create(**kwargs)

    text = "\n".join(b.text for b in response.content if b.type == "text")
    # Fallback: if model put answer in thinking blocks and content is empty
    if not text.strip():
        thinking = "\n".join(
            b.thinking
            for b in response.content
            if hasattr(b, "thinking") and b.thinking
        )
        text = _strip_thinking(thinking)

    tool_calls = [
        {"name": b.name, "id": b.id, "input": b.input}
        for b in response.content
        if b.type == "tool_use"
    ]
    return text, tool_calls, response.stop_reason




async def _raw_call(
    model: str,
    user_id: str,
    token: str,
    system: str,
    messages: list[dict],
    tools: list[dict] | None,
    max_tokens: int,
    temperature: float,
    think: bool | None = None,
) -> tuple[str, list[dict], str]:
    """Dispatch to the right backend. No semaphore — callers acquire before calling."""
    cfg = _get_registry_cfg(model, user_id, token)
    if cfg:
        backend = cfg.get("backend", "openai_compatible")
        if backend == "anthropic":
            # Mirror chat page: fall back to per-user key if model config key is empty
            api_key = cfg.get("api_key", "") or _get_api_key(user_id, token)
            return await _call_claude(
                api_key,
                cfg["model"],
                system,
                messages,
                tools,
                max_tokens,
                temperature,
                base_url=cfg.get("base_url", ""),
            )
        else:
            return await _call_openai_compatible(
                cfg.get("base_url", ""),
                cfg.get("api_key", ""),
                cfg["model"],
                system,
                messages,
                tools,
                max_tokens,
                temperature,
                think=think,
            )
    if is_api_model(model):
        api_key = _get_api_key(user_id, token)
        return await _call_claude(
            api_key, model, system, messages, tools, max_tokens, temperature
        )
    else:
        return await _call_openai_compatible(
            f"{settings.ollama_server.rstrip('/')}/v1",
            "",
            model,
            system,
            messages,
            tools,
            max_tokens,
            temperature,
            think=think,
        )


# ── Public API ────────────────────────────────────────────────────────────────


async def complete(
    system: str,
    messages: list[dict],
    *,
    model: str,
    user_id: str,
    token: str,
    tools: list[dict] | None = None,
    max_tokens: int = 12_000,
    temperature: float = 0.3,
    think: bool | None = None,
) -> str:
    """Semaphore-protected one-shot call. Returns text only.

    Used by Planner, Critic, Synthesizer, Compaction — roles that never loop.
    think=False disables qwen3-style thinking tokens (Ollama only, ignored for Claude).
    """
    sem = _sem_for(model, user_id, token)
    async with sem:
        _touch_watchdog(sem)
        text, _, _ = await _raw_call(
            model,
            user_id,
            token,
            system,
            messages,
            tools,
            max_tokens,
            temperature,
            think=think,
        )
        _touch_watchdog(sem)
    return text


async def complete_turn(
    system: str,
    messages: list[dict],
    *,
    model: str,
    user_id: str,
    token: str,
    tools: list[dict],
    max_tokens: int = 16_000,
    temperature: float = 0.5,
) -> tuple[str, list[dict], str]:
    """Semaphore-protected one-shot call. Returns (text, tool_calls, stop_reason).

    Used by the Worker to drive its own agentic loop (Phase 4).
    tool_calls format: [{"name": str, "id": str, "input": dict}]
    stop_reason: "end_turn" | "tool_use"
    """
    sem = _sem_for(model, user_id, token)
    async with sem:
        _touch_watchdog(sem)
        try:
            return await _raw_call(
                model, user_id, token, system, messages, tools, max_tokens, temperature
            )
        finally:
            _touch_watchdog(sem)


async def complete_structured(
    system: str,
    messages: list[dict],
    *,
    model: str,
    user_id: str,
    token: str,
    json_schema: dict,
    tool_name: str = "output",
    max_tokens: int = 12_000,
    temperature: float = 0.1,
) -> dict:
    """Structured-output call. Returns a parsed dict (not text).

    All backends: forced tool-use with json_schema as parameters; returns the
    tool's ``input`` / ``arguments`` dict directly.

    Raises:
        ValueError: if the response cannot be parsed / tool not called.
    """
    sem = _sem_for(model, user_id, token)
    async with sem:
        _touch_watchdog(sem)
        cfg = _get_registry_cfg(model, user_id, token)
        if cfg:
            backend = cfg.get("backend", "openai_compatible")
            if backend == "anthropic":
                api_key = cfg.get("api_key", "") or _get_api_key(user_id, token)
                return await _call_claude_structured(
                    api_key,
                    cfg["model"],
                    system,
                    messages,
                    json_schema,
                    tool_name,
                    max_tokens,
                    temperature,
                    base_url=cfg.get("base_url", ""),
                )
            else:
                return await _call_openai_compatible_structured(
                    cfg.get("base_url", ""),
                    cfg.get("api_key", ""),
                    cfg["model"],
                    system,
                    messages,
                    json_schema,
                    tool_name,
                    max_tokens,
                    temperature,
                )
        if is_api_model(model):
            return await _call_claude_structured(
                _get_api_key(user_id, token),
                model,
                system,
                messages,
                json_schema,
                tool_name,
                max_tokens,
                temperature,
            )
        else:
            return await _call_openai_compatible_structured(
                f"{settings.ollama_server.rstrip('/')}/v1",
                "",
                model,
                system,
                messages,
                json_schema,
                tool_name,
                max_tokens,
                temperature,
            )


async def _call_claude_structured(
    api_key: str,
    model: str,
    system: str,
    messages: list[dict],
    json_schema: dict,
    tool_name: str,
    max_tokens: int,
    temperature: float,
    base_url: str = "",
) -> dict:
    import anthropic

    client_kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        client_kwargs["base_url"] = base_url
    client = anthropic.AsyncAnthropic(**client_kwargs)
    tool = {
        "name": tool_name,
        "description": "Output the structured result according to the schema.",
        "input_schema": json_schema,
    }
    response = await client.messages.create(
        model=model,
        system=system,
        messages=messages,
        tools=[tool],
        tool_choice={"type": "tool", "name": tool_name},
        max_tokens=max_tokens,
        temperature=temperature,
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == tool_name:
            return block.input
    raise ValueError(f"Claude did not call tool '{tool_name}'")



async def _call_openai_compatible_structured(
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    messages: list[dict],
    json_schema: dict,
    tool_name: str,
    max_tokens: int,
    temperature: float,
) -> dict:
    """Structured output via forced tool-use for OpenAI-compatible APIs."""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system}] + messages,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": "Output structured result.",
                    "parameters": json_schema,
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": tool_name}},
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": False,
    }

    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            f"{base_url.rstrip('/')}/chat/completions", json=payload, headers=headers
        )
        resp.raise_for_status()

    msg = resp.json().get("choices", [{}])[0].get("message", {})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        if fn.get("name") == tool_name:
            args = fn.get("arguments", "{}")
            if isinstance(args, str):
                try:
                    return json.loads(args)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"OpenAI-compat structured output invalid JSON: {exc}"
                    ) from exc
            return args
    raise ValueError(f"OpenAI-compat model did not call tool '{tool_name}'")


def _create_llm_from_config(cfg: dict):
    """Return an llm callable driven by a model_registry config dict."""
    backend = cfg.get("backend", "openai_compatible")
    lane_sem = _LOCAL_SEM if cfg.get("lane") == "local" else _API_SEM

    async def _llm(
        system: str,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        max_tokens: int = 12_000,
        temperature: float = 0.3,
        think: bool | None = None,
    ) -> str:
        async with lane_sem:
            _touch_watchdog(lane_sem)
            if backend == "anthropic":
                text, _, _ = await _call_claude(
                    cfg.get("api_key", ""),
                    cfg["model"],
                    system,
                    messages,
                    tools,
                    max_tokens,
                    temperature,
                    base_url=cfg.get("base_url", ""),
                )
            else:  # openai_compatible
                text, _, _ = await _call_openai_compatible(
                    cfg.get("base_url", ""),
                    cfg.get("api_key", ""),
                    cfg["model"],
                    system,
                    messages,
                    tools,
                    max_tokens,
                    temperature,
                    think=think,
                )
        _touch_watchdog(lane_sem)
        return text

    return _llm


def create_llm(model: str, user_id: str, token: str):
    """Return a complete() callable for the given model.

    Checks the per-user model_registry first (by name); falls back to the
    legacy Ollama/Claude routing for raw model name strings.

    Signature of returned callable:
        async (system, messages, *, tools=None, max_tokens=4000, temperature=0.3, think=None) -> str
    """
    # Registry lookup
    try:
        from services.model_registry import get_by_name

        cfg = get_by_name(model, user_id, token)
        if cfg:
            # Resolve api_key now while we still have user_id/token — the closure
            # returned by _create_llm_from_config has no access to them.
            if cfg.get("backend") == "anthropic" and not cfg.get("api_key"):
                cfg = dict(cfg)
                cfg["api_key"] = _get_api_key(user_id, token)
            return _create_llm_from_config(cfg)
    except Exception:
        pass

    # Legacy fallback
    async def _llm(
        system: str,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        max_tokens: int = 4_000,
        temperature: float = 0.3,
        think: bool | None = None,
    ) -> str:
        return await complete(
            system,
            messages,
            model=model,
            user_id=user_id,
            token=token,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            think=think,
        )

    return _llm
