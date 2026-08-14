"""Three stores, and which of them the model may write to.

The assistant curates its own memory, comments on Paperless documents, and
*only reads* the user's notes. Nothing in the code enforces the last one at the
tool layer — there simply is no write tool — so the model only knows it from
what the tool descriptions and the prompt say. When that wording drifts, the
symptom is a model politely offering "shall I add that to your note?" and then
failing to do it, which reads as a bug to the user.
"""

import pytest

from config.chat_prompts import build_system_prompt
from services.chat_service import TOOL_DEFINITIONS

TOOLS = {t["name"]: t for t in TOOL_DEFINITIONS}

# Tools that write. Anything reaching the user's own notes must NOT be here.
WRITE_TOOLS = {
    "create_note",          # a comment on a Paperless document
    "remember_fact",        # the agent's own memory
    "create_deadline",
    "update_brain_fact",
    "delete_brain_fact",
    "create_kanban_task",
    "trigger_docx_generation",
    "create_email",
    "generate_chat_pdf",
}


def test_there_is_no_tool_that_writes_a_vault_note():
    """The invariant itself: vault_search is the only vault tool, and it reads."""
    vault_tools = {
        name for name in TOOLS
        if "vault" in name or "note" in name
    }
    # create_note is Paperless; vault_search is read-only. Nothing else may appear.
    assert vault_tools == {"vault_search", "create_note"}


def test_vault_search_states_it_cannot_write_and_forbids_offering_to():
    desc = TOOLS["vault_search"]["description"].lower()
    assert "read-only" in desc
    assert "no tool to create" in desc or "there is no tool" in desc
    assert "never offer" in desc


def test_create_note_says_it_targets_a_paperless_document_not_a_vault_note():
    desc = TOOLS["create_note"]["description"].lower()
    assert "paperless document" in desc
    assert "vault" in desc          # names the thing it is NOT
    assert "document_id" in desc


@pytest.mark.parametrize("name", ["remember_fact", "update_brain_fact", "delete_brain_fact"])
def test_memory_tools_say_the_memory_is_the_agents_own(name):
    desc = TOOLS[name]["description"].lower()
    assert "own" in desc and "memory" in desc


@pytest.mark.parametrize("name", ["update_brain_fact", "delete_brain_fact"])
def test_fact_ids_come_from_search_memory_not_from_vault_search(name):
    """A pbrain_id from a vault note must not be fed to a memory tool."""
    assert "search_memory" in TOOLS[name]["description"]
    assert "vault_search" in TOOLS[name]["description"]


def test_prompt_tells_the_model_not_to_offer_note_edits():
    prompt = build_system_prompt({"vault"})
    assert "READ-ONLY" in prompt
    assert "shall I add that to your note?" in prompt


def test_prompt_separates_the_three_stores():
    prompt = build_system_prompt({"vault", "memory", "documents", "document_notes"})
    for marker in ("vault_search", "remember_fact", "create_note"):
        assert marker in prompt
    # The memory block must claim the memory as the agent's own.
    assert "This memory is YOURS" in prompt


def test_vault_capability_line_does_not_promise_writing():
    line = build_system_prompt({"vault"})
    assert "no writing" in line
