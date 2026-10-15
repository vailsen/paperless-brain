"""An unknown tag/correspondent/type in `search_exact` comes back with the
names that do exist.

The usual miss is language, not spelling: an English question about a German
archive asks for "tax return" where Paperless says "Steuererklärung". No string
similarity bridges that, so the model is shown the real names to translate to.
"""

import asyncio

from services import chat_service
from services.chat_service import MAX_LISTED_NAMES, _existing_names_hint
from werkbank.v2.tools import OUTCOME_FAILED, _hits_from, classify

TAGS = {1: "Steuererklärung", 2: "Versicherung", 3: "Auto", 4: "2025"}


def test_a_small_vocabulary_is_listed_in_full_and_sorted():
    hint = _existing_names_hint("tag", ["tax return"], TAGS)
    assert hint == "Existing tag names: 2025; Auto; Steuererklärung; Versicherung."


def test_a_large_vocabulary_lists_only_related_names():
    many = {i: f"Projekt {i}" for i in range(MAX_LISTED_NAMES + 10)}
    many[9999] = "Steuer 2024"
    hint = _existing_names_hint("tag", ["Steuer"], many)
    assert "Steuer 2024" in hint and "Projekt 1;" not in hint
    assert str(len(many)) in hint


def test_a_large_vocabulary_with_nothing_related_says_so():
    many = {i: f"Projekt {i}" for i in range(MAX_LISTED_NAMES + 10)}
    hint = _existing_names_hint("tag", ["tax return"], many)
    assert "too many to list" in hint


class _FakePaperless:
    async def get_tag_map(self):
        return TAGS

    async def get_correspondent_map(self):
        return {10: "Finanzamt München"}

    async def get_document_types_map(self):
        return {}


def test_search_exact_lists_the_existing_names_on_a_miss(monkeypatch):
    monkeypatch.setattr(chat_service, "_user_paperless", lambda: _FakePaperless())
    text, results = asyncio.run(chat_service._tool_search_exact(
        {"tags": ["tax return"], "correspondent": "tax office"}
    ))
    assert results == []
    assert "Tag 'tax return'" in text and "Correspondent 'tax office'" in text
    assert "Steuererklärung" in text and "Finanzamt München" in text


def test_the_miss_is_a_call_that_did_not_run(monkeypatch):
    """Not "searched, nothing there": no hit count, nothing citable. And the name
    list must never be counted as results by the bullet-line fallback."""
    monkeypatch.setattr(chat_service, "_user_paperless", lambda: _FakePaperless())
    text, _ = asyncio.run(chat_service._tool_search_exact({"tags": ["tax return"]}))
    assert classify(text) == OUTCOME_FAILED
    assert _hits_from("search_exact", text) is None
