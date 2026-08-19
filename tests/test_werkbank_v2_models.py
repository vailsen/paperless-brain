"""Werkbank v2 — the schema's own guarantees.

The architecture's premise is that honesty comes from the schema having a place
for "I don't know" and from code checking what the model claims. So the model
layer has to refuse the shapes that would let a claim travel without its
backing — and it has to strip the fields a model is not allowed to decide.
"""

import pytest
from pydantic import ValidationError

from werkbank.v2.models import (
    DEPTH_BUDGETS,
    Brief,
    Confidence,
    DepthBudget,
    Evidence,
    Fact,
    Source,
    SourceTrust,
    Subtask,
    SubtaskResult,
    apply_derived_confidence,
    derive_confidence,
    strip_llm_controlled,
)


def _quote_source(**kw) -> Source:
    base = dict(id="s1", type="paperless", quote="Die Frist beträgt drei Monate.")
    base.update(kw)
    return Source(**base)


# ── Ids ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["1", "f1", "st1-f1", "st1.f", "ST1.F1"])
def test_a_fact_id_must_be_globally_addressable(bad):
    """`st3.f1`, not `1` — the writer references facts across subtasks."""
    with pytest.raises(ValidationError):
        Fact(id=bad, claim="x", evidence=Evidence.NONE)


def test_a_fact_knows_which_subtask_it_belongs_to():
    assert Fact(id="st12.f3", claim="x", evidence=Evidence.NONE).subtask_id == "st12"


def test_a_result_refuses_facts_from_another_subtask():
    with pytest.raises(ValidationError):
        SubtaskResult(subtask_id="st1",
                      facts=[Fact(id="st2.f1", claim="x", evidence=Evidence.NONE)])


# ── Evidence needs its backing ────────────────────────────────────────────────


def test_quote_evidence_without_a_quote_is_rejected():
    with pytest.raises(ValidationError):
        Fact(id="st1.f1", claim="x", evidence=Evidence.QUOTE,
             sources=[Source(id="s1", type="paperless")])


def test_derived_evidence_without_a_parent_is_rejected():
    with pytest.raises(ValidationError):
        Fact(id="st1.f1", claim="x", evidence=Evidence.DERIVED)


def test_model_knowledge_is_allowed_but_stays_visible():
    """Not forbidden — it has to be expressible so the reflection can list it."""
    fact = Fact(id="st1.f1", claim="Ein Mietvertrag ist ein Dauerschuldverhältnis.",
                evidence=Evidence.MODEL_KNOWLEDGE)
    assert fact.evidence is Evidence.MODEL_KNOWLEDGE


# ── Fields the model does not get to decide ───────────────────────────────────


def test_trust_hits_confidence_and_self_check_are_stripped_from_model_output():
    """Whatever the model wrote for these is dropped at the parse boundary.

    Cheaper than arguing with a prompt, and it cannot be forgotten under load.
    """
    payload = {
        "subtask_id": "st1",
        "self_check": {"tool_calls": 99},
        "facts": [{
            "id": "st1.f1", "claim": "x", "evidence": "quote", "confidence": "high",
            "sources": [{"id": "s1", "type": "paperless", "quote": "Die Frist beträgt drei Monate.",
                         "trust": "authoritative", "hits": 4711}],
        }],
    }
    result = SubtaskResult.model_validate(strip_llm_controlled(payload))
    assert result.self_check.tool_calls == 0          # filled from the tool log later
    assert result.facts[0].confidence is None
    assert result.facts[0].sources[0].trust is SourceTrust.MODEL   # until the wrapper sets it
    assert result.facts[0].sources[0].hits is None


def test_stripping_does_not_mutate_the_caller_s_payload():
    payload = {"facts": [{"id": "st1.f1", "claim": "x", "evidence": "none",
                          "sources": [{"id": "s1", "type": "web", "trust": "external"}]}]}
    strip_llm_controlled(payload)
    assert payload["facts"][0]["sources"][0]["trust"] == "external"


# ── Confidence is derived, never asked for ────────────────────────────────────


@pytest.mark.parametrize("evidence,trust,expected", [
    (Evidence.QUOTE, SourceTrust.AUTHORITATIVE, Confidence.HIGH),
    (Evidence.QUOTE, SourceTrust.USER_ASSERTED, Confidence.MEDIUM),
    (Evidence.QUOTE, SourceTrust.EXTERNAL, Confidence.MEDIUM),
    (Evidence.COMPUTED, SourceTrust.COMPUTED, Confidence.HIGH),
    (Evidence.DERIVED, SourceTrust.DERIVED, Confidence.MEDIUM),
    (Evidence.MODEL_KNOWLEDGE, SourceTrust.MODEL, Confidence.LOW),
    (Evidence.NONE, None, Confidence.LOW),
])
def test_confidence_follows_from_evidence_and_trust(evidence, trust, expected):
    assert derive_confidence(evidence, trust) is expected


