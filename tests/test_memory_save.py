"""Saving part of a chat answer as a memory (`app_ui/memory_save.py`)."""

from app_ui.memory_save import memory_text, parse_tags, single_source


def test_text_is_the_selection_else_the_plain_answer():
    answer = "Die Police endet am 31.03.2027 [^241-p3]."
    assert memory_text(answer, "  endet am 31.03.2027 ") == "endet am 31.03.2027"
    plain = memory_text(answer)
    assert "31.03.2027" in plain and "[^" not in plain  # markers become readable references


def test_source_only_when_one_document_is_cited():
    assert single_source("a [^241-p3] b [^241-p3]") == (241, 3)
    assert single_source("a [^241-p3] b [^241-p5]") == (241, None)
    assert single_source("a [^241] b [^7]") == (None, None)
    assert single_source("no citations") == (None, None)


def test_tags():
    assert parse_tags(" #kfz, Versicherung ;  ") == ["kfz", "Versicherung"]
    assert parse_tags("") == []


def test_keywords_are_cleaned_and_counted_across_memories():
    from app_ui.memory_save import clean_keywords, known_keywords

    assert clean_keywords(["#Kfz", " kfz ", "Versicherung; Auto", ""]) == ["Kfz", "Versicherung", "Auto"]
    known = known_keywords([["insurance", "Car"], ["Insurance"], ["insurance", "tax"]])
    assert known[0] == "insurance"  # used three times, spelled so twice
    assert set(known) == {"insurance", "Car", "tax"}


def test_suggestions_put_keywords_from_the_text_first():
    from app_ui.memory_save import suggest_keywords

    known = ["insurance", "tax", "Kfz-Versicherung", "family"]
    got = suggest_keywords(known, "Die Kfz-Versicherung wurde im März gekündigt.", limit=3)
    assert got[0] == "Kfz-Versicherung" and len(got) == 3
    assert suggest_keywords([], "anything") == []
