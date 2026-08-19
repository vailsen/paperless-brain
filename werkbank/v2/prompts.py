"""Werkbank v2 — role prompts, with a user override.

The prompts live as markdown files next to the code so they are versioned and
diffable, which is what an invariant-carrying prompt needs. A user override is
stored in `werkbank_settings` and wins when it is non-empty, so the default file
stays intact and "reset" is a delete, never a re-paste.

What an override can and cannot do is the point: it changes what the model is
*asked*. It cannot change what is *checked* — D1–D9 run in `checks.py` after the
model has spoken and are not reachable from here. That is the whole reason the
prompts are safe to expose.
"""

from __future__ import annotations

from pathlib import Path

from werkbank import settings_store

_DIR = Path(__file__).parent / "prompts"

# role -> (prompt file, default token limit)
ROLES: dict[str, tuple[Path, int]] = {
    "briefer": (_DIR / "briefer.md", 4_000),
    "planner": (_DIR / "planner.md", 8_000),
    "plan_critic": (_DIR / "plan_critic.md", 4_000),
    "fact_critic": (_DIR / "fact_critic.md", 8_000),
    "writer": (_DIR / "writer.md", 16_000),
    "contradiction_checker": (_DIR / "agents" / "contradiction_checker.md", 4_000),
}

# Roles whose prompt is an agent definition and belongs to the archetype editor,
# not here — listing them twice would give the same text two owners.
AGENT_PROMPT_DIR = _DIR / "agents"


def prompt_key(role: str) -> str:
    return f"wb2_prompt_{role}"


def tokens_key(role: str) -> str:
    return f"wb2_tokens_{role}"


def default_text(role: str) -> str:
    path, _ = ROLES[role]
    return path.read_text(encoding="utf-8")


def system_prompt(role: str) -> str:
    """The prompt actually sent: the user's override, else the shipped file."""
    stored = settings_store.get(prompt_key(role))
    return stored if stored.strip() else default_text(role)


def set_override(role: str, text: str) -> None:
    """Store an override. Empty (or identical to the default) clears it, so a
    later change to the shipped prompt still reaches the user."""
    value = (text or "").strip()
    settings_store.set_value(
        prompt_key(role), "" if not value or value == default_text(role).strip() else value
    )


def is_overridden(role: str) -> bool:
    return bool(settings_store.get(prompt_key(role)).strip())


def token_limit(role: str) -> int:
    _, default = ROLES.get(role, (None, 12_000))
    return settings_store.get_tokens(tokens_key(role), default)


def set_token_limit(role: str, value: int) -> None:
    settings_store.set_value(tokens_key(role), str(int(value)))
