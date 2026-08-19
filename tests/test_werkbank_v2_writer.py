"""Werkbank v2 — contradiction pass, generated reflection, writer.

The claim this phase has to make good on: a report cannot look complete while
quietly leaving out that a subtask found nothing, that a claim has no source, or
that two sources disagree. Every one of those is assembled by code from the run
state and put back if the model removes it.
"""

import asyncio

import pytest

from werkbank.v2 import contradictions, reflection, writer
from werkbank.v2.llm import LLMContext
from werkbank.v2.models import (
    Brief,
    CriterionCheck,
    CriterionVerdict,
    CriticDecision,
    CriticVerdict,
    Evidence,
    Fact,
    Gap,
    GapReason,
    RunState,
    Source,
    SourceTrust,
    SubtaskResult,
    SubtaskStatus,
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def ctx(tmp_path):
    return LLMContext(model="stub", user_id="alice", run_id="r1", log_dir=tmp_path)


@pytest.fixture
def replies(monkeypatch):
    box = {"queue": [], "calls": []}

    async def fake(system, messages, **kw):
        box["calls"].append({"system": system, "messages": messages,
                             "tool": kw.get("tool_name")})
        return box["queue"].pop(0) if box["queue"] else {}

    monkeypatch.setattr("werkbank.llm_lane.complete_structured", fake)
    return box


def _doc_fact() -> Fact:
    return Fact(
        id="st1.f1", claim="Die Kündigungsfrist beträgt sechs Wochen.",
        evidence=Evidence.QUOTE,
        sources=[Source(id="s1", type="paperless", trust=SourceTrust.AUTHORITATIVE,
                        ref="doc:12#p1", quote="Kündigungsfrist: sechs Wochen")],
    )


def _note_fact() -> Fact:
    return Fact(
        id="st2.f1", claim="Die Kündigungsfrist beträgt drei Monate.",
        evidence=Evidence.QUOTE,
        sources=[Source(id="s2", type="vault", trust=SourceTrust.USER_ASSERTED,
                        ref="Notizen/Miete.md", quote="Kündigungsfrist drei Monate")],
    )


def _state(**kw) -> RunState:
    state = RunState(
        run_id="r1", user_id="alice", model="stub",
        brief=Brief(original_request="Welche Frist gilt?", goal="Frist nennen",
                    acceptance_criteria=["nennt die Frist mit Quelle"]),
        results={
            "st1": SubtaskResult(subtask_id="st1", agent="doc_researcher",
                                 question="Was steht im Vertrag?",
                                 status=SubtaskStatus.OK, facts=[_doc_fact()]),
            "st2": SubtaskResult(subtask_id="st2", agent="doc_researcher",
                                 question="Was steht in den Notizen?",
                                 status=SubtaskStatus.OK, facts=[_note_fact()]),
        },
        started_at="2026-08-17T09:00:00", finished_at="2026-08-17T09:05:00",
    )
    for key, value in kw.items():
        setattr(state, key, value)
    return state


# ── Contradictions ────────────────────────────────────────────────────────────


def test_a_note_contradicting_a_document_is_recorded_on_both_facts(ctx, replies):
    """The constructed case from the tasks document."""
    state = _state()
    replies["queue"] = [{"pairs": [{"fact_a": "st1.f1", "fact_b": "st2.f1",
                                    "nature": "andere Dauer", "note": "6 Wochen vs. 3 Monate"}]}]
    pairs = _run(contradictions.find(state, ctx))
    assert len(pairs) == 1
    assert state.fact_by_id("st1.f1").contradicts == ["st2.f1"]
    assert state.fact_by_id("st2.f1").contradicts == ["st1.f1"]


def test_the_document_versus_note_pair_is_singled_out(ctx, replies):
    state = _state()
    replies["queue"] = [{"pairs": [{"fact_a": "st2.f1", "fact_b": "st1.f1"}]}]
    _run(contradictions.find(state, ctx))
    conflicts = contradictions.trust_conflicts(state)
    assert len(conflicts) == 1
    document, note = conflicts[0]
    assert document.id == "st1.f1" and note.id == "st2.f1"


def test_a_pair_naming_a_fact_that_does_not_exist_is_dropped(ctx, replies):
    state = _state()
    replies["queue"] = [{"pairs": [{"fact_a": "st1.f1", "fact_b": "st9.f9"}]}]
    assert _run(contradictions.find(state, ctx)) == []
    assert state.fact_by_id("st1.f1").contradicts == []


def test_the_checker_is_shown_the_trust_of_every_fact(ctx, replies):
    """Without it the interesting pair looks like any other disagreement."""
    state = _state()
    replies["queue"] = [{"pairs": []}]
    _run(contradictions.find(state, ctx))
    prompt = replies["calls"][0]["messages"][0]["content"]
    assert "trust: authoritative" in prompt and "trust: user_asserted" in prompt


def test_a_failing_contradiction_pass_costs_no_run(ctx, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("model down")

    monkeypatch.setattr("werkbank.llm_lane.complete_structured", boom)
    assert _run(contradictions.find(_state(), ctx)) == []


# ── The generated reflection ──────────────────────────────────────────────────


def test_an_unresolved_subtask_is_named(ctx):
    state = _state()
    state.results["st3"] = SubtaskResult(
        subtask_id="st3", agent="web_researcher",
        question="Gibt es vergleichbare Angebote?",
        status=SubtaskStatus.UNRESOLVABLE)
    block = reflection.build(state)
    assert "st3" in block and "Gibt es vergleichbare Angebote?" in block


def test_an_open_gap_is_named_with_its_reason(ctx):
    state = _state()
    state.results["st1"].gaps = [Gap(question="Gibt es eine Nebenabrede?",
                                     reason=GapReason.NOT_FOUND)]
    block = reflection.build(state)
    assert "Nebenabrede" in block and "not_found" in block


def test_a_claim_without_a_source_is_named(ctx):
    state = _state()
    state.results["st1"].facts.append(Fact(
        id="st1.f2", claim="Solche Verträge laufen meist drei Jahre.",
        evidence=Evidence.MODEL_KNOWLEDGE))
    block = reflection.build(state)
    assert "Modellwissen" in block and "st1.f2" in block


def test_a_subtask_at_the_revision_cap_is_marked(ctx):
    state = _state(capped_subtasks=["st1"])
    state.results["st1"].status = SubtaskStatus.UNRESOLVABLE
    assert "Revisionslimit" in reflection.build(state)


def test_an_unmet_criterion_is_named(ctx):
    state = _state()
    state.verdicts["st1"] = CriticVerdict(
        decision=CriticDecision.ACCEPT,
        criteria=[CriterionCheck(criterion="nennt die Frist mit Quelle",
                                 verdict=CriterionVerdict.PARTIAL, fact_ids=["st1.f1"])])
    block = reflection.build(state)
    assert "nennt die Frist mit Quelle" in block and "partial" in block


def test_the_source_distribution_is_shown(ctx):
    block = reflection.build(_state())
    assert "authoritative" in block and "user_asserted" in block


def test_a_clean_run_says_so_rather_than_inventing_a_caveat(ctx):
    state = RunState(run_id="r", user_id="alice",
                     results={"st1": SubtaskResult(subtask_id="st1", status=SubtaskStatus.OK)})
    block = reflection.build(state)
    assert "Alle Teilfragen wurden beantwortet" in block


# ── The writer ────────────────────────────────────────────────────────────────


def _report(markdown: str) -> dict:
    return {"title": "Bericht", "markdown": markdown}


def test_a_shortened_reflection_block_is_restored(ctx, replies):
    """The manipulation test: the block is not the model's to edit."""
    state = _state()
    state.results["st3"] = SubtaskResult(
        subtask_id="st3", agent="doc_researcher", question="Offene Frage?",
        status=SubtaskStatus.UNRESOLVABLE)
    block = reflection.build(state)
    assert "Offene Frage?" in block

    replies["queue"] = [_report(
        "## Bericht\n\nDie Frist beträgt sechs Wochen [st1.f1].\n\n"
        f"{reflection.BEGIN}\n## Selbstreflexion\n\nAlles gut gelaufen.\n{reflection.END}\n"
    )]
    markdown = _run(writer.write_report(state, ctx))
    assert "Alles gut gelaufen" not in markdown
    assert "Offene Frage?" in markdown


def test_a_dropped_reflection_block_is_appended(ctx, replies):
    state = _state()
    replies["queue"] = [_report("## Bericht\n\nDie Frist beträgt sechs Wochen [st1.f1].\n")]
    markdown = _run(writer.write_report(state, ctx))
    assert reflection.BEGIN in markdown and reflection.END in markdown


def test_an_untouched_block_is_left_alone(ctx, replies):
    state = _state()
    block = reflection.build(state)
    replies["queue"] = [_report(
        f"## Bericht\n\nDie Frist beträgt sechs Wochen [st1.f1].\n\n{block}\n")]
    markdown = _run(writer.write_report(state, ctx))
    assert markdown.count(reflection.BEGIN) == 1


def test_a_marker_pointing_at_nothing_is_stripped(ctx, replies):
    state = _state()
    replies["queue"] = [_report(
        f"## Bericht\n\nDie Frist ist klar [st1.f1] und lang [st9.f9].\n\n"
        f"{reflection.build(state)}\n")]
    markdown = _run(writer.write_report(state, ctx))
    assert "[st9.f9]" not in markdown and "[st1.f1]" in markdown


def test_a_paragraph_without_evidence_is_flagged_in_the_report_itself(ctx, replies):
    """The smuggled paragraph from the acceptance criteria."""
    state = _state()
    replies["queue"] = [_report(
        f"## Bericht\n\nDie Frist beträgt sechs Wochen [st1.f1].\n\n"
        f"Insgesamt wirkt der Vertrag ausgewogen und fair.\n\n"
        f"{reflection.build(state)}\n")]
    markdown = _run(writer.write_report(state, ctx))
    assert "Absätze ohne Fact-Beleg" in markdown
    assert "ausgewogen" in markdown.split("Absätze ohne Fact-Beleg")[1][:300]


def test_the_source_list_separates_the_trust_levels(ctx, replies):
    state = _state()
    replies["queue"] = [_report(
        f"## Bericht\n\nsechs Wochen [st1.f1], drei Monate [st2.f1].\n\n"
        f"{reflection.build(state)}\n")]
    markdown = _run(writer.write_report(state, ctx))
    assert "Dokumente (authoritative)" in markdown
    assert "Eigene Notizen, Mail, Kalender (user_asserted)" in markdown
    assert "doc:12#p1" in markdown and "Notizen/Miete.md" in markdown


def test_the_writer_is_given_the_contradiction_on_the_fact(ctx, replies):
    state = _state()
    state.fact_by_id("st1.f1").contradicts = ["st2.f1"]
    replies["queue"] = [_report(f"## Bericht\n\nx [st1.f1]\n\n{reflection.build(state)}\n")]
    _run(writer.write_report(state, ctx))
    prompt = replies["calls"][0]["messages"][0]["content"]
    assert "widerspricht st2.f1" in prompt


def test_the_writer_gets_the_original_request_verbatim(ctx, replies):
    state = _state()
    replies["queue"] = [_report(f"x [st1.f1]\n\n{reflection.build(state)}\n")]
    _run(writer.write_report(state, ctx))
    assert "Welche Frist gilt?" in replies["calls"][0]["messages"][0]["content"]


def test_holes_are_detectable_without_reading_the_prose():
    state = _state()
    assert not writer.has_unreported_holes(state)
    state.results["st1"].gaps = [Gap(question="?", reason=GapReason.NOT_FOUND)]
    assert writer.has_unreported_holes(state)
