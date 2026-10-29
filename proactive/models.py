"""Which model runs a user's proactive jobs — resolved here and nowhere else.

Order (CLAUDE.md, decision 2 in `docs/proactive-diagnostics.md`):

1. `PROACTIVE_MODEL_FIXED` — every user, no choice, ignores everything below.
2. The user's own choice from their model registry, if it is allowed.
3. `PROACTIVE_MODEL_DEFAULT`.
4. The user's chat model, if it is allowed.

"Allowed" = enabled in their registry, its model id on `PROACTIVE_MODEL_ALLOWLIST`
(when the list is set), and — for a model whose service is not on the local
lane, i.e. a cloud model — `PROACTIVE_CLOUD_ALLOWED`. FIXED and DEFAULT are
model ids on `OLLAMA_SERVER` (the operator's host), so they are always local.

A stored choice that is no longer allowed is not an error: it falls through to
the next step with a log line, and comes back if the operator allows it again
(stored choices are never deleted).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger("proactive.models")

Lane = Literal["local", "cloud"]
Source = Literal["fixed", "user", "default", "chat"]


@dataclass(frozen=True)
class ResolvedModel:
    #: What `werkbank.llm_lane` is called with: a registry display name for
    #: user/chat, a model id on OLLAMA_SERVER for fixed/default.
    name: str
    lane: Lane
    source: Source
    model_id: str = ""

    @property
    def key(self) -> str:
        """Grouping key: jobs with the same key need no model swap between them."""
        return f"{self.lane}:{self.model_id or self.name}"

    @property
    def uses_registry(self) -> bool:
        return self.source in ("user", "chat")


def _registry(user: str, token: str) -> list[dict]:
    try:
        from services.model_registry import get_models

        return get_models(user, token)
    except Exception as exc:  # unreadable store = no registry models
        log.warning("model registry of %s unreadable: %s", user, exc)
        return []


def lane_of(entry: dict) -> Lane:
    return "local" if entry.get("lane") == "local" else "cloud"


def allowed_registry_models(user: str, token: str, *, registry: list[dict] | None = None) -> list[dict]:
    """The user's models they may pick for proactive work (the settings dropdown)."""
    from config.proactive import proactive_settings

    cfg = proactive_settings()
    allow = set(cfg.model_allowlist)
    out = []
    for m in registry if registry is not None else _registry(user, token):
        if not m.get("enabled", True):
            continue
        if allow and m.get("model", "") not in allow:
            continue
        if lane_of(m) == "cloud" and not cfg.proactive_cloud_allowed:
            continue
        out.append(m)
    return out


def _from_registry(name: str, user: str, allowed: list[dict], source: Source) -> ResolvedModel | None:
    if not name:
        return None
    hit = next((m for m in allowed if m.get("name") == name), None)
    if hit is None:
        log.info("%s: %s model %r is not allowed for proactive jobs — falling back", user, source, name)
        return None
    return ResolvedModel(name=name, lane=lane_of(hit), source=source, model_id=hit.get("model", ""))


def resolve_model(user: str, token: str | None, prefs=None) -> ResolvedModel | None:
    """The model for `user`'s proactive LLM jobs, or None (then their LLM jobs
    do not run; jobs without LLM still do)."""
    from config.proactive import proactive_settings

    cfg = proactive_settings()
    if cfg.proactive_model_fixed:
        return ResolvedModel(cfg.proactive_model_fixed, "local", "fixed", cfg.proactive_model_fixed)

    if prefs is None:
        from proactive import prefs as pprefs

        prefs = pprefs.get(user)
    allowed = allowed_registry_models(user, token or "") if token else []

    found = _from_registry(prefs.model, user, allowed, "user")
    if found:
        return found
    if cfg.proactive_model_default:
        return ResolvedModel(cfg.proactive_model_default, "local", "default", cfg.proactive_model_default)
    return _from_registry(prefs.chat_model, user, allowed, "chat")
