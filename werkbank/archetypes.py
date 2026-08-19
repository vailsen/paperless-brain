"""werkbank/archetypes.py — the shipped archetypes, seeding, and resolution.

**`config/agents.yaml` is the only source of default archetypes.** The four v1
archetypes (`retriever`, `researcher`, `secretary`, `writer`) are gone: they were
written for the v1 worker, which answered in prose. v2 agents answer in facts
with sources, and their prompts say so — a v1 archetype in a v2 run gets a
generic fallback prompt with none of the evidence rules, which is precisely the
failure the rebuild exists to remove.

An unmodified v1 row is therefore deleted on the first seed after the upgrade. A
row the user edited is *kept* and becomes an ordinary user archetype — it is
their work, and deleting it to tidy up would be the wrong trade.

Users still create and edit archetypes; what a default now means is "shipped in
the yaml, and restorable from it at any time". The prompt of a shipped agent
lives in a markdown file next to the code; an edited row overrides it, the same
override-and-reset shape `werkbank/v2/prompts.py` uses for the pipeline roles.

Tool names must always match the keys in services.chat_service.TOOL_DEFINITIONS.
Tools that require a browser dialog (trigger_docx_generation, create_email,
generate_chat_pdf) are intentionally absent from all worker subsets.
"""

from __future__ import annotations

from dataclasses import dataclass

from werkbank import repository

# Rows seeded by v1. Deleted on first seed when still untouched — recognised by
# the opening sentence of the prompt v1 shipped, since a user who rewrote the
# prompt no longer matches and keeps their archetype.
_V1_SEEDS: dict[str, str] = {
    "retriever": "You are a precise document researcher.",
    "researcher": "You are a thorough web researcher.",
    "secretary": "You are a careful assistant for personal correspondence.",
    "writer": "You are a precise editor.",
}


@dataclass
class _DefaultSpec:
    name: str
    description: str
    soul_text: str
    enabled_tools: list[str]


def _defaults() -> list[_DefaultSpec]:
    """The shipped archetypes, read from `config/agents.yaml`.

    Not a module constant: the yaml is editable without a deploy, and a cached
    list would keep serving the version that happened to be on disk at import.
    """
    from werkbank.v2 import registry

    specs: list[_DefaultSpec] = []
    for agent_id, spec in registry.load_defaults().agents.items():
        path = spec.prompt_path()
        specs.append(_DefaultSpec(
            name=agent_id,
            description=spec.description.strip(),
            soul_text=path.read_text(encoding="utf-8") if path.is_file() else "",
            enabled_tools=list(spec.tools),
        ))
    return specs


# ── Validation ────────────────────────────────────────────────────────────────

def _known_tool_names() -> frozenset[str]:
    from services.chat_service import TOOL_DEFINITIONS
    return frozenset(t["name"] for t in TOOL_DEFINITIONS)


def validate_tool_subset(tools: list[str]) -> list[str]:
    """Return only tool names that exist in TOOL_DEFINITIONS. Logs unknowns."""
    known = _known_tool_names()
    valid, invalid = [], []
    for t in tools:
        (valid if t in known else invalid).append(t)
    if invalid:
        print(f"[werkbank/archetypes] Unknown tools stripped: {invalid}")
    return valid


# ── Seeding ───────────────────────────────────────────────────────────────────


def _drop_untouched_v1_seeds(existing: list) -> list:
    """Remove the v1 archetypes the app itself seeded, keep the ones edited.

    A v1 archetype in a v2 run is worse than no archetype: it has no prompt file,
    so the runner falls back to a generic instruction without the evidence rules,
    and the planner is offered an agent that looks equivalent to a tuned one.
    A row the user rewrote is a different matter — that is their archetype, and
    it survives as a user-defined one.
    """
    kept = []
    for arch in existing:
        opener = _V1_SEEDS.get(arch.name)
        if opener and (arch.soul_text or "").lstrip().startswith(opener):
            print(f"[werkbank/archetypes] removing untouched v1 archetype '{arch.name}'")
            repository.delete_archetype(arch.id, arch.user_id)
            continue
        kept.append(arch)
    return kept


