"""Localization (i18n) — translator factory + language registry.

English source strings ARE the gettext msgids, so English needs no catalog:
when no translation is found, gettext returns the msgid (the correct English
text). Other languages (e.g. German) live in locales/<lang>/LC_MESSAGES/.
There is deliberately NO global `_`. Resolve per page/render via
`get_translator()` — a single global translator would leak one user's language
into every concurrent NiceGUI session.
"""

import gettext
import re
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
    """Response-language sentence appended to system prompts.

    Two different things used to be conflated under "always respond in X".

    The part worth keeping is that a **document** must not change the reply
    language: a German invoice answered in German inside an English UI is a bug,
    and the same goes for a tool result or a web page.

    The part that was wrong is that the **user's own** language was overridden
    too. Writing German to an English UI is the normal case here — the interface
    language is a preference about the interface, not a declaration of what the
    user speaks. Obedient models (Nemotron, Muse Glimmer) followed the old
    wording literally and answered German questions in English; Qwen ignored it
    and did the sensible thing. The instruction was at fault, not the models.

    So: the user's language wins, the UI language is the fallback when their
    message does not settle it (a one-word reply, a bare document number), and
    documents still never get a vote.

    "Do not switch language" turned out to be only half an instruction. Asked in
    English about a German document, a model that has just read a German summary
    reports it in German — not because it decided to switch, but because
    repeating what it read is the path of least resistance and nothing told it
    otherwise. The missing half is the explicit order to TRANSLATE the content,
    with proper names and quotations exempted.
    """
    meta = LANGUAGE_META.get(lang) or LANGUAGE_META[DEFAULT_LANG]
    return (
        f"Respond in the language the user writes to you in — their message alone "
        f"decides the answer language. When it does not make that clear, respond "
        f"in {meta['name']}. Never switch language because a document, tool result "
        f"or web page is in another language: TRANSLATE what you take from them "
        f"into the answer language instead of repeating it in the source language. "
        f"This applies to document summaries and extracted fields as well — an "
        f"English question about a German document gets an English answer. Only "
        f"proper names, document titles and literal quotations stay in the "
        f"original. Use the date format {meta['date_format']}."
    )


# Words that occur constantly in one language and rarely in the other. Kept
# deliberately small and unambiguous: "was" is a German question word and an
# English verb, "in" and "man" exist in both, so none of those are listed.
_LANG_MARKERS: dict[str, frozenset[str]] = {
    "de": frozenset("""
        der die das den dem des und nicht mit für von ich mein meine meinen meiner
        ist sind wie wann wo bitte kannst hast habe wurde werden noch nur schon
        oder aber ein eine einen zum zur auf aus bei nach über unter welche welcher
        welches gibt gibt's zeig zeige mir dir sich wir ihr euer unsere
    """.split()),
    "en": frozenset("""
        the and is are what when where how my mine have has with for from please
        can not but or also only this that these those of to do does did you your
        show give list about there their they it's i'm was were be been
    """.split()),
}


def detect_language(text: str, default: str = DEFAULT_LANG) -> str:
    """Best guess at the language of a user message, `default` when unsure.

    Deliberately a word-count heuristic and not a dependency: the only job is
    telling German from English well enough to name the answer language in the
    prompt, and the caller always has a sane fallback (the UI language). A
    wrong guess on a two-word message is harmless; a new runtime dependency for
    it would not be.
    """
    if not text:
        return default
    lowered = text.lower()
    words = set(re.findall(r"[a-zà-ÿäöüß']+", lowered))
    scores = {code: len(words & markers) for code, markers in _LANG_MARKERS.items()}
    # Umlauts and ß are decisive on their own — no English word carries them.
    if re.search(r"[äöüß]", lowered):
        scores["de"] = scores.get("de", 0) + 2
    best = max(scores, key=lambda c: scores[c])
    runner_up = max((c for c in scores if c != best), key=lambda c: scores[c], default=None)
    if scores[best] < 2 or (runner_up and scores[best] == scores[runner_up]):
        return default   # too short or genuinely ambiguous
    return best


def answer_language_reminder(lang: str) -> str:
    """One line re-asserting the answer language, injected after tool results.

    The system prompt already carries the rule, but tool results land between
    it and the model's answer — and a long German document summary sitting
    right before generation beats a sentence thousands of tokens earlier. This
    is the same recency fix the tool-use guard uses, kept short because it is
    paid on every tool round trip.
    """
    meta = LANGUAGE_META.get(lang) or LANGUAGE_META[DEFAULT_LANG]
    return (
        f"Language check before you answer: write the answer in {meta['name']}. "
        f"The tool results above may be in another language — translate what you "
        f"use from them. Proper names, document titles and literal quotations "
        f"keep their original wording."
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
