"""Werkbank v2 — the agent registry.

Three jobs:

1. **Load the defaults** from `config/agents.yaml`, so prompts and tool subsets
   can be changed without a deploy.
2. **Filter by what this user actually has.** An agent whose requirement is not
   met does not exist for that run — it is not in the registry the planner
   sees, so it cannot be assigned, and a question only it could have answered
   becomes a gap with `source_unavailable`. That is the whole point: without
   the filter the planner assigns `comms_researcher`, the run finds no mail
   tool, and the model answers from parametric knowledge instead.
3. **Merge the user's own archetypes** and let them be reset. Users keep the
   ability to create and edit archetypes; what was missing is the way back —
   a broken default prompt was previously unrecoverable.

Trust is attached to the *tool*, never to the archetype, so a user-defined
agent cannot grant itself a better source of truth by existing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from config.settings import settings

_log = logging.getLogger(__name__)

REGISTRY_PATH = Path(settings.app_path) / "config" / "agents.yaml"
_FALLBACK_PATH = Path(__file__).resolve().parents[2] / "config" / "agents.yaml"


@dataclass
class AgentSpec:
    id: str
    label: str
    description: str = ""
    tools: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    prompt_file: str = ""
    risk: str = ""
    user_defined: bool = False
    # A user's edited prompt for this agent. Wins over `prompt_file`, the same
    # override-and-reset shape the pipeline roles use.
    prompt_text: str = ""

    def prompt_path(self) -> Path:
        return Path(__file__).parent / self.prompt_file if self.prompt_file else Path()


@dataclass
class Registry:
    agents: dict[str, AgentSpec]
    tool_trust: dict[str, str]
    forbidden_tools: list[str]
    capabilities: dict[str, str]

    def planner_view(self) -> list[dict]:
        """What the planner is told: id, label, what it is for, what it can use."""
        return [
            {
                "id": spec.id,
                "label": spec.label,
                "description": spec.description.strip(),
                "tools": spec.tools,
            }
            for spec in self.agents.values()
        ]

    def trust_for(self, tool: str) -> str:
        """Trust of a tool's results. Unknown tools are worth nothing on purpose."""
        return self.tool_trust.get(tool, "model")


def _load_yaml() -> dict:
    for path in (REGISTRY_PATH, _FALLBACK_PATH):
        try:
            if path.is_file():
                return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            _log.warning("werkbank v2: could not read the agent registry at %s", path)
    return {}


def load_defaults() -> Registry:
    """The registry as shipped — no user edits, no availability filtering."""
    raw = _load_yaml()
    forbidden = list(raw.get("forbidden_tools") or [])
    agents: dict[str, AgentSpec] = {}
    for agent_id, cfg in (raw.get("agents") or {}).items():
        agents[agent_id] = AgentSpec(
            id=agent_id,
            label=cfg.get("label", agent_id),
            description=cfg.get("description", ""),
            tools=[t for t in (cfg.get("tools") or []) if t not in forbidden],
            requires=list(cfg.get("requires") or []),
            prompt_file=cfg.get("prompt_file", ""),
            risk=cfg.get("risk", ""),
        )
    return Registry(
        agents=agents,
        tool_trust=dict(raw.get("tool_trust") or {}),
        forbidden_tools=forbidden,
        capabilities=dict(raw.get("capabilities") or {}),
    )


def user_capabilities(username: str, token: str) -> set[str]:
    """What this user has configured. Wrong answers here cost real work, so it
    reads the same credential store the chat tools read at call time."""
    caps = {"paperless", "vault"}
    if getattr(settings, "searxng_host", ""):
        caps.add("web")
    try:
        from services.credential_store import load_credentials

        creds = load_credentials(username, token) if username and token else {}
    except Exception:      # no session, encrypted store unavailable
        creds = {}
    imap = creds.get("imap") or {}
    if imap.get("host") and imap.get("username") and imap.get("password"):
        caps.add("mail")
    if creds.get("calendar"):
        caps.add("calendar")
    return caps


def available_agents(
    capabilities: set[str], *, user_agents: list[AgentSpec] | None = None
) -> Registry:
    """The registry for one run: defaults + user archetypes, minus the impossible.

    An agent survives only when every one of its requirements is met. This is
    what the planner is shown, and it is deliberately the *only* place the
    filtering happens — a planner that sees an unavailable agent will use it.
    """
    registry = load_defaults()
    for spec in user_agents or []:
        spec.tools = [t for t in spec.tools if t not in registry.forbidden_tools]
        shipped = registry.agents.get(spec.id)
        if shipped is None:
            registry.agents[spec.id] = spec
            continue
        # An edited shipped agent keeps its requirement and its label: the
        # capability filter is what stops the planner from assigning an agent
        # whose tools this user does not have, and editing a prompt must not
        # switch that off. Only the parts the dialog actually edits are taken.
        shipped.tools = spec.tools
        shipped.description = spec.description or shipped.description
        shipped.prompt_text = spec.prompt_text

    registry.agents = {
        agent_id: spec
        for agent_id, spec in registry.agents.items()
        if all(req in capabilities for req in spec.requires)
    }
    return registry


# ── User archetypes: read, write, reset ───────────────────────────────────────


def load_user_agents(username: str) -> list[AgentSpec]:
    """Archetypes this user created or edited, from the v1 archetype table."""
    try:
        from werkbank import repository
    except Exception:
        return []
    try:
        rows = repository.get_archetypes(username)
    except Exception:
        _log.debug("werkbank v2: no user archetypes available", exc_info=True)
        return []
    return [
        AgentSpec(
            id=row.name,
            label=row.name,
            description=row.description or "",
            tools=list(row.enabled_tools or []),
            requires=[],          # a user archetype gets no requirement of its own
            user_defined=True,
            prompt_text=row.soul_text or "",
        )
        for row in rows
    ]


def default_ids() -> set[str]:
    return set(load_defaults().agents)


def is_default(agent_id: str) -> bool:
    return agent_id in default_ids()


def default_spec(agent_id: str) -> AgentSpec | None:
    """What a default archetype looks like as shipped — the way back."""
    return load_defaults().agents.get(agent_id)


def diverges_from_default(spec: AgentSpec) -> bool:
    """True when a default archetype has been edited, so the UI can offer a reset."""
    original = default_spec(spec.id)
    if original is None:
        return False
    return sorted(spec.tools) != sorted(original.tools) or (
        spec.description.strip() != original.description.strip()
    )
