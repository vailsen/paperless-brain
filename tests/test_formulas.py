"""Formulas: LaTeX in the extraction, its JSON escapes, and the scoped re-ingest."""

import json

import pytest

from config.extraction_rules import BASE_INSTRUCTIONS, PROMPT_VERSION
from services.sidecar_service import is_outdated
from services.vision import repair_latex_escapes


def _sidecar(version: str, text: str = "", images: list | None = None) -> dict:
    return {
        "prompt_version": version,
        "pages": [{"page_number": 1, "page_text": text, "images": images or []}],
    }


def test_prompt_asks_for_latex_but_not_for_units():
    assert "LaTeX" in BASE_INSTRUCTIONS
    assert "5 m²" in BASE_INSTRUCTIONS  # values with units stay plain text


# ── Which old sidecars a formula re-ingest touches ───────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Spannung σ = F / A",
        "∑ aller Posten",
        "Für x = 2a + b gilt",
        "Abweichung Δ 0,3 mm",
        "Wert ≤ 5",
    ],
)
def test_a_page_showing_mathematics_is_reingested(text):
    assert is_outdated(_sidecar("0.3", text), "0.4")


@pytest.mark.parametrize(
    "text",
    [
        "Wohnfläche 120 m², Preis 2 × 3 Euro ± 5 %",
        "Blutdruck 210/80 mmHg",
        "Messbereich 5 µm",  # micro sign
        "Messbereich 5 μm",  # Greek mu, the same unit as typed by a model
        "Kundennummer: K-2024=ok",
        "Gesamtbetrag = 1.014,01 EUR",  # a word, not a one-letter variable
    ],
)
def test_ordinary_pages_stay_current(text):
    assert not is_outdated(_sidecar("0.3", text), "0.4")


def test_steps_are_walked_from_the_sidecars_version():
    # 0.2 → 0.4 passes the picture step and the formula step.
    assert not is_outdated(_sidecar("0.2", "Rechnung"), "0.4")
    assert is_outdated(_sidecar("0.2", "[Image description: chart]"), "0.4")
    assert is_outdated(_sidecar("0.2", "σ = F / A"), "0.4")
    assert is_outdated(_sidecar("0.2", "", images=[{"description": "Foto"}]), "0.4")


def test_signatures_step_reingests_every_document():
    # 0.5 (stamps, seals, signatures) is not scoped: the old text cannot tell.
    assert PROMPT_VERSION == "0.5"
    assert is_outdated(_sidecar("0.4", "Rechnung"), "0.5")
    assert is_outdated(_sidecar("0.2", "Rechnung"), "0.5")


def test_unknown_versions_reingest_everything():
    assert is_outdated(_sidecar("0.1", "Rechnung"), "0.4")
    assert is_outdated(_sidecar("", "Rechnung"), "0.4")
    assert is_outdated(_sidecar("0.4", "Rechnung"), "0.5")
    assert not is_outdated(_sidecar("0.4", "σ"), "0.4")


# ── Backslashes in the model's JSON ──────────────────────────────────────────


def _parse(raw: str) -> str:
    return json.loads(repair_latex_escapes(raw))["t"]


def test_invalid_latex_escapes_no_longer_fail_the_page():
    assert _parse(r'{"t": "$\sigma = \cdot$"}') == r"$\sigma = \cdot$"


@pytest.mark.parametrize("cmd", ["frac{a}{b}", "times b", "beta", "nabla f", "rho", "theta", "text{m}"])
def test_commands_that_look_like_json_escapes_keep_their_backslash(cmd):
    assert _parse('{"t": "$\\' + cmd + '$"}') == "$\\" + cmd + "$"


def test_real_json_escapes_are_left_alone():
    raw = '{"t": "Zeile\\nnotwendig\\tTab \\"x\\" \\u00e4 \\\\alpha /\\/"}'
    assert repair_latex_escapes(raw) == raw
    assert _parse(raw) == 'Zeile\nnotwendig\tTab "x" ä \\alpha //'


def test_text_without_backslashes_is_returned_as_is():
    raw = '{"t": "nothing to do"}'
    assert repair_latex_escapes(raw) is raw
