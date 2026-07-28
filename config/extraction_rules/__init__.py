# config/extraction_rules/__init__.py
"""Vision-model extraction rules, selectable per profile.

A profile is a set of per-document-type prompts keyed by the `document_type`
name in Paperless-ngx. Because those names are whatever the user called them,
profiles are language- and jurisdiction-specific:

    en  — common international types (default)
    de  — German legal/administrative domain, ~46 types

Choose it in Settings > Processing; EXTRACTION_PROFILE in .env is the fallback.
An unknown profile name falls back to `en` with a warning rather than failing —
extraction still works, since every profile provides a `_default` entry.

Adding a profile: drop `<code>.py` next to this file exporting a `RULES` dict
and add the code to AVAILABLE_PROFILES. Nothing else changes.

Resolution happens per call, not at import: the profile is now settable in the
UI, and a value frozen at import time would keep the old rules until restart.
"""

import importlib
import logging

from config.extraction_rules.base import (
    BASE_INSTRUCTIONS,
    CONDENSED_SUMMARY_PROMPT,
    EXTRACTION_JSON_SCHEMA,
    PROMPT_VERSION,
    TABLE_CONTINUATION_CONTEXT,
)

log = logging.getLogger(__name__)

AVAILABLE_PROFILES = ("en", "de")
DEFAULT_PROFILE = "en"

_warned: set[str] = set()


def _resolve(name: str) -> str:
    """Map a requested profile name to one that exists, warning once."""
    if name in AVAILABLE_PROFILES:
        return name
    if name not in _warned:
        _warned.add(name)
        log.warning(
            "Unknown extraction profile %r — falling back to %r. Available: %s",
            name, DEFAULT_PROFILE, ", ".join(AVAILABLE_PROFILES),
        )
    return DEFAULT_PROFILE


def get_active_profile() -> str:
    """The profile actually in use — resolved, so a fallback shows as the truth.

    Callers that display this must not show the requested value instead: the two
    differ exactly when something is misconfigured, which is when it matters.
    """
    from werkbank.settings_store import get_extraction_profile

    try:
        requested = get_extraction_profile()
    except Exception:
        requested = ""
    return _resolve(requested or DEFAULT_PROFILE)


def get_extraction_rules() -> dict[str, dict]:
    """Rules for the active profile. Cheap to call — modules are import-cached."""
    return importlib.import_module(
        f"config.extraction_rules.{get_active_profile()}"
    ).RULES


__all__ = [
    "AVAILABLE_PROFILES",
    "BASE_INSTRUCTIONS",
    "CONDENSED_SUMMARY_PROMPT",
    "DEFAULT_PROFILE",
    "EXTRACTION_JSON_SCHEMA",
    "PROMPT_VERSION",
    "TABLE_CONTINUATION_CONTEXT",
    "get_active_profile",
    "get_extraction_rules",
]
