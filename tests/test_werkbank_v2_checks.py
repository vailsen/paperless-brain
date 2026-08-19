"""Werkbank v2 — the deterministic checks.

These are the part of the system that cannot be talked out of a verdict. Every
test here is a failure mode observed in v1: a citation that does not appear in
the document, a total that does not add up, an answer produced without looking
anything up, a confident narrative standing on nothing.

No LLM in this file, by design — the checks have to be verifiable without one.
"""

import pytest

from werkbank.v2 import checks
from werkbank.v2.models import (
    Evidence,
    Fact,
    FactKind,
    Gap,
    GapReason,
    SelfCheck,
    Source,
    SourceTrust,
    SubtaskResult,
    SubtaskStatus,
)

PAGE = (
    "Mietvertrag Musterstraße 12\n\n"
    "§ 4 Kündigung\nDie Kündigungsfrist beträgt drei Monate zum Quartalsende.\n"
    "Die Kaution beläuft sich auf 1.240,00 EUR.\n"
)


def _fact(**kw) -> Fact:
    base = dict(
        id="st1.f1",
        claim="Die Kündigungsfrist beträgt drei Monate.",
        evidence=Evidence.QUOTE,
        sources=[Source(id="s1", type="paperless", ref="doc:12#p1",
                        quote="Die Kündigungsfrist beträgt drei Monate zum Quartalsende.")],
    )
    base.update(kw)
    return Fact(**base)


def _result(facts, **kw) -> SubtaskResult:
    base = dict(subtask_id="st1", question="Welche Frist?", facts=facts)
    base.update(kw)
    return SubtaskResult(**base)


# ── D2: quote grounding ───────────────────────────────────────────────────────


def test_a_verbatim_quote_passes():
    report = checks.check_d2_quote_grounding(_result([_fact()]), {"s1": PAGE})
    assert report.passed and not report.rejected_fact_ids


def test_the_same_quote_reflowed_still_passes():
    """A PDF breaks lines wherever it likes; that is not a different sentence."""
    fact = _fact(sources=[Source(
        id="s1", type="paperless",
        quote="Die  Kündigungsfrist\nbeträgt   drei Monate\nzum Quartalsende.",
    )])
    assert checks.check_d2_quote_grounding(_result([fact]), {"s1": PAGE}).passed


def test_typographic_quotes_and_dashes_do_not_break_the_match():
    page = 'Der Vertrag läuft — laut „Anlage 1" — bis 2027.'
    fact = _fact(sources=[Source(
        id="s1", type="paperless", quote='Der Vertrag läuft - laut "Anlage 1" - bis 2027.',
    )])
    assert checks.check_d2_quote_grounding(_result([fact]), {"s1": page}).passed


def test_a_tampered_quote_is_rejected():
    """One word changed — the exact case a model reads straight past."""
    fact = _fact(sources=[Source(
        id="s1", type="paperless",
        quote="Die Kündigungsfrist beträgt sechs Monate zum Quartalsende.",
    )])
    report = checks.check_d2_quote_grounding(_result([fact]), {"s1": PAGE})
    assert not report.passed and report.rejected_fact_ids == ["st1.f1"]


def test_an_invented_quote_is_rejected():
    fact = _fact(sources=[Source(
        id="s1", type="paperless",
        quote="Eine Verlängerungsoption um zwölf Monate ist vereinbart.",
    )])
    assert not checks.check_d2_quote_grounding(_result([fact]), {"s1": PAGE}).passed


def test_a_quote_attributed_to_the_wrong_source_still_passes_if_retrieved():
    """Misattribution is a bookkeeping slip; fabrication is the thing to catch."""
    fact = _fact(sources=[Source(
        id="s9", type="paperless",
        quote="Die Kündigungsfrist beträgt drei Monate zum Quartalsende.",
    )])
    assert checks.check_d2_quote_grounding(_result([fact]), {"s1": PAGE}).passed


def test_a_two_word_quote_is_not_evidence():
    fact = _fact(sources=[Source(id="s1", type="paperless", quote="drei Monate")])
    assert not checks.check_d2_quote_grounding(_result([fact]), {"s1": PAGE}).passed


