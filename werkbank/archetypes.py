"""werkbank/archetypes.py — default archetypes, seeding, and resolution helpers.

Tool names must always match the keys in services.chat_service.TOOL_DEFINITIONS.
Tools that require a browser dialog (trigger_docx_generation, create_email,
generate_chat_pdf) are intentionally absent from all worker subsets.
"""

from __future__ import annotations

from dataclasses import dataclass

from werkbank import repository

# ── Worker tool subsets ───────────────────────────────────────────────────────
# Each list is validated at seeding time against the real TOOL_DEFINITIONS.

_RETRIEVER_TOOLS = [
    "search",
    "search_exact",
    "get_document_details",
    "get_document_table",
    "get_document_page_text",
    "get_actions",
    "search_memory",
    "vault_search",
    "calculate",
    "get_current_date",
]

_RESEARCHER_TOOLS = [
    "web_search",
    "web_fetch_page",
    "search_memory",
    "vault_search",
    "calculate",
    "get_current_date",
]

_SECRETARY_TOOLS = [
    "search_emails",
    "search_calendar",
    "search_memory",
    "calculate",
    "get_current_date",
]

_WRITER_TOOLS = [
    "calculate",
    "get_current_date",
]


@dataclass
class _DefaultSpec:
    name: str
    description: str
    soul_text: str
    enabled_tools: list[str]


_DEFAULTS: list[_DefaultSpec] = [
    _DefaultSpec(
        name="retriever",
        description="Searches the document archive and memory.",
        soul_text=(
            "You are a precise document researcher. Your job is to find relevant "
            "documents, facts and deadlines in the personal archive "
            "and summarize them in a structured way.\n\n"
            "Rules:\n"
            "- Always search memory first (search_memory), then the document archive (search).\n"
            "- For exact identifiers (invoice numbers, IBANs, license plates) use search_exact.\n"
            "- Read documents in this order: get_document_details → get_document_page_text "
            "for specific pages; get_document_table for tables (e.g. invoice line items).\n"
            "- Use vault_search for the user's personal notes.\n"
            "- Cite the document ID as #ID for EVERY statement — results without "
            "a document reference are rejected. If nothing was found, write "
            "explicitly 'not found'.\n"
            "- If a search does not return sufficient results, rephrase the query "
            "and search again (max. 3 attempts).\n"
            "- Answer exclusively based on the documents found — "
            "no assumptions from general knowledge."
        ),
        enabled_tools=_RETRIEVER_TOOLS,
    ),
    _DefaultSpec(
        name="researcher",
        description="Researches current information on the web.",
        soul_text=(
            "You are a thorough web researcher. Your job is to obtain current "
            "information from the web and summarize it in a structured way.\n\n"
            "Rules:\n"
            "- Start with web_search, then read relevant pages with web_fetch_page.\n"
            "- Use vault_search for the user's personal notes.\n"
            "- Back every statement with its source (URL).\n"
            "- Distinguish clearly between confirmed information and estimates."
        ),
        enabled_tools=_RESEARCHER_TOOLS,
    ),
    _DefaultSpec(
        name="secretary",
        description="Searches the user's emails and calendar.",
        soul_text=(
            "You are a careful assistant for personal correspondence. Your "
            "job is to search the user's emails and calendar entries "
            "and summarize the relevant findings in a structured way.\n\n"
            "Rules:\n"
            "- Use search_emails for correspondence, orders and confirmations. "
            "For persons use only the last name in the sender field.\n"
            "- Use search_calendar for appointments and deadlines; for time-range "
            "questions set date_from/date_to instead of month names in the query.\n"
            "- Back every statement with its source (sender + date, or event title).\n"
            "- If nothing was found, write explicitly 'not found'."
        ),
        enabled_tools=_SECRETARY_TOOLS,
    ),
    _DefaultSpec(
        name="writer",
        description="Processes and structures collected results into a readable report.",
        soul_text=(
            "You are a precise editor. You receive fully researched information "
            "from previous work steps as context. Your job is to combine this data "
            "into a clear, well-structured and readable document.\n\n"
            "Rules:\n"
            "- Use exclusively the information from the context — no research of your own.\n"
            "- Structure with Markdown: headings, tables, lists where useful.\n"
            "- Summarize concisely without dropping important facts.\n"
            "- Mark missing or uncertain data explicitly."
        ),
        enabled_tools=_WRITER_TOOLS,
    ),
]


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

# Toolsets that earlier code versions seeded for default archetypes. A row whose
# tools set-equal one of these was never customized by the user, so it is safe
# to resync it fully (tools + soul + description) to the current default.
_SUPERSEDED_TOOLSETS: dict[str, list[frozenset[str]]] = {
    "retriever": [
        frozenset({"search", "search_exact", "get_document_details",
                   "get_document_page_text", "get_actions", "search_memory",
                   "calculate", "get_current_date"}),
    ],
    "researcher": [
        # pre-split, without vault_search
        frozenset({"web_search", "web_fetch_page", "search_emails",
                   "search_calendar", "search_memory", "calculate",
                   "get_current_date"}),
        # pre-split, with vault_search
        frozenset({"web_search", "web_fetch_page", "search_emails",
                   "search_calendar", "search_memory", "vault_search",
                   "calculate", "get_current_date"}),
    ],
}


def seed_defaults_if_needed(user_id: str) -> None:
    """Insert missing default archetypes; keep existing defaults in sync.

    Existing default-named archetypes:
    - tools set-equal a superseded default toolset → full resync to the current
      default (tools, soul_text, description) — the row was never customized.
    - otherwise: additive tool merge only (new default tools appended,
      user-added tools stay, nothing removed).
    """
    existing = repository.get_archetypes(user_id)
    existing_by_name = {a.name: a for a in existing}

    for spec in _DEFAULTS:
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

        if frozenset(arch.enabled_tools) in _SUPERSEDED_TOOLSETS.get(spec.name, []):
            print(f"[werkbank/archetypes] '{spec.name}': resyncing superseded default")
            repository.update_archetype(
                arch.id, user_id,
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
