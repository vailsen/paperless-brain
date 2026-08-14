"""i18n helpers and translation-catalog integrity.

The catalog tests encode the Phase 2 acceptance criteria as executable checks:
no empty msgstr, no unreviewed fuzzy entries, and matching {placeholders}
between msgid and msgstr. An empty msgstr silently renders English in the
German UI, and a dropped placeholder raises KeyError at render time — both are
invisible until a user hits that exact string.
"""

import re
from datetime import datetime
from pathlib import Path

import pytest
from babel.messages.mofile import read_mo
from babel.messages.pofile import read_po

import i18n

PO_FILES = sorted(Path("locales").glob("*/LC_MESSAGES/messages.po"))
_PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")


def _catalog(path: Path):
    with path.open("rb") as fh:
        return read_po(fh)


# ── helpers ──────────────────────────────────────────────────────────────────


def test_supported_languages_have_metadata():
    """Every selectable language needs LANGUAGE_META or prompts break."""
    assert set(i18n.SUPPORTED_LANGUAGES) <= set(i18n.LANGUAGE_META)


@pytest.mark.parametrize("lang", ["en", "de"])
def test_weekday_and_month_names_cover_the_full_range(lang):
    meta = i18n.LANGUAGE_META[lang]
    assert len(meta["weekdays"].split()) == 7
    assert len(meta["months"].split()) == 12


def test_weekday_name_matches_python_weekday_index():
    monday = datetime(2026, 7, 20)  # a Monday
    assert monday.weekday() == 0
    assert i18n.weekday_name(monday, "en") == "Monday"
    assert i18n.weekday_name(monday, "de") == "Montag"


def test_month_name_is_one_based():
    assert i18n.month_name(datetime(2026, 1, 15), "en") == "January"
    assert i18n.month_name(datetime(2026, 12, 15), "de") == "Dezember"


def test_format_datetime_uses_language_convention():
    dt = datetime(2026, 7, 20, 8, 13)
    assert i18n.format_datetime(dt, "en") == "2026-07-20 08:13"
    assert i18n.format_datetime(dt, "de") == "20.07.2026 08:13"


@pytest.mark.parametrize("fn", [i18n.weekday_name, i18n.month_name, i18n.format_datetime])
def test_unknown_language_falls_back_to_default(fn):
    """An unknown code must degrade to English, never raise."""
    dt = datetime(2026, 7, 20)
    assert fn(dt, "klingon") == fn(dt, i18n.DEFAULT_LANG)


def test_language_directive_names_the_language_and_date_format():
    directive = i18n.language_directive("de")
    assert "German" in directive
    assert "DD.MM.YYYY" in directive


def test_language_directive_lets_the_user_choose_the_language():
    """The UI language is the fallback, not an override.

    Writing German to an English interface is the normal case, and a model that
    follows instructions literally must not be told to answer in English anyway.
    """
    directive = i18n.language_directive("en")
    assert "Respond in the language the user writes to you in" in directive
    assert "Always respond in" not in directive


def test_language_directive_still_ignores_document_language():
    """The half of the old rule that was right: a German invoice inside an
    English session is still answered in the session's language."""
    directive = i18n.language_directive("en")
    assert "Never switch language because a document" in directive


def test_language_directive_orders_the_content_translated():
    """Not switching is only half of it.

    A model that has just read a German summary answers an English question in
    German — it is repeating what it read, not choosing a language. So the rule
    has to say what to DO with the foreign content, not only what not to do.
    """
    directive = i18n.language_directive("en")
    assert "TRANSLATE" in directive
    assert "summaries" in directive


def test_language_directive_exempts_names_and_quotations():
    """Translating a correspondent's name or a quoted clause would be wrong —
    and would break document references the user has to recognise."""
    directive = i18n.language_directive("en")
    assert "proper names" in directive
    assert "quotations stay in the" in directive


def test_language_directive_falls_back_for_unknown_code():
    assert i18n.language_directive("klingon") == i18n.language_directive(i18n.DEFAULT_LANG)


def test_n_marker_is_identity():
    """N_() must not transform — it only marks strings for extraction."""
    assert i18n.N_("Dashboard") == "Dashboard"


def test_no_english_catalog_exists():
    """English is the msgid; a locales/en/ catalog would shadow it."""
    assert not Path("locales/en").exists()


# ── catalog integrity ────────────────────────────────────────────────────────


@pytest.mark.parametrize("po_path", PO_FILES, ids=lambda p: p.parts[1])
def test_catalog_has_no_empty_translations(po_path):
    missing = [
        m.id for m in _catalog(po_path)
        if m.id and not m.string
    ]
    assert not missing, (
        f"{len(missing)} untranslated msgid(s) in {po_path} — these render "
        f"English in that locale. First few: {missing[:5]}"
    )


