"""services/model_registry.py — per-user LLM providers, each with its models.

Stored in the encrypted credentials under ``"llm_providers"``:

    [
      {
        "id":         str,   # short unique slug
        "name":       str,   # display name of the service ("Ollama local", "OpenRouter")
        "backend":    "anthropic" | "openai_compatible",
        "base_url":   str,   # openai_compatible: API base URL (e.g. http://ollama:11434/v1)
        "api_key":    str,   # stored encrypted, like everything in the blob
        "lane":       "local" | "api",
        "enabled":    bool,
        "auto_named": bool,  # name was suggested by the migration, not chosen
        "models": [
          {
            "id":      str,
            "name":    str,  # display name — unique across ALL providers (see below)
            "model":   str,  # model id sent to the API
            "enabled": bool,
            "temperature":       float,
            "max_output_tokens": int,
            # Thinking (anthropic backend only):
            "think":           bool | None,  # None = let the endpoint decide
            "thinking_budget": int,   # compatible endpoints only — Anthropic removed it
            "effort":          str,   # "" | low | medium | high | xhigh | max — first party
            # Tool behaviour (openai_compatible only, all optional):
            "supports_tools":        bool,  # False = never offer tools to this model
            "force_tool_first_turn": bool,  # require a tool on personal questions
          },
        ],
      },
    ]

The address, key and concurrency lane belong to the *service*; a service hosts
many models. Everything that runs a model still sees the flat view of
``get_models()`` — one dict per model with its provider's fields merged in — so
chat, vision, Werkbank and the dashboard did not have to change.

**A model is referenced by its display name** (chat choice, ingest model, memo and
dreaming model, Werkbank runs). That is why display names are unique across all
providers: the ingest model is a *global* setting resolved in whichever user's
registry triggers the sync, and a name — unlike a per-user id — means the same
thing in every user's registry.

"OpenAI-compatible" describes the wire format, not the behaviour behind it. Some
deployments drop `tools` unless `tool_choice` is set, some models never emit a
tool call however they are asked, and a model that answers a question about the
user's own mail from its weights is worse than one that says it cannot. Hence
per-model flags rather than one global assumption — see the tool-use guard in
`services/chat_service.py`.
"""

from __future__ import annotations

import re
import uuid
from urllib.parse import urlparse

from services.credential_store import load_credentials, save_credentials

_KEY = "llm_providers"
# The flat list of versions before providers existed. Read once to migrate and
# then left in place, untouched, as a fallback copy.
_LEGACY_KEY = "llm_models"

PROVIDER_FIELDS = ("backend", "base_url", "api_key", "lane")


def _slug() -> str:
    return str(uuid.uuid4())[:8]


# ── Base URL ─────────────────────────────────────────────────────────────────

# What people paste from a provider's docs: the full endpoint, not the base.
# The clients append the endpoint themselves (`/chat/completions`; the Anthropic
# SDK `/v1/messages`), so leaving it in doubles the path and the provider answers
# 404 "Not Found" — which says nothing about why.
_OPENAI_SUFFIX_RE = re.compile(r"/(chat/completions|completions)/*$", re.IGNORECASE)
_ANTHROPIC_SUFFIX_RE = re.compile(r"(/v1)?/messages/*$", re.IGNORECASE)


def normalize_base_url(url: str, backend: str = "openai_compatible") -> str:
    url = (url or "").strip()
    pattern = _ANTHROPIC_SUFFIX_RE if backend == "anthropic" else _OPENAI_SUFFIX_RE
    return pattern.sub("", url).rstrip("/")


def suggest_provider_name(backend: str, base_url: str) -> str:
    """A readable default for a service, from its address."""
    url = normalize_base_url(base_url, backend)
    if not url:
        return "Anthropic" if backend == "anthropic" else "OpenAI-compatible"
    parsed = urlparse(url if "://" in url else f"http://{url}")
    host = (parsed.hostname or url).lower()
    known = {
        "openrouter.ai": "OpenRouter",
        "api.openai.com": "OpenAI",
        "api.anthropic.com": "Anthropic",
        "api.minimaxi.chat": "MiniMax",
        "api.minimax.io": "MiniMax",
        "api.moonshot.ai": "Moonshot",
        "api.deepseek.com": "DeepSeek",
        "api.mistral.ai": "Mistral",
        "generativelanguage.googleapis.com": "Google Gemini",
        "router.requesty.ai": "Requesty",
        "api.portkey.ai": "Portkey",
    }
    if host in known:
        return known[host]
    if parsed.port == 11434:
        return f"Ollama ({host})"
    return f"{host}:{parsed.port}" if parsed.port else host


# ── Migration from the flat list ─────────────────────────────────────────────


