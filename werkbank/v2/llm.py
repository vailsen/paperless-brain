"""Werkbank v2 — the one way a role talks to a model.

Thin layer on `werkbank.llm_lane.complete_structured`, which already does the
hard part (per-backend forced tool-use with a JSON schema, lane semaphores).
What v2 adds is the part the architecture leans on:

- **A pydantic model in, a pydantic model out.** A schema violation is fed back
  to the model as a concrete error list and retried, capped. No caller ever
  sees a half-parsed dict.
- **Every call is logged in full.** Phase 4 has to *prove* that the fact critic
  never saw the runner's narrative; an assertion about a prompt is worth
  nothing without the prompt. The log is scratch data, one file per run.
- **`current_date` is injected here, not in each prompt.** The architecture
  requires it in every prompt; putting it in the one place all calls pass
  through is the difference between an invariant and a convention.
- **Temperature belongs to the role**, not the call site. The critic runs cold
  (0.1) on the same model as the runner, which is what makes its verdict worth
  anything when there is only one model available.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from config.settings import settings

_log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# The runner may explore; everything that judges runs cold. Same model for all
# of them in a single-model setup, so temperature is the only dial left that
# separates "produce" from "check".
ROLE_TEMPERATURE: dict[str, float] = {
    "briefer": 0.2,
    "planner": 0.2,
    "plan_critic": 0.1,
    "runner": 0.4,
    "fact_critic": 0.1,
    "contradiction_checker": 0.1,
    "synthesizer": 0.2,
    "writer": 0.3,
}
DEFAULT_TEMPERATURE = 0.2
MAX_SCHEMA_RETRIES = 2
RETRY_BACKOFF_S = 2.0

# Failures worth another attempt: an unusable answer, or a model server having a
# bad moment. Everything else propagates — a wrong API key is not a hiccup.
TRANSIENT_ERRORS = (ValueError, httpx.HTTPError)


def _token_limit(role: str) -> int:
    """Per-role output cap, user-settable. Imported lazily: `prompts` reads the
    settings DB, and `llm` is imported by modules that must stay import-cheap."""
    from werkbank.v2 import prompts

    try:
        return prompts.token_limit(role)
    except Exception:                                       # noqa: BLE001
        return 12_000


@dataclass
class LLMContext:
    """Everything a call needs that is not the prompt."""

    model: str
    user_id: str
    token: str = ""
    run_id: str = ""
    log_dir: Path | None = None

    def log_path(self) -> Path:
        base = self.log_dir or (Path(settings.app_path) / "data" / "werkbank_v2_logs")
        base.mkdir(parents=True, exist_ok=True)
        return base / f"{self.run_id or 'no-run'}.jsonl"


@dataclass
class PromptLog:
    """In-memory mirror of the file log, so tests can assert on it directly."""

    entries: list[dict] = field(default_factory=list)

    def systems_for(self, role: str) -> list[str]:
        return [e["system"] for e in self.entries if e["role"] == role]

    def messages_for(self, role: str) -> list[list[dict]]:
        return [e["messages"] for e in self.entries if e["role"] == role]


def current_date_block(now: datetime | None = None) -> str:
    """Date, weekday and calendar week — injected into every single prompt.

    Without it a model dates "last quarter" from its training cut-off, which is
    a silent, confident, wrong answer rather than a visible failure.
    """
    now = now or datetime.now()
    iso_year, iso_week, _ = now.isocalendar()
    weekday = [
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    ][now.weekday()]
    return (
        f"Current date: {now:%Y-%m-%d} ({weekday}), calendar week {iso_week}/{iso_year}. "
        f"Use it for every relative date. Never date anything from your training data."
    )


def _schema_for(model_cls: type[BaseModel]) -> dict:
    """JSON schema for the structured-output call."""
    return model_cls.model_json_schema()


def _describe(exc: ValidationError) -> str:
    """Validation errors in the form a model can act on: field, rule, value."""
    lines = []
    for err in exc.errors()[:12]:
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        lines.append(f"- {loc}: {err['msg']}")
    return "\n".join(lines)


async def call_structured(
    role: str,
    system: str,
    user: str,
    schema: type[T],
    ctx: LLMContext,
    *,
    prompt_log: PromptLog | None = None,
    sanitize=None,
    max_retries: int = MAX_SCHEMA_RETRIES,
    now: datetime | None = None,
) -> T:
    """One structured call, validated into `schema`. Raises on exhausted retries.

    `sanitize` runs on the raw payload before validation — that is where
    `strip_llm_controlled()` goes, so fields the model must not decide are
    dropped before they can reach a model instance.
    """
    from werkbank.llm_lane import complete_structured

    system = f"{current_date_block(now)}\n\n{system}"
    messages = [{"role": "user", "content": user}]
    temperature = ROLE_TEMPERATURE.get(role, DEFAULT_TEMPERATURE)
    last_error = ""

    for attempt in range(max_retries + 1):
        if attempt:
            # Feed the exact violations back. A generic "invalid JSON, try
            # again" retry mostly reproduces the same output.
            if "cut off" in last_error:
                # Repeating "answer again, same content" here asks for the exact
                # answer that did not fit. The only thing that helps is fewer,
                # shorter facts — an aggregate over a long list instead of one
                # entry per item.
                instruction = (
                    f"Your last answer was cut off before it was complete: {last_error}\n"
                    "Answer again, but shorter: summarise long lists into a few "
                    "aggregate facts with counts and the notable entries, rather "
                    "than one fact per item, and keep every quote to one sentence. "
                    "An answer that fits is worth more than a complete one that is lost."
                )
            elif "did not call tool" in last_error:
                instruction = (
                    "Your last answer was not delivered as a tool call, so it was "
                    f"lost: {last_error}\nAnswer again — same content, but as a "
                    "call to the tool, with nothing outside it."
                )
            else:
                instruction = (
                    "Your previous answer did not match the schema:\n"
                    f"{last_error}\n\nAnswer again, correcting exactly these points."
                )
            messages = messages[:1] + [{"role": "user", "content": instruction}]

        _record(ctx, prompt_log, role, system, messages, temperature, attempt)

        try:
            raw = await complete_structured(
                system,
                messages,
                model=ctx.model,
                user_id=ctx.user_id,
                token=ctx.token,
                json_schema=_schema_for(schema),
                tool_name=role,
                temperature=temperature,
                max_tokens=_token_limit(role),
            )
        except TRANSIENT_ERRORS as exc:
            # Two kinds of failure, one answer: retry.
            #
            # `ValueError` — the backend produced no usable answer, typically a
            # local model writing prose where a tool call was required.
            # `HTTPError` — the model server itself faltered: one observed run
            # lost a subtask to a single `500 Internal Server Error` from Ollama
            # after it had already made thirteen tool calls.
            #
            # Either way the alternative is discarding research that has already
            # been paid for.
            last_error = str(exc)
            if isinstance(exc, httpx.HTTPError):
                # A server that just fell over needs a moment, not an instant
                # retry with the same load.
                await asyncio.sleep(RETRY_BACKOFF_S * (attempt + 1))
            _log.warning("werkbank v2 %s: %s (attempt %s)", role, exc, attempt + 1)
            _record(ctx, prompt_log, role, system, messages, temperature, attempt,
                    error=last_error)
            if attempt == max_retries:
                raise
            continue
        payload = sanitize(raw) if sanitize else raw
        try:
            parsed = schema.model_validate(payload)
        except ValidationError as exc:
            last_error = _describe(exc)
            _log.warning("werkbank v2 %s: schema violation (attempt %s)", role, attempt + 1)
            _record(ctx, prompt_log, role, system, messages, temperature, attempt,
                    response=raw, error=last_error)
            continue
        _record(ctx, prompt_log, role, system, messages, temperature, attempt, response=raw)
        return parsed

    raise ValueError(f"{role}: no schema-valid answer after {max_retries + 1} attempts:\n{last_error}")


def _record(
    ctx: LLMContext,
    prompt_log: PromptLog | None,
    role: str,
    system: str,
    messages: list[dict],
    temperature: float,
    attempt: int,
    *,
    response: dict | None = None,
    error: str = "",
) -> None:
    entry = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "run_id": ctx.run_id,
        "role": role,
        "model": ctx.model,
        "temperature": temperature,
        "attempt": attempt,
        "system": system,
        "messages": messages,
        "response": response,
        "error": error,
    }
    if prompt_log is not None:
        prompt_log.entries.append(entry)
    try:
        with ctx.log_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        # A failed log write must never take a run down with it.
        _log.debug("werkbank v2: could not write the prompt log", exc_info=True)
