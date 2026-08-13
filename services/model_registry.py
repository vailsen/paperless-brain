"""services/model_registry.py — ordered per-user LLM model registry.

Each model config stored in encrypted credentials under key "llm_models":
    {
        "id":       str,   # short unique slug
        "name":     str,   # display name (used as model ID in dropdowns)
        "backend":  "anthropic" | "openai_compatible",
        "model":    str,   # model name sent to the API
        "base_url": str,   # openai_compatible: API base URL (e.g. http://ollama:11434/v1)
        "api_key":  str,   # API key (stored encrypted)
        "lane":     "local" | "api",
        "enabled":  bool,

        # Tool behaviour (openai_compatible only, all optional):
        "supports_tools":        bool,  # False = never offer tools to this model
        "force_tool_first_turn": bool,  # require a tool on personal questions
    }

"OpenAI-compatible" describes the wire format, not the behaviour behind it. Some
deployments drop `tools` unless `tool_choice` is set, some models never emit a
tool call however they are asked, and a model that answers a question about the
user's own mail from its weights is worse than one that says it cannot. Hence
per-model flags rather than one global assumption — see the tool-use guard in
`services/chat_service.py`.
"""

from __future__ import annotations

import uuid

from services.credential_store import load_credentials, save_credentials

_KEY = "llm_models"


def get_models(username: str, token: str) -> list[dict]:
    creds = load_credentials(username, token)
    return creds.get(_KEY, [])


def save_models(username: str, token: str, models: list[dict]) -> None:
    creds = load_credentials(username, token)
    creds[_KEY] = models
    save_credentials(username, token, creds)


def model_names(username: str, token: str) -> list[str]:
    """Ordered list of enabled model names for dropdowns."""
    return [m["name"] for m in get_models(username, token) if m.get("enabled", True)]


def get_by_name(name: str, username: str, token: str) -> dict | None:
    return next((m for m in get_models(username, token) if m["name"] == name), None)


def new_model(
    name: str,
    backend: str,
    model: str,
    base_url: str = "",
    api_key: str = "",
    lane: str = "api",
) -> dict:
    return {
        "id":       str(uuid.uuid4())[:8],
        "name":     name,
        "backend":  backend,
        "model":    model,
        "base_url": base_url,
        "api_key":  api_key,
        "lane":     lane,
        "enabled":  True,
        "supports_tools":        True,
        "force_tool_first_turn": False,
    }


def tool_config(model: dict) -> dict:
    """Tool flags of a stored model config, with defaults for older entries.

    Kept here rather than at the call site so a model saved before these flags
    existed behaves exactly as it did before: tools on, nothing forced.
    """
    return {
        "supports_tools": bool(model.get("supports_tools", True)),
        "force_tool_first_turn": bool(model.get("force_tool_first_turn", False)),
        "tool_choice_mode": str(model.get("tool_choice_mode") or "auto"),
    }