def test_a_note_and_a_document_saying_the_same_thing_do_not_carry_equal_weight():
    """The distinction users miss most: a vault note is a remembered guess."""
    document = apply_derived_confidence(Fact(
        id="st1.f1", claim="Frist: 3 Monate", evidence=Evidence.QUOTE,
        sources=[_quote_source(trust=SourceTrust.AUTHORITATIVE)]))
    note = apply_derived_confidence(Fact(
        id="st1.f2", claim="Frist: 3 Monate", evidence=Evidence.QUOTE,
        sources=[_quote_source(type="vault", trust=SourceTrust.USER_ASSERTED)]))
    assert document.confidence is Confidence.HIGH
    assert note.confidence is Confidence.MEDIUM


def test_the_strongest_source_decides_the_confidence():
    fact = apply_derived_confidence(Fact(
        id="st1.f1", claim="x", evidence=Evidence.QUOTE,
        sources=[_quote_source(type="vault", trust=SourceTrust.USER_ASSERTED),
                 _quote_source(id="s2", trust=SourceTrust.AUTHORITATIVE)]))
    assert fact.confidence is Confidence.HIGH


# ── Budgets ───────────────────────────────────────────────────────────────────


def test_every_depth_budget_has_a_configuration():
    assert set(DEPTH_BUDGETS) == set(DepthBudget)


def test_quick_skips_the_plan_critic_and_forbids_revisions():
    quick = DEPTH_BUDGETS[DepthBudget.QUICK]
    assert quick.run_plan_critic is False and quick.max_revisions == 0


def test_budgets_grow_monotonically():
    q, s, d = (DEPTH_BUDGETS[b] for b in (DepthBudget.QUICK, DepthBudget.STANDARD, DepthBudget.DEEP))
    assert q.max_subtasks < s.max_subtasks < d.max_subtasks
    assert q.max_revisions <= s.max_revisions <= d.max_revisions


def test_a_brief_exposes_its_budget():
    brief = Brief(original_request="x", goal="y", depth_budget=DepthBudget.DEEP)
    assert brief.budget.max_subtasks == 20


def test_the_default_budget_is_standard():
    assert Brief(original_request="x", goal="y").depth_budget is DepthBudget.STANDARD


# ── Plan ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["1", "task1", "st", "ST1"])
def test_subtask_ids_are_shaped_too(bad):
    with pytest.raises(ValidationError):
        Subtask(subtask_id=bad, question="x", agent="doc_researcher")


def test_a_subtask_carries_its_own_acceptance_criteria():
    """A subtask without a criterion is not plannable — it has no bar to clear."""
    sub = Subtask(subtask_id="st1", question="Welche Fristen?", agent="doc_researcher",
                  acceptance_criteria=["nennt jede Frist mit Datum"], covers_criteria=[0])
    assert sub.acceptance_criteria and sub.covers_criteria == [0]


# ── List fields the model sent as a string ────────────────────────────────────


def test_a_list_field_sent_as_a_json_string_is_still_a_list():
    """Seen in the wild: every list in the answer came back correctly except
    `assumptions`, which arrived as `"[\\"a\\", \\"b\\"]"`. Retrying does not help —
    the model reproduces its own habit, so three calls fail on the same line and
    the user sees a schema error for an answer that was otherwise complete."""
    brief = Brief.model_validate({
        "original_request": "x", "goal": "y",
        "assumptions": '["Valentin ist der Steuerpflichtige", "Bescheid 2024"]',
    })

    assert brief.assumptions == ["Valentin ist der Steuerpflichtige", "Bescheid 2024"]


def test_a_bullet_list_in_one_string_becomes_the_list_it_looks_like():
    brief = Brief.model_validate({
        "original_request": "x", "goal": "y",
        "acceptance_criteria": "- nennt jede Frist mit Datum\n- nennt das Quelldokument",
    })

    assert brief.acceptance_criteria == [
        "nennt jede Frist mit Datum", "nennt das Quelldokument"
    ]


def test_a_single_item_sent_unwrapped_is_wrapped():
    brief = Brief.model_validate({
        "original_request": "x", "goal": "y", "out_of_scope": "Anlageberatung",
    })

    assert brief.out_of_scope == ["Anlageberatung"]