def seed_defaults_if_needed(user_id: str) -> None:
    """Insert missing shipped archetypes; keep existing ones in sync.

    A shipped archetype that already exists is only extended, never overwritten:
    new default tools are appended, the user's own tools and prompt stay. Putting
    a row *back* to the shipped state is `restore_default`, and that is the
    user's decision, not a side effect of opening the page.
    """
    existing = _drop_untouched_v1_seeds(repository.get_archetypes(user_id))
    existing_by_name = {a.name: a for a in existing}

    for spec in _defaults():
        arch = existing_by_name.get(spec.name)
        valid_tools = validate_tool_subset(spec.enabled_tools)

        if arch is None:
            repository.create_archetype(
                user_id=user_id,
                name=spec.name,
                description=spec.description,
                soul_text=spec.soul_text,
                enabled_tools=valid_tools,
            )
            continue

        missing = [t for t in valid_tools if t not in arch.enabled_tools]
        if missing:
            print(f"[werkbank/archetypes] '{spec.name}': adding new default tools {missing}")
            repository.update_archetype(
                arch.id, user_id, enabled_tools=arch.enabled_tools + missing
            )


# ── Resolution ────────────────────────────────────────────────────────────────

def resolve_archetype(archetype_id: int, user_id: str) -> tuple[str, list[str]] | None:
    """Return (soul_text, validated_tool_subset) or None if not found."""
    arch = repository.get_archetype(archetype_id, user_id)
    if arch is None:
        return None
    valid_tools = validate_tool_subset(arch.enabled_tools)
    return arch.soul_text, valid_tools


def resolve_archetype_by_name(name: str, user_id: str) -> tuple[str, list[str]] | None:
    """Return (soul_text, validated_tool_subset) or None if not found."""
    arch = repository.get_archetype_by_name(name, user_id)
    if arch is None:
        return None
    valid_tools = validate_tool_subset(arch.enabled_tools)
    return arch.soul_text, valid_tools


def list_archetype_summaries(user_id: str) -> list[dict]:
    """Return [{id, name, description}] — what the Splitter receives."""
    seed_defaults_if_needed(user_id)
    return [
        {"id": a.id, "name": a.name, "description": a.description}
        for a in repository.get_archetypes(user_id)
    ]


# ── Restoring a default ───────────────────────────────────────────────────────
#
# v1 had no way back: edit a default archetype's prompt into something broken
# and the original was gone, because the defaults only ever seeded rows that did
# not exist yet. These two functions are that way back, and they are what the
# archetype dialog offers.


def default_names() -> set[str]:
    """Archetypes that ship with the app, and can therefore be restored."""
    return {spec.name for spec in _defaults()}


def default_for(name: str) -> _DefaultSpec | None:
    return next((s for s in _defaults() if s.name == name), None)


def differs_from_default(archetype) -> bool:
    """True when a shipped archetype has been edited — so the UI can offer a reset."""
    spec = default_for(archetype.name)
    if spec is None:
        return False
    return (
        (archetype.soul_text or "").strip() != spec.soul_text.strip()
        or (archetype.description or "").strip() != spec.description.strip()
        or sorted(archetype.enabled_tools or []) != sorted(validate_tool_subset(spec.enabled_tools))
    )


def restore_default(name: str, user_id: str) -> bool:
    """Put one shipped archetype back the way it came. False if it is not one.

    Recreates the row when it was deleted rather than only updating: "restore"
    has to work after the mistake people actually make.
    """
    spec = default_for(name)
    if spec is None:
        return False
    tools = validate_tool_subset(spec.enabled_tools)
    existing = repository.get_archetype_by_name(name, user_id)
    if existing is None:
        repository.create_archetype(
            user_id=user_id, name=spec.name, description=spec.description,
            soul_text=spec.soul_text, enabled_tools=tools,
        )
        return True
    repository.update_archetype(
        existing.id, user_id,
        description=spec.description, soul_text=spec.soul_text, enabled_tools=tools,
    )
    return True


def restore_all_defaults(user_id: str) -> int:
    """Restore every shipped archetype. User-created ones are left untouched."""
    return sum(1 for spec in _defaults() if restore_default(spec.name, user_id))