def test_quote_evidence_without_any_quote_cannot_even_be_built():
    with pytest.raises(ValueError):
        _fact(sources=[Source(id="s1", type="paperless", quote="")])


# ── D3: computed ──────────────────────────────────────────────────────────────


def test_arithmetic_that_adds_up_passes():
    fact = _fact(
        claim="Die Summe beträgt 4230 EUR.",
        evidence=Evidence.COMPUTED,
        expression="1240 + 890 + 2100",
        sources=[],
    )
    assert checks.check_d3_computed(_result([fact])).passed


def test_arithmetic_that_does_not_match_the_claim_is_rejected():
    fact = _fact(
        claim="Die Summe beträgt 5000 EUR.",
        evidence=Evidence.COMPUTED,
        expression="1240 + 890 + 2100",
        sources=[],
    )
    report = checks.check_d3_computed(_result([fact]))
    assert not report.passed and "4230" in report.messages[0]


def test_computed_without_expression_and_without_query_is_rejected():
    fact = _fact(claim="Es gibt 17 Rechnungen.", evidence=Evidence.COMPUTED, sources=[])
    assert not checks.check_d3_computed(_result([fact])).passed


def test_a_metadata_query_needs_no_quote_but_needs_hits():
    fact = _fact(
        claim="Es gibt 17 Rechnungen von den Stadtwerken.",
        evidence=Evidence.COMPUTED,
        sources=[Source(id="s1", type="paperless", trust=SourceTrust.COMPUTED,
                        query="correspondent=Stadtwerke&created__year=2025", hits=17)],
    )
    assert checks.check_d3_computed(_result([fact])).passed


@pytest.mark.parametrize("expr", ["__import__('os').system('rm -rf /')", "open('/etc/passwd')", "1 ** 999999999"])
def test_expression_evaluation_refuses_anything_that_is_not_arithmetic(expr):
    assert checks.eval_expression(expr) is None


def test_expression_evaluation_does_the_arithmetic():
    assert checks.eval_expression("1240 + 890 + 2100") == pytest.approx(4230)
    assert checks.eval_expression("(10 - 4) * 2.5") == pytest.approx(15)


# ── D4: derived ───────────────────────────────────────────────────────────────


def test_derived_from_a_known_fact_passes():
    fact = _fact(id="st2.f1", evidence=Evidence.DERIVED, derived_from=["st1.f4"], sources=[])
    result = SubtaskResult(subtask_id="st2", facts=[fact])
    assert checks.check_d4_derived(result, {"st1.f4"}).passed


def test_derived_from_a_fact_that_does_not_exist_is_rejected():
    fact = _fact(id="st2.f1", evidence=Evidence.DERIVED, derived_from=["st9.f1"], sources=[])
    result = SubtaskResult(subtask_id="st2", facts=[fact])
    report = checks.check_d4_derived(result, {"st1.f4"})
    assert not report.passed and report.rejected_fact_ids == ["st2.f1"]


def test_derived_without_derived_from_cannot_be_built():
    with pytest.raises(ValueError):
        _fact(evidence=Evidence.DERIVED, derived_from=[], sources=[])


# ── D5: tool use ──────────────────────────────────────────────────────────────


def test_no_tool_call_with_tools_available_forces_a_revision():
    """The 'answered from memory' failure, caught by counting rather than asking."""
    result = _result([_fact()], self_check=SelfCheck(tool_calls=0))
    assert not checks.check_d5_tool_use(result, tools_were_available=True).passed


def test_no_tool_call_is_fine_when_there_were_no_tools():
    result = _result([_fact()], self_check=SelfCheck(tool_calls=0))
    assert checks.check_d5_tool_use(result, tools_were_available=False).passed


# ── D6: narrative markers ─────────────────────────────────────────────────────


def test_an_unknown_marker_is_stripped_and_flagged():
    result = _result([_fact()], narrative="Die Frist ist klar [st1.f1] und lang [st3.f99].")
    cleaned, report = checks.check_d6_narrative_markers(result)
    assert "[st3.f99]" not in cleaned
    assert "[st1.f1]" in cleaned
    assert report.flags


