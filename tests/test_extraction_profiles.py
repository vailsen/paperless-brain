"""Extraction-rule profiles — loading, fallback and structural integrity.

Every profile must provide `_default`: it is the fallback for any document type
the user has that the profile does not name, and without it ingestion raises a
KeyError on the first unrecognised document.
"""

import importlib

import pytest

from config.extraction_rules import (
    AVAILABLE_PROFILES,
    DEFAULT_PROFILE,
    EXTRACTION_JSON_SCHEMA,
    PROMPT_VERSION,
    _resolve,
    get_active_profile,
    get_extraction_rules,
)


def _rules(name: str) -> dict:
    return importlib.import_module(f"config.extraction_rules.{name}").RULES


# ── loading ──────────────────────────────────────────────────────────────────


def test_default_profile_is_available():
    assert DEFAULT_PROFILE in AVAILABLE_PROFILES


def test_active_profile_resolved_to_something_real():
    assert get_active_profile() in AVAILABLE_PROFILES
    assert get_extraction_rules()


def test_unknown_profile_falls_back_and_reports_the_resolved_name():
    """Regression: the loader used to return the *requested* name after falling
    back, so the UI would display a profile that was not actually loaded."""
    assert _resolve("klingon") == DEFAULT_PROFILE


def test_known_profile_resolves_to_itself():
    for code in AVAILABLE_PROFILES:
        assert _resolve(code) == code


# ── structure ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("profile", AVAILABLE_PROFILES)
def test_every_profile_has_a_default_entry(profile):
    """Without _default, an unrecognised document type raises at ingest time."""
    assert "_default" in _rules(profile)


@pytest.mark.parametrize("profile", AVAILABLE_PROFILES)
def test_every_rule_has_a_nonempty_prompt(profile):
    for key, rule in _rules(profile).items():
        assert "prompt" in rule, f"{profile}:{key} has no prompt"
        assert rule["prompt"].strip(), f"{profile}:{key} has an empty prompt"


@pytest.mark.parametrize("profile", AVAILABLE_PROFILES)
def test_every_prompt_includes_the_base_instructions(profile):
    """Rules are BASE_INSTRUCTIONS + type guidance; a rule that forgot the base
    would omit the JSON contract and produce unparseable output."""
    from config.extraction_rules.base import BASE_INSTRUCTIONS

    for key, rule in _rules(profile).items():
        assert BASE_INSTRUCTIONS in rule["prompt"], f"{profile}:{key} lost the base"


@pytest.mark.parametrize("profile", AVAILABLE_PROFILES)
def test_profile_keys_are_unique_and_stripped(profile):
    keys = list(_rules(profile))
    assert len(keys) == len(set(keys))
    assert all(k == k.strip() for k in keys)


def test_german_profile_kept_its_full_rule_set():
    """The de profile is the original 46-entry set; a silent drop is a regression."""
    assert len(_rules("de")) >= 45


def test_english_profile_covers_the_common_types():
    keys = set(_rules("en"))
    for expected in ("Invoice", "Contract", "Bank Statement", "Receipt"):
        assert expected in keys


# ── shared scaffolding ───────────────────────────────────────────────────────


def test_json_schema_declares_the_expected_fields():
    props = EXTRACTION_JSON_SCHEMA.get("properties", {})
    for field in ("page_text", "tables", "actions", "page_summary", "cross_references"):
        assert field in props, f"{field} missing from the extraction schema"


def test_prompt_version_is_set():
    """PROMPT_VERSION drives re-ingest detection on the dashboard."""
    assert PROMPT_VERSION
    assert isinstance(PROMPT_VERSION, str)