@pytest.mark.parametrize("po_path", PO_FILES, ids=lambda p: p.parts[1])
def test_catalog_has_no_fuzzy_entries(po_path):
    fuzzy = [m.id for m in _catalog(po_path) if m.id and m.fuzzy]
    assert not fuzzy, f"unreviewed fuzzy entries in {po_path}: {fuzzy[:5]}"


@pytest.mark.parametrize("po_path", PO_FILES, ids=lambda p: p.parts[1])
def test_catalog_placeholders_match(po_path):
    """A msgstr dropping or inventing a {placeholder} raises at render time."""
    mismatches = []
    for message in _catalog(po_path):
        if not message.id or not message.string:
            continue
        if isinstance(message.id, (list, tuple)):  # plural forms
            continue
        src = set(_PLACEHOLDER_RE.findall(message.id))
        dst = set(_PLACEHOLDER_RE.findall(message.string))
        if src != dst:
            mismatches.append((message.id, sorted(src), sorted(dst)))
    assert not mismatches, f"placeholder mismatch in {po_path}: {mismatches[:3]}"


@pytest.mark.parametrize("po_path", PO_FILES, ids=lambda p: p.parts[1])
def test_compiled_catalog_is_current(po_path):
    """A stale .mo means the running app shows different text than the .po.

    Compares the compiled catalog's translations against the source .po by
    content, not by file mtime: git does not preserve mtimes, so a timestamp
    check is non-deterministic after a fresh checkout (flaky in CI).
    """
    mo_path = po_path.with_suffix(".mo")
    assert mo_path.exists(), f"{mo_path} missing — run `pybabel compile -d locales`"

    po_cat = _catalog(po_path)
    with mo_path.open("rb") as fh:
        mo_cat = read_mo(fh)

    # pybabel compile emits only non-empty, non-fuzzy entries into the .mo, so
    # compare exactly that subset: every such source translation must be present
    # and identical in the compiled catalog.
    stale = [
        msg.id
        for msg in po_cat
        if msg.id and msg.string and not msg.fuzzy
        and getattr(mo_cat.get(msg.id, msg.context), "string", None) != msg.string
    ]
    assert not stale, (
        f"{mo_path} is out of date with {po_path} — run `pybabel compile -d locales`; "
        f"first stale: {stale[:3]}"
    )


def test_every_supported_language_has_a_catalog_or_is_the_source():
    """Selecting a language with no catalog silently falls back to English."""
    for code in i18n.SUPPORTED_LANGUAGES:
        if code == i18n.DEFAULT_LANG:
            continue
        assert Path(f"locales/{code}/LC_MESSAGES/messages.po").exists(), (
            f"{code} is selectable in SUPPORTED_LANGUAGES but has no catalog"
        )


# ── Answer-language detection ────────────────────────────────────────────────
#
# The UI language is a preference about the interface. What the user typed is
# what decides the answer language — writing German into an English UI (or the
# reverse) is the normal case here.


@pytest.mark.parametrize("text", [
    "What are the dimensions and fuel consumption values of my Ford Focus?",
    "Show me the invoice from Vodafone",
    "Can you list the documents from last month?",
])
def test_english_messages_detect_as_english_whatever_the_ui_says(text):
    assert i18n.detect_language(text, default="de") == "en"


@pytest.mark.parametrize("text", [
    "Welche Maße und Verbrauchswerte hat mein Ford Focus?",
    "Was steht in der Rechnung von Vodafone?",
    "Zeig mir bitte die Dokumente vom letzten Monat",
])
def test_german_messages_detect_as_german_whatever_the_ui_says(text):
    assert i18n.detect_language(text, default="en") == "de"


@pytest.mark.parametrize("text", ["ok", "#42", "", "42 12", "hm"])
def test_short_or_ambiguous_messages_fall_back_to_the_ui_language(text):
    """A bare document number says nothing about language — and guessing from
    it would flip the answer language mid-conversation."""
    assert i18n.detect_language(text, default="de") == "de"
    assert i18n.detect_language(text, default="en") == "en"


def test_umlauts_alone_settle_it():
    assert i18n.detect_language("Größe prüfen", default="en") == "de"


def test_answer_language_reminder_names_the_language_and_orders_translation():
    reminder = i18n.answer_language_reminder("en")
    assert "English" in reminder
    assert "translate" in reminder.lower()
    assert "quotations keep their original" in reminder


def test_answer_language_reminder_falls_back_for_unknown_code():
    assert i18n.answer_language_reminder("klingon") == i18n.answer_language_reminder(
        i18n.DEFAULT_LANG
    )