def test_a_narrative_with_only_known_markers_is_untouched():
    result = _result([_fact()], narrative="Die Frist ist klar [st1.f1].")
    cleaned, report = checks.check_d6_narrative_markers(result)
    assert cleaned == "Die Frist ist klar [st1.f1]." and not report.flags


# ── D7: source restriction ────────────────────────────────────────────────────


def test_citing_outside_the_restriction_is_rejected():
    fact = _fact(sources=[Source(id="s1", type="vault", quote="Kündigungsfrist drei Monate laut Notiz")])
    result = _result([fact], sources_restrict=["paperless"])
    report = checks.check_d7_sources_restrict(result)
    assert not report.passed and "vault" in report.messages[0]


# ── D8: nothing survived ──────────────────────────────────────────────────────


def test_no_facts_and_no_gaps_is_unresolvable():
    assert not checks.check_d8_empty_result(_result([])).passed


def test_no_facts_but_a_declared_gap_is_a_legitimate_outcome():
    """An empty search result is a result — the whole point of having gaps."""
    result = _result([], gaps=[Gap(question="Gibt es eine Nebenabrede?",
                                   reason=GapReason.NOT_FOUND)])
    assert checks.check_d8_empty_result(result).passed


# ── D9: report paragraphs ─────────────────────────────────────────────────────


def test_a_paragraph_without_a_fact_marker_is_flagged():
    report = checks.check_d9_paragraph_markers(
        "## Ergebnis\n\nDie Frist beträgt drei Monate [st1.f1].\n\n"
        "Insgesamt wirkt der Vertrag ausgewogen.\n"
    )
    assert not report.passed
    assert "ausgewogen" in report.flags[0]


def test_headings_and_tables_need_no_marker():
    report = checks.check_d9_paragraph_markers(
        "# Titel\n\n| a | b |\n|---|---|\n\n- ein Punkt\n\nText mit Beleg [st1.f1].\n"
    )
    assert report.passed


# ── The full pass ─────────────────────────────────────────────────────────────


def test_run_all_drops_rejected_facts_before_any_critic_sees_them():
    good = _fact()
    bad = _fact(id="st1.f2", sources=[Source(
        id="s1", type="paperless", quote="Eine Verlängerung um zwölf Monate ist vereinbart.")])
    result = _result([good, bad], self_check=SelfCheck(tool_calls=2),
                     narrative="Frist [st1.f1], Option [st1.f2].")
    survived, report = checks.run_all(
        result, raw_texts={"s1": PAGE}, known_fact_ids=set(), tools_were_available=True
    )
    assert survived.fact_ids() == {"st1.f1"}
    assert "st1.f2" in report.rejected_fact_ids
    # The narrative may no longer point at the fact that was thrown out.
    assert "[st1.f2]" not in survived.narrative


def test_run_all_marks_a_subtask_unresolvable_when_everything_was_rejected():
    bad = _fact(sources=[Source(id="s1", type="paperless",
                                quote="Frei erfundener Satz über eine Verlängerung.")])
    result = _result([bad], self_check=SelfCheck(tool_calls=1))
    survived, report = checks.run_all(
        result, raw_texts={"s1": PAGE}, known_fact_ids=set(), tools_were_available=True
    )
    assert survived.status is SubtaskStatus.UNRESOLVABLE
    assert not survived.facts and not report.passed


def test_a_table_fact_survives_the_pass():
    fact = _fact(
        id="st1.f3",
        kind=FactKind.TABLE,
        claim="| Posten | Betrag |\n|---|---|\n| Kaution | 1.240,00 EUR |",
        evidence=Evidence.QUOTE,
        sources=[Source(id="s1", type="paperless",
                        quote="Die Kaution beläuft sich auf 1.240,00 EUR.")],
    )
    survived, _ = checks.run_all(
        _result([fact], self_check=SelfCheck(tool_calls=1)),
        raw_texts={"s1": PAGE}, known_fact_ids=set(), tools_were_available=True,
    )
    assert survived.fact_ids() == {"st1.f3"}


# ── The single-token swap ─────────────────────────────────────────────────────
#
# A changed number or a changed quantity word leaves ~90% of a long sentence
# intact, so similarity alone waves it through. These are the cases that decide
# whether a report can be trusted at all.


