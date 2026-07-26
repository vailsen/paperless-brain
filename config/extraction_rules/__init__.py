# config/extraction_rules/__init__.py
"""Vision-model extraction rules, selectable per profile.

A profile is a set of per-document-type prompts keyed by the `document_type`
name in Paperless-ngx. Because those names are whatever the user called them,
profiles are language- and jurisdiction-specific:

    en  — common international types (default)
    de  — German legal/administrative domain, ~46 types

Select with EXTRACTION_PROFILE in .env. An unknown profile name falls back to
`en` with a warning rather than failing at import time — extraction still works,
since every profile provides a `_default` entry.

Adding a profile: drop `<code>.py` next to this file exporting a `RULES` dict,
then set EXTRACTION_PROFILE=<code>. Nothing else changes.
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
from config.settings import settings

log = logging.getLogger(__name__)

AVAILABLE_PROFILES = ("en", "de")
DEFAULT_PROFILE = "en"


def _load_profile(name: str) -> tuple[str, dict[str, dict]]:
    """Return the profile that was actually loaded, plus its rules.

    Returns the resolved name, not the requested one — on fallback the two
    differ, and callers that display the active profile must show the truth.
    """
    if name not in AVAILABLE_PROFILES:
        log.warning(
            "Unknown EXTRACTION_PROFILE %r — falling back to %r. Available: %s",
            name, DEFAULT_PROFILE, ", ".join(AVAILABLE_PROFILES),
        )
        name = DEFAULT_PROFILE
    return name, importlib.import_module(f"config.extraction_rules.{name}").RULES


EXTRACTION_PROFILE, EXTRACTION_RULES = _load_profile(
    (settings.extraction_profile or DEFAULT_PROFILE).lower()
)

__all__ = [
    "BASE_INSTRUCTIONS",
    "CONDENSED_SUMMARY_PROMPT",
    "EXTRACTION_JSON_SCHEMA",
    "EXTRACTION_PROFILE",
    "EXTRACTION_RULES",
    "PROMPT_VERSION",
    "TABLE_CONTINUATION_CONTEXT",
]
