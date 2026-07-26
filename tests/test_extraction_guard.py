"""Ingestion guard against degenerate vision output.

Calibration matters more than the code here. The thresholds sit inside measured
gaps from a real 1,333-page archive, and the false-positive risk is concrete:
dense tabular documents legitimately reach a 0.09 unique-word ratio because the
same field labels repeat on every row. A guard that rejected those would be
worse than no guard.
"""

import pytest

from services.extraction_guard import (
    MAX_PLAUSIBLE_PAGE_WORDS,
    MIN_UNIQUE_WORD_RATIO,
    MIN_WORDS_FOR_RATIO_CHECK,
    inspect_page_text,
    salvage_page_text,
)


def _looped(phrase: str, times: int) -> str:
    return " ".join([phrase] * times)


# ── healthy text passes ──────────────────────────────────────────────────────


@pytest.mark.parametrize("text", ["", "   ", "A short page.", "one two three"])
def test_short_or_empty_text_is_accepted(text):
    """Ratios are meaningless below the length floor — never reject on them."""
    assert inspect_page_text(text)


def test_normal_prose_is_accepted():
    prose = " ".join(f"word{i}" for i in range(800))
    assert inspect_page_text(prose)


def test_dense_tabular_text_is_accepted():
    """Real case: mileage logs and cost breakdowns repeat field labels on every
    row and reach a ~0.09 unique ratio. These must not be flagged."""
    rows = [
        f"Datum: {d:02d}.01.2023, Zweck: Fahrt, Von: Ort A, "
        f"Bis: Ort B, Strecke: 100 km, Fahrtkosten: 30,00 EUR"
        for d in range(1, 60)
    ]
    verdict = inspect_page_text(" ".join(rows))
    assert verdict, f"false positive on legitimate tabular text: {verdict.reason}"
    assert verdict.unique_ratio < 0.25, "fixture should be genuinely repetitive"


def test_ratio_just_above_the_threshold_is_accepted():
    unique = [f"w{i}" for i in range(40)]
    text = " ".join(unique + ["pad"] * 960)  # ratio ~0.041
    verdict = inspect_page_text(text)
    assert verdict.unique_ratio > MIN_UNIQUE_WORD_RATIO
    assert verdict


# ── degenerate text is caught ────────────────────────────────────────────────


def test_repetition_loop_is_rejected():
    verdict = inspect_page_text(_looped("the same phrase again", 500))
    assert not verdict
    assert "repetition loop" in verdict.reason


def test_implausible_page_length_is_rejected():
    """Independent of ratio: no real page holds this many words."""
    varied = " ".join(f"w{i}" for i in range(MAX_PLAUSIBLE_PAGE_WORDS + 500))
    verdict = inspect_page_text(varied)
    assert not verdict
    assert "implausible page length" in verdict.reason
    assert verdict.unique_ratio > 0.9, "length trigger must not depend on ratio"


def test_the_real_archive_failure_is_caught():
    """The case that motivated this: 119,165 words at a 0.0037 unique ratio."""
    verdict = inspect_page_text(_looped("Wird das Ruecktritt von den Bestimmungen", 20000))
    assert not verdict


def test_short_repetitive_text_is_not_flagged():
    """Below the length floor, repetition is normal (form labels, headers)."""
    assert inspect_page_text(_looped("Name Datum", 20))


def test_verdict_reports_its_measurements():
    verdict = inspect_page_text(_looped("a b c d", 400))
    assert verdict.word_count == 1600
    assert verdict.unique_ratio == pytest.approx(4 / 1600)
    assert verdict.reason


def test_verdict_is_falsy_when_rejected_and_truthy_when_ok():
    assert bool(inspect_page_text("normal short page"))
    assert not bool(inspect_page_text(_looped("loop loop", 5000)))


@pytest.mark.parametrize("n", [MIN_WORDS_FOR_RATIO_CHECK - 1, MIN_WORDS_FOR_RATIO_CHECK])
def test_ratio_check_activates_at_the_length_floor(n):
    text = " ".join(["same"] * n)
    verdict = inspect_page_text(text)
    assert verdict.ok == (n < MIN_WORDS_FOR_RATIO_CHECK)


# ── salvage ──────────────────────────────────────────────────────────────────


def test_salvage_keeps_short_text_untouched():
    text = "a normal page of text"
    assert salvage_page_text(text) == text


def test_salvage_truncates_to_the_word_cap():
    words = salvage_page_text(_looped("x", 50000)).split()
    assert len(words) == MAX_PLAUSIBLE_PAGE_WORDS


def test_salvage_keeps_the_head_not_the_tail():
    """A loop starts partway through — the opening is usually genuine content."""
    text = "GENUINE OPENING CONTENT " + _looped("loop", 50000)
    salvaged = salvage_page_text(text)
    assert salvaged.startswith("GENUINE OPENING CONTENT")


def test_salvaged_text_passes_the_length_check():
    salvaged = salvage_page_text(" ".join(f"w{i}" for i in range(50000)))
    assert inspect_page_text(salvaged)


def test_salvage_handles_empty_input():
    assert salvage_page_text("") == ""
