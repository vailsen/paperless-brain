"""The archive's language reaches every prompt that searches the archive.

The answer language follows the user; the search language has to follow the
archive, because tag, correspondent and document-type names and `search_exact`
text are matched literally. Without the archive language named in the prompt,
an English question about German documents is searched with English words.
"""

from config.chat_prompts import build_system_prompt
from i18n import archive_search_directive
from werkbank.v2 import runner
from werkbank.v2.registry import AgentSpec


def test_the_directive_names_the_archive_language():
    text = archive_search_directive("de")
    assert "German (Deutsch)" in text
    assert "search queries" in text


def test_chat_prompt_carries_it_in_the_document_block():
    prompt = build_system_prompt({"documents"}, archive_language="de")
    assert archive_search_directive("de") in prompt
    # Inside the document-search rules, not floating after the language rule.
    assert prompt.index(archive_search_directive("de")) < prompt.index("Respond in the language")


def test_chat_prompt_omits_it_without_document_search():
    assert "archive is in" not in build_system_prompt({"web"}, archive_language="de")
    assert "archive is in" not in build_system_prompt({"documents"})


def test_an_archive_searching_agent_gets_it_even_with_an_edited_prompt(monkeypatch):
    monkeypatch.setattr("werkbank.settings_store.get_archive_language", lambda: "de")
    spec = AgentSpec(id="doc", label="Doc", tools=["search", "calculate"],
                     prompt_text="My own prompt.")
    assert archive_search_directive("de") in runner.archive_language_block(spec)


def test_an_agent_without_archive_search_does_not(monkeypatch):
    monkeypatch.setattr("werkbank.settings_store.get_archive_language", lambda: "de")
    spec = AgentSpec(id="web", label="Web", tools=["web_search", "web_fetch_page"])
    assert runner.archive_language_block(spec) == ""