@pytest.mark.parametrize("tampered", [
    "Die Kaution beläuft sich auf 1.420,00 EUR.",       # digits swapped
    "Die Kündigungsfrist beträgt sechs Monate zum Quartalsende.",
    "Die Kündigungsfrist beträgt drei Wochen zum Quartalsende.",
])
def test_one_changed_token_is_enough_to_reject(tampered):
    fact = _fact(sources=[Source(id="s1", type="paperless", quote=tampered)])
    report = checks.check_d2_quote_grounding(_result([fact]), {"s1": PAGE})
    assert not report.passed, f"{tampered!r} slipped through"


def test_a_number_that_is_in_the_document_is_accepted():
    fact = _fact(
        claim="Die Kaution beträgt 1.240,00 EUR.",
        sources=[Source(id="s1", type="paperless",
                        quote="Die Kaution beläuft sich auf 1.240,00 EUR.")],
    )
    assert checks.check_d2_quote_grounding(_result([fact]), {"s1": PAGE}).passed


# ── A negative finding is evidence, not an error ──────────────────────────────


def test_a_metadata_fact_survives_an_expression_that_is_prose():
    """The real failure: the model wrote `search_exact(ETCS)=0; …` into
    `expression`. That is not arithmetic, so D3 rejected all three facts, the
    subtask was left with nothing, and D8 called it unresolvable — throwing away
    a correct "I searched and found nothing", which is the answer."""
    fact = Fact(
        id="st1.f1", claim="In Paperless liegen keine Dokumente zu ETCS-Antennen.",
        evidence=Evidence.COMPUTED,
        expression="search_exact(ETCS)=0; search_exact(Funkmast)=0",
        sources=[
            Source(id="s7", type="search_exact", query="ETCS", hits=0),
            Source(id="s8", type="search_exact", query="Funkmast", hits=0),
        ],
    )
    result = SubtaskResult(subtask_id="st1", agent="doc_researcher", facts=[fact])

    report = checks.check_d3_computed(result)

    assert report.passed
    assert report.flags                      # it says what it did
    assert fact.expression == ""             # the prose is dropped, not believed


def test_arithmetic_that_does_not_add_up_is_still_rejected():
    fact = Fact(
        id="st1.f1", claim="Die Summe beträgt 5000 Euro.", evidence=Evidence.COMPUTED,
        expression="1240 + 890 + 2100",
        sources=[Source(id="s1", type="search", query="rechnungen", hits=3)],
    )
    result = SubtaskResult(subtask_id="st1", agent="doc_researcher", facts=[fact])

    assert not checks.check_d3_computed(result).passed


def test_computed_with_neither_arithmetic_nor_a_query_is_rejected():
    fact = Fact(
        id="st1.f1", claim="Es gibt nichts.", evidence=Evidence.COMPUTED,
        expression="ich habe gesucht",
        sources=[Source(id="s1", type="search")],
    )
    result = SubtaskResult(subtask_id="st1", agent="doc_researcher", facts=[fact])

    assert not checks.check_d3_computed(result).passed


def test_searching_and_then_answering_from_memory_is_caught():
    """Twelve searches, three pages fetched, and then six facts all marked
    `model_knowledge` — the retrieval happened and the answer was written from
    memory anyway. The tool log makes that countable."""
    facts = [
        Fact(id=f"st1.f{i}", claim=f"Behauptung {i}", evidence=Evidence.MODEL_KNOWLEDGE)
        for i in range(1, 4)
    ]
    result = _result(facts, self_check=SelfCheck(tool_calls=12))

    report = checks.check_d5_tool_use(result, tools_were_available=True)

    assert not report.passed
    assert "not one of them is backed" in report.messages[0]


def test_one_sourced_fact_among_them_is_enough_to_pass():
    """The check is about answering from memory *instead* of from the tools, not
    about every fact needing a source: a run may legitimately mix them."""
    result = _result(
        [_fact(), Fact(id="st1.f2", claim="Allgemein bekannt.",
                       evidence=Evidence.MODEL_KNOWLEDGE)],
        self_check=SelfCheck(tool_calls=12),
    )

    assert checks.check_d5_tool_use(result, tools_were_available=True).passed
