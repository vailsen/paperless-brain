"""Localization (i18n) — translator factory + language registry.

English source strings ARE the gettext msgids, so English needs no catalog:
when no translation is found, gettext returns the msgid (the correct English
text). Other languages (e.g. German) live in locales/<lang>/LC_MESSAGES/.
There is deliberately NO global `_`. Resolve per page/render via
`get_translator()` — a single global translator would leak one user's language
into every concurrent NiceGUI session.
"""

import gettext
from pathlib import Path

from nicegui import app

LOCALES_DIR = Path(__file__).parent / "locales"
DEFAULT_LANG = "en"

# SINGLE SOURCE OF TRUTH for available languages: code -> display name.
# To add a language: add one entry here, then run the pybabel init/translate/
# compile cycle for that code. Nothing else changes.
SUPPORTED_LANGUAGES: dict[str, str] = {
    "en": "English",
    "de": "Deutsch",
}

# Per-language LLM metadata: English language name + date-format hint.
# Single source of truth for the response-language directive appended to
# system prompts (chat + werkbank roles).
LANGUAGE_META: dict[str, dict[str, str]] = {
    "en": {"name": "English", "date_format": "YYYY-MM-DD", "dt_strftime": "%Y-%m-%d %H:%M", "weekdays": "Monday Tuesday Wednesday Thursday Friday Saturday Sunday", "months": "January February March April May June July August September October November December"},
    "de": {"name": "German (Deutsch)", "date_format": "DD.MM.YYYY", "dt_strftime": "%d.%m.%Y %H:%M", "weekdays": "Montag Dienstag Mittwoch Donnerstag Freitag Samstag Sonntag", "months": "Januar Februar März April Mai Juni Juli August September Oktober November Dezember"},
}


def language_name(lang: str) -> str:
    """English name of a language code, for use inside LLM prompts."""
    return LANGUAGE_META.get(lang, {}).get("name", lang)


def language_directive(lang: str) -> str:
    """Response-language sentence appended to system prompts."""
    meta = LANGUAGE_META.get(lang) or LANGUAGE_META[DEFAULT_LANG]
    return (
        f"Always respond in {meta['name']}, regardless of the language of "
        f"documents or tool results. Use the date format {meta['date_format']}."
    )


def weekday_name(dt, lang: str = DEFAULT_LANG) -> str:
    """Weekday name from LANGUAGE_META — independent of the process locale.

    strftime %A depends on locale.setlocale(), which is process-global and
    racy in an async server; explicit names avoid it entirely.
    """
    meta = LANGUAGE_META.get(lang) or LANGUAGE_META[DEFAULT_LANG]
    return meta["weekdays"].split()[dt.weekday()]


def month_name(dt, lang: str = DEFAULT_LANG) -> str:
    """Month name from LANGUAGE_META — independent of the process locale."""
    meta = LANGUAGE_META.get(lang) or LANGUAGE_META[DEFAULT_LANG]
    return meta["months"].split()[dt.month - 1]


def format_datetime(dt, lang: str = DEFAULT_LANG) -> str:
    """Numeric date+time in the language's conventional format."""
    meta = LANGUAGE_META.get(lang) or LANGUAGE_META[DEFAULT_LANG]
    return dt.strftime(meta["dt_strftime"])


def N_(message: str) -> str:
    """No-op marker for deferred translation (module-level constants).

    pybabel extracts N_() calls into the catalog; the actual translation
    happens later at render time via `_(message)`.
    """
    return message


def get_translator():
    """Return the gettext callable for the current user's language.

    MUST be called inside a page function or event handler — it reads
    app.storage.user, which only exists once a client/session is connected.
    """
    lang = app.storage.user.get("language", DEFAULT_LANG)
    if lang == DEFAULT_LANG:
        return gettext.NullTranslations().gettext  # msgid IS English
    try:
        return gettext.translation("messages", LOCALES_DIR, languages=[lang]).gettext
    except FileNotFoundError:
        return gettext.NullTranslations().gettext  # graceful fallback to English
