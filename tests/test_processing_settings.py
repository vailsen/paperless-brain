"""Extraction profile and archive language resolve from the UI store, not .env.

Both used to be read straight off config.settings, which meant they were
env-only, invisible in the app, and defaulted to "en". Dropping a line from
.env silently switched a German archive to the generic English rule set and
started writing English summaries, with nothing in the UI to reveal it.

They now follow the same precedence as every other ingestion setting: the
stored UI value wins, .env is the fallback.
"""

import sqlite3

import pytest

from config.extraction_rules import (
    AVAILABLE_PROFILES,
    DEFAULT_PROFILE,
    get_active_profile,
    get_extraction_rules,
)
from config.settings import settings
from werkbank import settings_store as ws


@pytest.fixture(autouse=True)
def store_db(tmp_path, monkeypatch):
    """Point the settings store at a throwaway DB.

    settings_store binds _DB_PATH by value at import, so patching it on that
    module is what takes effect — patching werkbank.repository would not.
    """
    db = tmp_path / "papersage.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE werkbank_settings ("
            " key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)"
        )
    monkeypatch.setattr(ws, "_DB_PATH", db)
    yield db


@pytest.fixture
def env_profile(monkeypatch):
    def _set(profile: str = "", language: str = ""):
        monkeypatch.setattr(settings, "extraction_profile", profile, raising=False)
        monkeypatch.setattr(settings, "archive_language", language, raising=False)
    return _set


# ── precedence ────────────────────────────────────────────────────────────────


def test_env_is_used_when_the_store_is_empty(env_profile):
    env_profile(profile="de", language="de")
    assert ws.get_extraction_profile() == "de"
    assert ws.get_archive_language() == "de"


def test_stored_value_beats_env(env_profile):
    env_profile(profile="en", language="en")
    ws.set_value(ws.EXTRACTION_PROFILE, "de")
    ws.set_value(ws.ARCHIVE_LANGUAGE, "de")
    assert ws.get_extraction_profile() == "de"
    assert ws.get_archive_language() == "de"


def test_the_setting_survives_env_being_wiped(env_profile):
    """The actual regression: .env was rewritten and both keys vanished."""
    ws.set_value(ws.EXTRACTION_PROFILE, "de")
    ws.set_value(ws.ARCHIVE_LANGUAGE, "de")
    env_profile(profile="", language="")
    assert ws.get_extraction_profile() == "de"
    assert ws.get_archive_language() == "de"


def test_archive_language_falls_back_to_english_when_nothing_is_set(env_profile):
    env_profile(profile="", language="")
    assert ws.get_archive_language() == "en"


def test_values_are_normalised(env_profile):
    env_profile()
    ws.set_value(ws.EXTRACTION_PROFILE, "  DE  ")
    assert ws.get_extraction_profile() == "de"


# ── the rules that come out ───────────────────────────────────────────────────


def test_rules_follow_the_stored_profile(env_profile):
    env_profile(profile="en", language="en")
    ws.set_value(ws.EXTRACTION_PROFILE, "de")
    assert get_active_profile() == "de"
    assert get_extraction_rules() is _rules_of("de")


def test_rules_change_without_a_restart(env_profile):
    """They used to be bound at import, so a UI change did nothing until restart."""
    env_profile()
    ws.set_value(ws.EXTRACTION_PROFILE, "de")
    assert get_active_profile() == "de"
    ws.set_value(ws.EXTRACTION_PROFILE, "en")
    assert get_active_profile() == "en"
    assert get_extraction_rules() is _rules_of("en")


def test_unknown_stored_profile_reports_the_fallback_not_the_request(env_profile):
    """get_active_profile must show what is loaded — the two differ exactly when
    something is misconfigured, which is when the UI needs to be honest."""
    env_profile()
    ws.set_value(ws.EXTRACTION_PROFILE, "klingon")
    assert get_active_profile() == DEFAULT_PROFILE
    assert get_extraction_rules() is _rules_of(DEFAULT_PROFILE)


def test_every_profile_can_actually_be_selected(env_profile):
    env_profile()
    for code in AVAILABLE_PROFILES:
        ws.set_value(ws.EXTRACTION_PROFILE, code)
        assert get_active_profile() == code


def _rules_of(name: str) -> dict:
    import importlib

    return importlib.import_module(f"config.extraction_rules.{name}").RULES