def migrate_flat(models: list[dict]) -> list[dict]:
    """Group the old one-entry-per-model list into providers.

    Entries with the same backend, base URL and API key become one provider —
    the key is part of the identity because a provider entry holds exactly one.
    Every model keeps its id and its display name, so everything that refers to
    a model by name keeps resolving. The provider name is only a suggestion and
    marked as such until the user saves the provider once.
    """
    providers: list[dict] = []
    by_key: dict[tuple, dict] = {}
    for m in models or []:
        if not isinstance(m, dict) or not m.get("name"):
            continue
        backend = m.get("backend") or "openai_compatible"
        base_url = normalize_base_url(m.get("base_url", ""), backend)
        api_key = m.get("api_key") or ""
        key = (backend, base_url.lower(), api_key)
        prov = by_key.get(key)
        if prov is None:
            prov = {
                "id": _slug(),
                "name": suggest_provider_name(backend, base_url),
                "backend": backend,
                "base_url": base_url,
                "api_key": api_key,
                "lane": m.get("lane") or "api",
                "enabled": True,
                "auto_named": True,
                "models": [],
            }
            by_key[key] = prov
            providers.append(prov)
        model = {k: v for k, v in m.items() if k not in PROVIDER_FIELDS}
        model.setdefault("id", _slug())
        model.setdefault("enabled", True)
        prov["models"].append(model)
    # Two services can suggest the same name (two Ollama boxes on one host
    # name, the same service with two keys); number them so they stay apart.
    seen: dict[str, int] = {}
    for prov in providers:
        n = seen.get(prov["name"], 0) + 1
        seen[prov["name"]] = n
        if n > 1:
            prov["name"] = f"{prov['name']} ({n})"
    return providers


# ── Storage ──────────────────────────────────────────────────────────────────


def get_providers(username: str, token: str) -> list[dict]:
    """The user's providers, migrating the old flat list on first read."""
    creds = load_credentials(username, token)
    if _KEY in creds:
        return creds[_KEY] or []
    legacy = creds.get(_LEGACY_KEY) or []
    if not legacy:
        return []
    providers = migrate_flat(legacy)
    creds[_KEY] = providers
    if username and token:
        save_credentials(username, token, creds)
    return providers


def save_providers(username: str, token: str, providers: list[dict]) -> None:
    creds = load_credentials(username, token)
    creds[_KEY] = providers
    save_credentials(username, token, creds)


def flatten(providers: list[dict]) -> list[dict]:
    """One dict per model, its provider's fields merged in, in display order.

    A disabled provider disables all of its models.
    """
    out: list[dict] = []
    for prov in providers or []:
        for m in prov.get("models") or []:
            flat = dict(m)
            for k in PROVIDER_FIELDS:
                flat[k] = prov.get(k, "" if k != "lane" else "api")
            flat["backend"] = flat["backend"] or "openai_compatible"
            flat["lane"] = flat["lane"] or "api"
            flat["provider_id"] = prov.get("id", "")
            flat["provider_name"] = prov.get("name", "")
            flat["enabled"] = bool(m.get("enabled", True)) and bool(prov.get("enabled", True))
            out.append(flat)
    return out


def get_models(username: str, token: str) -> list[dict]:
    return flatten(get_providers(username, token))


def model_names(username: str, token: str) -> list[str]:
    """Ordered list of enabled model names for dropdowns."""
    return [m["name"] for m in get_models(username, token) if m.get("enabled", True)]


def get_by_name(name: str, username: str, token: str) -> dict | None:
    return next((m for m in get_models(username, token) if m["name"] == name), None)


def name_taken(providers: list[dict], name: str, except_model_id: str = "") -> bool:
    """Whether another model already uses this display name (case-insensitive)."""
    wanted = (name or "").strip().casefold()
    return any(
        (m.get("name") or "").strip().casefold() == wanted and m.get("id") != except_model_id
        for p in providers or []
        for m in p.get("models") or []
    )


def unique_name(providers: list[dict], name: str, provider_name: str = "") -> str:
    """``name``, or ``name (provider)`` / a number when it is taken."""
    if not name_taken(providers, name):
        return name
    if provider_name and not name_taken(providers, f"{name} ({provider_name})"):
        return f"{name} ({provider_name})"
    n = 2
    while name_taken(providers, f"{name} ({n})"):
        n += 1
    return f"{name} ({n})"


def rename_references(username: str, token: str, old: str, new: str) -> None:
    """Point the per-user model choices at a renamed model.

    The global ingest model and the chat choice live elsewhere (settings store,
    user prefs); the settings page updates those itself.
    """
    if not old or old == new:
        return
    creds = load_credentials(username, token)
    changed = False
    for key in ("memo_model", "dream_model"):
        if creds.get(key) == old:
            creds[key] = new
            changed = True
    if changed:
        save_credentials(username, token, creds)


def new_provider(
    name: str,
    backend: str = "openai_compatible",
    base_url: str = "",
    api_key: str = "",
    lane: str = "api",
) -> dict:
    return {
        "id": _slug(),
        "name": name,
        "backend": backend,
        "base_url": normalize_base_url(base_url, backend),
        "api_key": api_key,
        "lane": lane,
        "enabled": True,
        "auto_named": False,
        "models": [],
    }


def new_model(name: str, model: str = "") -> dict:
    return {
        "id": _slug(),
        "name": name or model,
        "model": model,
        "enabled": True,
        "supports_tools": True,
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