def test_coercion_widens_what_is_accepted_never_what_is_believed():
    """A shape that is not unambiguously a list is passed through, so validation
    still rejects it. Coercion must never turn a wrong answer into a valid one."""
    with pytest.raises(ValidationError):
        Brief.model_validate({"original_request": "x", "goal": "y", "assumptions": 42})

    with pytest.raises(ValidationError):
        Fact.model_validate({
            "id": "st1.f1", "claim": "c", "evidence": "quote", "sources": 7,
        })


def test_a_fact_still_needs_real_sources_after_coercion():
    """`sources` is a list of models, not of strings: a stray string in it must
    not become a Source with empty everything."""
    with pytest.raises(ValidationError):
        Fact.model_validate({
            "id": "st1.f1", "claim": "c", "evidence": "quote",
            "sources": "doc:12#p1",
        })


def test_a_json_string_with_german_quotes_inside_still_yields_its_items():
    """The real failure: `["„ETCS-Antennen" bedeutet …", "…"]` is not valid JSON
    — the plain quote after a typographic „ closes the string early. The item
    boundary `", "` is still unambiguous, so the items are recoverable."""
    raw = (
        '["„ETCS-Antennen" bedeutet die fahrgestellmontierten Antennen", '
        '"„menschliche Unfälle" bedeutet Unfälle mit Opfern", '
        '"Zeitraum: seit ca. 2000 bis heute"]'
    )
    brief = Brief.model_validate({"original_request": "x", "goal": "y", "assumptions": raw})

    assert len(brief.assumptions) == 3
    assert brief.assumptions[0].startswith("„ETCS-Antennen")
    assert brief.assumptions[2] == "Zeitraum: seit ca. 2000 bis heute"


def test_a_bracketed_blob_is_never_accepted_as_one_item():
    """Worse than an error: the user would confirm one giant 'assumption' that is
    really five, and the run would be measured against it."""
    from werkbank.v2.models import _as_list

    raw = '["eins", "zwei", "drei"]'
    assert _as_list(raw) == ["eins", "zwei", "drei"]

    unparseable = '[{"a": }]'
    assert _as_list(unparseable) == unparseable      # passed through, so it fails


# ── Mislabelled evidence is corrected, not rejected ───────────────────────────


def test_derived_without_a_parent_is_relabelled_to_what_it_actually_is():
    """The failure: a subtask fetched six case-report pages, called its facts
    `derived` without filling `derived_from`, failed validation three times and
    was reported as *nothing found* — with the pages in the tool log."""
    from werkbank.v2.models import relabel_mislabelled_evidence

    payload = relabel_mislabelled_evidence({"facts": [
        {"id": "st2.f1", "claim": "Ein Fallbericht beschreibt Symptome.",
         "evidence": "derived",
         "sources": [{"id": "s8", "type": "web", "quote": "developed symptoms"}]},
        {"id": "st2.f2", "claim": "Keine Treffer im Archiv.", "evidence": "derived",
         "sources": [{"id": "s1", "type": "search_exact", "query": "radar", "hits": 0}]},
        {"id": "st2.f3", "claim": "Allgemein bekannt.", "evidence": "derived",
         "sources": []},
        {"id": "st2.f4", "claim": "Folgt aus f1.", "evidence": "derived",
         "derived_from": ["st2.f1"]},
    ]})

    kinds = [f["evidence"] for f in payload["facts"]]
    assert kinds == ["quote", "computed", "model_knowledge", "derived"]


def test_relabelling_changes_the_word_never_the_checking():
    """A relabelled `quote` still has to survive D2 against the retrieved text."""
    from werkbank.v2 import checks
    from werkbank.v2.models import relabel_mislabelled_evidence

    payload = relabel_mislabelled_evidence({"facts": [
        {"id": "st1.f1", "claim": "Die Frist beträgt sechs Wochen.", "evidence": "derived",
         "sources": [{"id": "s1", "type": "web",
                      "quote": "Die Frist beträgt sechs Wochen"}]},
    ]})
    fact = Fact.model_validate(payload["facts"][0])
    assert fact.evidence is Evidence.QUOTE          # relabelled…
    result = SubtaskResult(subtask_id="st1", agent="web_researcher", facts=[fact])

    # …and then held to what a quote has to survive: the page said three.
    report = checks.check_d2_quote_grounding(
        result, {"s1": "Die Frist beträgt drei Wochen"}
    )
    assert "st1.f1" in report.rejected_fact_ids


def test_code_that_builds_a_derived_fact_without_a_parent_still_fails():
    """The rescue is for model payloads. In code it is a bug, and stays one."""
    with pytest.raises(ValidationError):
        Fact(id="st1.f1", claim="x", evidence=Evidence.DERIVED)


