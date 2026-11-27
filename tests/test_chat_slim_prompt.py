"""The chat's fixed part — system prompt + tool schemas — is read on every
model call, so it says each rule once and names only what exists.

Letters and email drafts became buttons (2026-11-15); the archive map in the
prompt lost the lines that only repeated others or changed with every new
document; today's date sits last so the rest stays a cached prefix.
"""

import re
from datetime import date

from config.chat_prompts import build_system_prompt, today_line
from services import briefings
from services.chat_service import TOOL_DEFINITIONS

TOOLS = {t["name"]: t for t in TOOL_DEFINITIONS}
PARAMS = {p for t in TOOL_DEFINITIONS for p in t["input_schema"].get("properties", {})}
ALL_GROUPS = {"documents", "visual", "calculate", "email", "calendar", "web", "vault",
              "document_notes", "memory"}


def test_letter_and_email_are_no_longer_tools():
    assert "trigger_docx_generation" not in TOOLS
    assert "create_email" not in TOOLS


def test_prompt_points_to_the_menu_for_letters_and_emails():
    prompt = build_system_prompt(ALL_GROUPS)
    assert '"Write a letter"' in prompt and '"Write an email"' in prompt


def test_every_identifier_the_prompt_names_exists():
    """A renamed tool or parameter must not linger in the prompt — the model
    would call something that is not there."""
    prompt = build_system_prompt(ALL_GROUPS)
    named = set(re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", prompt))
    unknown = named - TOOLS.keys() - PARAMS - {"tag_match", "pbrain_id"}
    assert not unknown, unknown


def test_no_tool_description_names_a_missing_tool():
    for tool in TOOL_DEFINITIONS:
        for word in re.findall(r"\b[a-z]+(?:_[a-z]+)+\b", tool["description"]):
            if word.startswith(("search", "get_", "view_", "show_", "create_", "remember_",
                                "update_", "delete_", "vault_", "web_", "find_")):
                assert word in TOOLS, (tool["name"], word)


def test_show_document_image_does_not_point_to_a_tool_that_is_usually_off():
    # view_document_page is in the "visual" group, off by default.
    assert "view_document_page" not in TOOLS["show_document_image"]["description"]


def test_date_is_in_the_prompt_and_last():
    prompt = build_system_prompt(ALL_GROUPS, "alice", "en", today=date(2026, 11, 15))
    assert prompt.rstrip().endswith(today_line(date(2026, 11, 15)))
    assert "Sunday, 2026-11-15" in prompt
    # No get_current_date round trip for "newest" or "next week" any more.
    assert "get_current_date" not in prompt


def test_rules_are_not_repeated_from_the_tool_descriptions():
    prompt = build_system_prompt(ALL_GROUPS)
    assert "Your capabilities" not in prompt
    assert "image_index" not in prompt          # show_document_image's own description
    assert "time_range" not in prompt           # web_search's own description
    assert prompt.count("search the archive") + prompt.count("Search the archive") == 1


def test_search_memory_description_agrees_with_the_prompt():
    """It used to say "call this FIRST" while the prompt said "only when asked"."""
    desc = TOOLS["search_memory"]["description"]
    assert "FIRST" not in desc
    assert "'search' already" in desc


def test_fixed_part_stays_small():
    """Guard against creep: prompt + schemas of a full chat turn."""
    prompt = build_system_prompt(ALL_GROUPS, "alice", "de", archive_language="de")
    schemas = sum(len(str(t)) for t in TOOL_DEFINITIONS)
    assert len(prompt) < 8_000
    assert schemas < 19_000


# ── Archive map in the prompt ────────────────────────────────────────────────

MAP = {
    "total": 513, "first": "1973-02-07", "last": "2026-10-29",
    "correspondents": [["Finanzamt", 80]], "document_types": [["Rechnung", 169]],
    "tags": [["Steuererklärung", 206]], "years": [],
    "series": [{"correspondent": "Amazon", "type": "Rechnung", "count": 48, "since": "2016-04-19"}],
    "newest": [{"id": 734, "title": "Skate", "date": "2026-10-29"}],
}


def test_prompt_map_keeps_names_and_drops_series_and_newest():
    block = briefings.map_only_block(MAP)
    assert "Finanzamt 80" in block and "Rechnung 169" in block and "Steuererklärung 206" in block
    assert "Recurring" not in block
    assert "Newest" not in block and "#734" not in block
    # Nothing was studied for it: the header says counted by code, and keeps the warning.
    assert block.startswith(briefings.MAP_HEADER) and "studying" not in block
    assert "MAP, not evidence" in block


def test_full_map_still_has_them_for_the_briefing_tab():
    md = briefings.map_markdown(MAP)
    assert "Recurring" in md and "Newest" in md


def test_studied_briefing_prompt_uses_the_compact_map_and_keeps_the_orientation():
    b = briefings.Briefing(expert_id="tax", map=MAP, body=(
        "<!-- pb:map -->\n## Archive map\n\n" + briefings.map_markdown(MAP)
        + "\n\n<!-- pb:orientation -->\n## Orientation\n\n- Tax returns are filed yearly (#646)\n"))
    block = briefings.prompt_block(b)
    assert "Newest" not in block and "Recurring" not in block
    assert "Tax returns are filed yearly" in block
    assert "Finanzamt 80" in block