def test_a_single_item_sent_as_an_object_becomes_a_list_of_one():
    """`"facts": {…}` instead of `"facts": [{…}]` failed as "Input should be a
    valid list" three attempts running, and the subtask lost everything it had
    retrieved."""
    result = SubtaskResult.model_validate({
        "subtask_id": "st3", "agent": "doc_researcher",
        "facts": {"id": "st3.f1", "claim": "Nichts gefunden.",
                  "evidence": "model_knowledge"},
        "gaps": None,
    })

    assert len(result.facts) == 1 and result.facts[0].id == "st3.f1"
    assert result.gaps == []


def test_a_trailing_comma_does_not_turn_the_list_into_one_item():
    """The third variant of the same habit, and the one that got through: the
    blob ended `],`. My bracket test looked at the last character, saw a comma,
    and never tried to parse it — so the user was shown four assumptions glued
    into one. `ast` does parse it, as a *tuple containing the list*, which is
    not the list either."""
    raw = (
        ' ["\'ETCS-Antennen\' umfasst die Antennen des ETCS.", '
        '"\'Ähnliche Vorfälle\' meint andere Sendeantennen.", '
        '"\'Schadensbeschreibung\' meint die körperliche Reaktion."],'
    )
    brief = Brief.model_validate({
        "original_request": "x", "goal": "y", "assumptions": raw,
    })

    assert len(brief.assumptions) == 3
    assert brief.assumptions[0].startswith("'ETCS-Antennen'")
    assert brief.assumptions[2].endswith("körperliche Reaktion.")


def test_a_list_holding_one_serialised_list_is_the_list_it_holds():
    brief = Brief.model_validate({
        "original_request": "x", "goal": "y",
        "assumptions": ['["eins", "zwei", "drei"]'],
    })

    assert brief.assumptions == ["eins", "zwei", "drei"]


def test_an_ordinary_one_item_list_is_left_alone():
    """The unwrapping must not eat a legitimate single assumption."""
    brief = Brief.model_validate({
        "original_request": "x", "goal": "y",
        "assumptions": ["Es geht um den Vertrag von 2024."],
    })

    assert brief.assumptions == ["Es geht um den Vertrag von 2024."]


# ── Broken JSON that is only broken typographically ───────────────────────────


def test_a_german_closing_quote_inside_a_value_does_not_destroy_the_answer():
    """The real payload: three complete facts about the Wikipedia article on
    prions, thrown away three attempts running because `„Prion" und ist …`
    ends the JSON string three words early."""
    raw = (
        '[{"id": "st1.f1", "claim": "Der Artikel trägt den Titel „Prion" und ist '
        'erreichbar.", "evidence": "quote", '
        '"sources": [{"id": "s2", "type": "web", "quote": "Prion"}]}]'
    )
    result = SubtaskResult.model_validate({
        "subtask_id": "st1", "agent": "web_researcher", "facts": raw,
    })

    assert len(result.facts) == 1
    assert "„Prion\" und ist erreichbar." in result.facts[0].claim


def test_the_repair_leaves_valid_json_exactly_as_it_was():
    """It runs on answers that already parse, so it must be a no-op there."""
    import json

    from werkbank.v2.models import repair_json

    for raw in (
        '[{"a": "plain"}]',
        '{"quote": "He said \\"hi\\" loudly"}',
        '{"url": "https://x.example/a?b=1", "n": 3}',
        '{"nested": {"x": ["a", "b"]}}',
    ):
        assert repair_json(raw) == raw
        json.loads(repair_json(raw))


def test_the_repair_only_escapes_quotes_that_cannot_be_terminators():
    """A quote followed by a delimiter closes the string; anything else is
    content the model failed to escape."""
    import json

    from werkbank.v2.models import repair_json

    fixed = repair_json('{"a": "„X" ja", "b": 2}')
    assert json.loads(fixed) == {"a": '„X" ja', "b": 2}


def test_a_content_quote_followed_by_a_comma_is_still_content():
    """The shape a lookahead cannot decide: `(als „M.Milbich", Bauleiter)` looks
    exactly like the end of a value. The real payload — one document found after
    twelve tool calls — was discarded three attempts running because of it."""
    raw = (
        '[{"id": "st2.f1", "claim": "Genau ein Dokument erwähnt Marcel Milbich '
        '(als „M.Milbich", Bauleiter), Datum 05.01.2023.", "evidence": "quote", '
        '"sources": [{"id": "s1", "type": "document_page", '
        '"quote": "Bauleiter M.Milbich"}]}]'
    )
    result = SubtaskResult.model_validate({
        "subtask_id": "st2", "agent": "doc_researcher", "facts": raw,
    })

    assert len(result.facts) == 1
    assert "(als „M.Milbich\", Bauleiter)" in result.facts[0].claim
    assert result.facts[0].sources[0].quote == "Bauleiter M.Milbich"
