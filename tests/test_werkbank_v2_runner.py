"""Werkbank v2 — tool layer, runner, fact critic, revision loop.

The failures this phase exists to prevent, each with a test:

- a question answered from parametric knowledge with no lookup at all,
- a citation that does not appear in anything a tool returned,
- a critic that approves its own work because it was shown the reasoning,
- a revision loop with no end.

The model is stubbed. That is not a compromise here: the point of the design is
that these outcomes are decided by code, so they must be testable without one.
"""

import asyncio
import pathlib

import pytest

from werkbank.v2 import checks, critic, registry, runner, tools
from werkbank.v2.llm import LLMContext, PromptLog
from werkbank.v2.models import (
    CriticDecision,
    Evidence,
    Fact,
    Source,
    Subtask,
    SubtaskResult,
    SubtaskStatus,
)

PAGE = (
    "Mietvertrag Musterstraße 12\n"
    "Die Kündigungsfrist beträgt drei Monate zum Quartalsende.\n"
)
NOTE = "Kündigungsfrist ist glaube ich sechs Wochen.\n"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def ctx(tmp_path):
    return LLMContext(model="stub", user_id="alice", run_id="r1", log_dir=tmp_path)


@pytest.fixture
def reg():
    return registry.available_agents({"paperless", "vault"})


@pytest.fixture
def tool_results(monkeypatch):
    """What each tool returns, and a log of what was called."""
    box = {"by_tool": {"search": PAGE, "vault_search": NOTE}, "calls": []}

    async def fake_execute(name, args):
        box["calls"].append((name, args))
        return box["by_tool"].get(name, f"(no result from {name})"), [], []

    monkeypatch.setattr("services.chat_service.execute_tool", fake_execute)
    return box


def _one_tool_turn(query="q"):
    """One tool call, then the model is done — per attempt.

    Written as a pair on purpose: a stub that only ever answers `tool_use`
    makes the first attempt loop until the queue is empty, and every later
    attempt then runs with no tools at all.
    """
    return [
        ("looking", [{"id": "t1", "name": "search", "input": {"query": query}}], "tool_use"),
        ("done", [], "end_turn"),
    ]


@pytest.fixture
def model(monkeypatch):
    """Stubs both halves: the tool loop and the structured call."""
    box = {"turns": [], "structured": [], "structured_prompts": []}

    async def fake_turn(system, messages, **kw):
        return box["turns"].pop(0) if box["turns"] else ("done", [], "end_turn")

    async def fake_structured(system, messages, **kw):
        box["structured_prompts"].append({"system": system, "messages": messages,
                                          "tool": kw.get("tool_name")})
        return box["structured"].pop(0) if box["structured"] else {}

    monkeypatch.setattr("werkbank.llm_lane.complete_turn", fake_turn)
    monkeypatch.setattr("werkbank.llm_lane.complete_structured", fake_structured)
    monkeypatch.setattr("werkbank.llm_lane.is_anthropic_backend", lambda *a: False)
    monkeypatch.setattr("werkbank.llm_lane.is_openai_compat_model", lambda *a: True)
    return box


def _subtask(**kw) -> Subtask:
    base = dict(
        subtask_id="st1",
        question="Welche Kündigungsfrist gilt?",
        agent="doc_researcher",
        acceptance_criteria=["nennt die Frist mit Quelldokument"],
        covers_criteria=[0],
    )
    base.update(kw)
    return Subtask(**base)


def _result_payload(**kw) -> dict:
    base = {
        "subtask_id": "st1",
        "facts": [{
            "id": "st1.f1",
            "claim": "Die Kündigungsfrist beträgt drei Monate.",
            "evidence": "quote",
            "sources": [{"id": "s1", "type": "paperless",
                         "quote": "Die Kündigungsfrist beträgt drei Monate zum Quartalsende."}],
        }],
        "narrative": "Die Frist ergibt sich aus dem Vertrag [st1.f1].",
    }
    base.update(kw)
    return base


# ── Tool layer ────────────────────────────────────────────────────────────────


def test_a_document_search_also_searches_the_notes(reg, tool_results):
    """Where contradictions come from — and they only surface if both are read."""
    belt = tools.ToolBelt(registry=reg, allowed_tools=reg.agents["doc_researcher"].tools,
                          user_id="alice", persist=False)
    text = _run(belt.execute("search", {"query": "Kündigungsfrist"}))
    assert [c[0] for c in tool_results["calls"]] == ["search", "vault_search"]
    assert "Notes of the user" in text
    assert belt.call_count == 2


def test_trust_comes_from_the_tool_not_from_the_store(reg, tool_results):
    belt = tools.ToolBelt(registry=reg, allowed_tools=reg.agents["doc_researcher"].tools,
                          user_id="alice", persist=False)
    _run(belt.execute("search", {"query": "x"}))
    trust = belt.trust_by_source()
    assert trust["s1"] == "authoritative"     # the document
    assert trust["s2"] == "user_asserted"     # the note about it


def test_a_restricted_subtask_cannot_reach_the_other_store(reg, tool_results):
    """Enforced before the call — not judged afterwards."""
    belt = tools.ToolBelt(registry=reg, allowed_tools=reg.agents["doc_researcher"].tools,
                          user_id="alice", persist=False, sources_restrict=["paperless"])
    out = _run(belt.execute("vault_search", {"query": "x"}))
    assert "blocked" in out
    assert not tool_results["calls"]
    # and the companion search does not smuggle it back in
    _run(belt.execute("search", {"query": "x"}))
    assert [c[0] for c in tool_results["calls"]] == ["search"]


def test_a_blocked_tool_is_not_even_offered_to_the_model(reg):
    belt = tools.ToolBelt(registry=reg, allowed_tools=reg.agents["doc_researcher"].tools,
                          user_id="alice", persist=False, sources_restrict=["paperless"])
    offered = {d["name"] for d in belt.definitions()}
    assert "vault_search" not in offered and "search" in offered


def test_a_search_result_list_is_not_quotable_for_the_web(reg):
    """The snippet shows a product's name, never its purpose — the AirEx failure."""
    belt = tools.ToolBelt(registry=reg, allowed_tools=["web_search", "web_fetch_page"],
                          user_id="alice", persist=False)
    belt._record("web_search", {"query": "x"}, "snippet text")
    belt._record("web_fetch_page", {"url": "https://e.example"}, "full article text")
    assert set(belt.raw_texts()) == {"s2"}


def test_the_model_cannot_upgrade_its_own_source(reg, tool_results):
    """It may write any trust it likes; the tool log overwrites it."""
    belt = tools.ToolBelt(registry=reg, allowed_tools=["vault_search"],
                          user_id="alice", persist=False)
    _run(belt.execute("vault_search", {"query": "x"}))
    result = SubtaskResult.model_validate({
        "subtask_id": "st1",
        "facts": [{"id": "st1.f1", "claim": "x", "evidence": "quote",
                   "sources": [{"id": "s1", "type": "paperless",
                                "trust": "authoritative", "quote": "Kündigungsfrist"}]}],
    })
    tools.apply_tool_trust(result, belt)
    source = result.facts[0].sources[0]
    assert source.trust.value == "user_asserted" and source.type == "vault"


def test_an_invented_source_id_gets_the_lowest_trust(reg, tool_results):
    belt = tools.ToolBelt(registry=reg, allowed_tools=["search"], user_id="alice", persist=False)
    _run(belt.execute("search", {"query": "x"}))
    result = SubtaskResult.model_validate({
        "subtask_id": "st1",
        "facts": [{"id": "st1.f1", "claim": "x", "evidence": "quote",
                   "sources": [{"id": "s99", "type": "paperless", "quote": "erfunden"}]}],
    })
    tools.apply_tool_trust(result, belt)
    assert result.facts[0].sources[0].trust.value == "model"


# ── Runner ────────────────────────────────────────────────────────────────────


def test_the_runner_fills_the_self_check_from_the_tool_log(ctx, reg, tool_results, model):
    """Never from the model: this is what D5 stands on."""
    model["turns"] = _one_tool_turn("Kündigungsfrist")
    model["structured"] = [_result_payload(self_check={"tool_calls": 99})]
    result, belt = _run(runner.run_subtask(
        _subtask(), reg.agents["doc_researcher"], reg, ctx, persist=False))
    assert result.self_check.tool_calls == belt.call_count == 2
    assert result.model == "stub" and result.agent == "doc_researcher"


def test_a_dependent_subtask_inherits_facts_and_never_prose(ctx, reg, tool_results, model):
    """Provenance survives, and context stays bounded on deep DAGs."""
    from werkbank.v2.models import Fact, Source

    inherited = [Fact(id="st1.f1", claim="Der Vertrag läuft bis 2027.",
                      evidence=Evidence.QUOTE,
                      sources=[Source(id="s1", type="paperless", quote="läuft bis 2027")])]
    model["turns"] = [("done", [], "end_turn")]
    model["structured"] = [_result_payload(subtask_id="st2", facts=[])]
    _run(runner.run_subtask(_subtask(subtask_id="st2"), reg.agents["doc_researcher"],
                            reg, ctx, inherited_facts=inherited, persist=False))
    prompt = model["structured_prompts"][0]["messages"][0]["content"]
    assert "[st1.f1] (quote) Der Vertrag läuft bis 2027." in prompt
    assert "narrative" not in prompt.lower()


def test_a_fact_id_from_the_wrong_subtask_is_repaired(ctx, reg, tool_results, model):
    model["turns"] = [("done", [], "end_turn")]
    model["structured"] = [_result_payload(facts=[{
        "id": "st7.f1", "claim": "x", "evidence": "model_knowledge", "sources": [],
    }])]
    result, _ = _run(runner.run_subtask(_subtask(), reg.agents["doc_researcher"],
                                        reg, ctx, persist=False))
    assert result.facts[0].id == "st1.f1"


# ── Fact critic ───────────────────────────────────────────────────────────────


def test_the_critic_never_sees_the_narrative(ctx):
    """The acceptance criterion of this phase, asserted on the prompt itself."""
    result = SubtaskResult.model_validate(_result_payload())
    text = critic.critic_input(result, "Welche Frist?", {"s1": PAGE})
    assert "Die Frist ergibt sich aus dem Vertrag" not in text
    assert "Die Kündigungsfrist beträgt drei Monate." in text     # the claim, yes
    assert PAGE[:30] in text                                      # the source, yes


def test_a_criterion_called_met_without_a_fact_is_unmet(ctx):
    from werkbank.v2.models import CriterionCheck, CriterionVerdict, CriticVerdict

    result = SubtaskResult.model_validate(_result_payload())
    verdict = critic.enforce_evidence(
        CriticVerdict(decision=CriticDecision.ACCEPT, criteria=[
            CriterionCheck(criterion="nennt die Frist", verdict=CriterionVerdict.MET,
                           fact_ids=[]),
        ]),
        result,
    )
    assert verdict.criteria[0].verdict is CriterionVerdict.UNMET


def test_a_criterion_citing_a_fact_that_does_not_exist_is_unmet(ctx):
    from werkbank.v2.models import CriterionCheck, CriterionVerdict, CriticVerdict

    result = SubtaskResult.model_validate(_result_payload())
    verdict = critic.enforce_evidence(
        CriticVerdict(decision=CriticDecision.ACCEPT, criteria=[
            CriterionCheck(criterion="x", verdict=CriterionVerdict.MET,
                           fact_ids=["st1.f99"]),
        ]),
        result,
    )
    assert verdict.criteria[0].verdict is CriterionVerdict.UNMET


# ── The loop ──────────────────────────────────────────────────────────────────


def _accept() -> dict:
    return {"decision": "accept", "criteria": [
        {"criterion": "nennt die Frist mit Quelldokument", "verdict": "met",
         "fact_ids": ["st1.f1"]}]}


def test_a_clean_subtask_is_accepted_after_one_pass(ctx, reg, tool_results, model):
    model["turns"] = _one_tool_turn()
    model["structured"] = [_result_payload(), _accept()]
    result, verdict, capped = _run(critic.run_with_review(
        _subtask(), reg.agents["doc_researcher"], reg, ctx,
        original_request="Welche Frist?", max_revisions=1, persist=False))
    assert verdict.decision is CriticDecision.ACCEPT and not capped
    assert result.status is SubtaskStatus.OK


def test_an_answer_with_no_tool_call_is_sent_back(ctx, reg, tool_results, model):
    """D5: tools were there, none was used — so the answer came from memory."""
    model["turns"] = [("I know this", [], "end_turn"), ("I know this", [], "end_turn")]
    model["structured"] = [
        _result_payload(facts=[{"id": "st1.f1", "claim": "Drei Monate.",
                                "evidence": "model_knowledge", "sources": []}]),
        _accept(),
        _result_payload(facts=[{"id": "st1.f1", "claim": "Drei Monate.",
                                "evidence": "model_knowledge", "sources": []}]),
        _accept(),
    ]
    _result, verdict, capped = _run(critic.run_with_review(
        _subtask(), reg.agents["doc_researcher"], reg, ctx,
        original_request="Welche Frist?", max_revisions=1, persist=False))
    assert capped is True
    assert verdict.decision is not CriticDecision.ACCEPT


def test_a_fabricated_quote_cannot_be_rescued_by_the_critic(ctx, reg, tool_results, model):
    """The checks outrank the model: it may not approve what they threw out."""
    model["turns"] = _one_tool_turn() * 3
    invented = _result_payload(facts=[{
        "id": "st1.f1", "claim": "Die Frist beträgt zwölf Monate.", "evidence": "quote",
        "sources": [{"id": "s1", "type": "paperless",
                     "quote": "Die Kündigungsfrist beträgt zwölf Monate."}],
    }], narrative="")
    model["structured"] = [invented, _accept(), invented, _accept()]
    result, verdict, capped = _run(critic.run_with_review(
        _subtask(), reg.agents["doc_researcher"], reg, ctx,
        original_request="Welche Frist?", max_revisions=1, persist=False))
    assert not result.facts
    assert result.status is SubtaskStatus.UNRESOLVABLE
    assert verdict.decision is not CriticDecision.ACCEPT


def test_an_unanswerable_question_ends_unresolvable_with_a_gap(ctx, reg, tool_results, model):
    """The negative test from the tasks document: zero invented facts."""
    tool_results["by_tool"] = {"search": "Keine Treffer.", "vault_search": "Keine Treffer."}
    model["turns"] = _one_tool_turn("Mietvertrag 99")
    model["structured"] = [{
        "subtask_id": "st1", "facts": [],
        "gaps": [{"question": "Welche Frist steht in Dokument X?", "reason": "not_found"}],
    }, {"decision": "unresolvable", "criteria": [], "defects": ["nichts gefunden"]}]
    result, verdict, _capped = _run(critic.run_with_review(
        _subtask(question="Welche Frist steht in ›Mietvertrag Musterstraße 99‹?"),
        reg.agents["doc_researcher"], reg, ctx,
        original_request="…", max_revisions=1, persist=False))
    assert result.facts == []
    assert result.gaps and result.gaps[0].reason.value == "not_found"
    assert result.status is SubtaskStatus.UNRESOLVABLE
    assert verdict.decision is CriticDecision.UNRESOLVABLE


def test_the_revision_cap_ends_the_loop(ctx, reg, tool_results, model):
    """Without a cap there is ping-pong, and the report can never say 'no'."""
    revise = {"decision": "revise", "criteria": [], "defects": ["Beleg fehlt"]}
    model["turns"] = _one_tool_turn() * 5
    model["structured"] = [_result_payload(), revise] * 5
    result, _verdict, capped = _run(critic.run_with_review(
        _subtask(), reg.agents["doc_researcher"], reg, ctx,
        original_request="x", max_revisions=2, persist=False))
    assert capped is True
    # Facts that passed the deterministic checks survive the cap: the critic
    # wanting *more* is not a reason to discard what is already verified.
    assert result.status is SubtaskStatus.PARTIAL
    assert result.facts
    # 3 attempts = 1 initial + 2 revisions, each a runner call and a critic call
    assert len([p for p in model["structured_prompts"] if p["tool"] == "runner"]) == 3


def test_the_defects_are_handed_to_the_next_attempt(ctx, reg, tool_results, model):
    revise = {"decision": "revise", "criteria": [],
              "defects": ["st1.f1 zitiert nicht den Vertrag, sondern die Notiz"]}
    model["turns"] = _one_tool_turn() * 3
    model["structured"] = [_result_payload(), revise, _result_payload(), _accept()]
    _run(critic.run_with_review(
        _subtask(), reg.agents["doc_researcher"], reg, ctx,
        original_request="x", max_revisions=1, persist=False))
    second_runner = [p for p in model["structured_prompts"] if p["tool"] == "runner"][1]
    assert "zitiert nicht den Vertrag" in second_runner["messages"][0]["content"]


def test_an_accepted_subtask_with_an_open_gap_is_partial_not_ok(ctx, reg, tool_results, model):
    model["turns"] = _one_tool_turn()
    model["structured"] = [
        _result_payload(gaps=[{"question": "Nebenabrede?", "reason": "not_found"}]),
        _accept(),
    ]
    result, _v, _c = _run(critic.run_with_review(
        _subtask(), reg.agents["doc_researcher"], reg, ctx,
        original_request="x", max_revisions=1, persist=False))
    assert result.status is SubtaskStatus.PARTIAL


# ── Budget and dead sources ───────────────────────────────────────────────────


def test_a_tool_stops_answering_once_its_budget_is_spent(reg, tool_results):
    """A real run fired 46 web searches on one subtask — looping on a query that
    returned nothing, and throttling the search host in the process."""
    belt = tools.ToolBelt(registry=reg, allowed_tools=["web_search"],
                          user_id="alice", persist=False, max_calls_per_tool=3)

    outs = [_run(belt.execute("web_search", {"query": f"q{i}"})) for i in range(5)]

    assert len(tool_results["calls"]) == 3
    assert "limit" in outs[3] and "gap" in outs[3]
    assert belt.exhausted_tools() == ["web_search"]


def test_a_source_that_never_answers_is_reported_as_unavailable(reg, tool_results):
    """A throttled search host answers HTTP 200 with zero results, so "the web
    has nothing" and "the search refused me" reach the model as one string.
    Recording that as `not_found` turns an outage into a finding."""
    from werkbank.v2.models import Gap, GapReason
    from werkbank.v2.runner import _mark_dead_sources

    belt = tools.ToolBelt(registry=reg, allowed_tools=["web_search"],
                          user_id="alice", persist=False)
    for i in range(3):
        belt._record("web_search", {"query": f"q{i}"}, "No results found.")

    result = SubtaskResult(
        subtask_id="st1", agent="web_researcher",
        gaps=[Gap(question="Gibt es Vorfälle?", reason=GapReason.NOT_FOUND)],
    )
    _mark_dead_sources(result, belt)

    assert result.gaps[0].reason is GapReason.SOURCE_UNAVAILABLE
    assert "web_search" in result.gaps[0].note


def test_a_source_that_answered_at_least_once_is_not_called_dead(reg):
    belt = tools.ToolBelt(registry=reg, allowed_tools=["web_search"],
                          user_id="alice", persist=False)
    belt._record("web_search", {"query": "a"}, "No results found.")
    belt._record("web_search", {"query": "b"}, "1. [Ein Treffer](https://e.example)")
    belt._record("web_search", {"query": "c"}, "No results found.")

    assert belt.dead_tools() == []


def test_the_cap_discards_nothing_that_survived_the_checks(ctx, reg, tool_results, model):
    """The failure this fixes: a subtask produced five quoted facts, the critic
    asked for a sixth, the cap hit — and all five were reported as unanswered,
    leaving the synthesizer with nothing to build on."""
    revise = {"decision": "revise", "criteria": [],
              "defects": ["Sendeleistung fehlt"]}
    model["turns"] = _one_tool_turn() * 4
    model["structured"] = [_result_payload(), revise] * 4

    result, _verdict, capped = _run(critic.run_with_review(
        _subtask(), reg.agents["doc_researcher"], reg, ctx,
        original_request="x", max_revisions=1, persist=False))

    assert capped is True
    assert result.facts                       # kept
    assert result.status is SubtaskStatus.PARTIAL


def test_an_agent_without_retrieval_tools_is_not_accused_of_guessing(ctx, reg, model):
    """D5 asks "tools were there and none was called?". The synthesizer's only
    tools are calculate and get_current_date — it is *designed* to work from the
    facts it inherits, so firing D5 at it marked every synthesis as invented."""
    from werkbank.v2.tools import has_retrieval

    assert not has_retrieval(reg.agents["synthesizer"].tools)
    assert not has_retrieval(reg.agents["contradiction_checker"].tools)
    assert has_retrieval(reg.agents["doc_researcher"].tools)


def test_the_critic_is_told_the_source_text_is_only_an_excerpt():
    """It sent a subtask back with "the quotes are not verifiable in the
    provided source text" — for quotes D2 had already matched against the full
    page. The excerpt starts at the top; the quote was further down."""
    from werkbank.v2 import critic as critic_mod

    text = critic_mod.critic_input(
        SubtaskResult(subtask_id="st1", agent="web_researcher",
                      question="Q", acceptance_criteria=["c"]),
        "original", {"s1": "x" * 5000},
    )

    assert "excerpt" in text.lower()
    assert "5000" in text                      # it is told what it is missing
    assert "already matched" in text

    root = pathlib.Path(__file__).resolve().parent.parent
    prompt = (root / "werkbank" / "v2" / "prompts" / "fact_critic.md").read_text(
        encoding="utf-8"
    )
    assert "already been done, in code" in prompt


def test_the_same_search_is_not_run_twice(reg, tool_results):
    """It returns the same nothing, and costs the search host a request it may
    answer with a CAPTCHA next time."""
    belt = tools.ToolBelt(registry=reg, allowed_tools=["web_search"],
                          user_id="alice", persist=False)

    _run(belt.execute("web_search", {"query": "radar burn"}))
    second = _run(belt.execute("web_search", {"query": "radar burn"}))

    assert len(tool_results["calls"]) == 1
    assert "already ran this exact" in second


def test_a_revision_is_not_charged_for_the_previous_attempts_searches(reg, tool_results):
    """The starvation this fixes: a subtask spent its twelve searches in the
    first attempts, so the *last* attempt — the one whose result counts — could
    not retrieve anything and was reported as unanswered, although twelve pages
    had been fetched along the way."""
    belt = tools.ToolBelt(
        registry=reg, allowed_tools=["web_search"], user_id="alice", persist=False,
        max_calls_per_tool=3,
        prior_queries={tools.ToolBelt.query_key("web_search", {"query": "old"})},
    )

    for i in range(3):
        assert "limit" not in _run(belt.execute("web_search", {"query": f"new{i}"}))
    assert len(tool_results["calls"]) == 3


def test_a_query_from_an_earlier_revision_is_not_run_again(reg, tool_results):
    """What actually protects the search host: the second attempt repeating the
    first one's twelve searches is how one subtask made 36 requests at one host
    and got every engine CAPTCHA'd."""
    belt = tools.ToolBelt(
        registry=reg, allowed_tools=["web_search"], user_id="alice", persist=False,
        prior_queries={tools.ToolBelt.query_key("web_search", {"query": "radar burn"})},
    )

    out = _run(belt.execute("web_search", {"query": "radar burn"}))

    assert "already ran this exact" in out
    assert not tool_results["calls"]


def test_a_revision_that_came_back_empty_does_not_erase_the_better_attempt(
    ctx, reg, tool_results, model
):
    """Observed: the first attempts fetched twelve pages and produced facts, the
    last one retrieved nothing, and the last one was reported — as unanswered."""
    revise = {"decision": "revise", "criteria": [], "defects": ["mehr Details"]}
    empty_result = {"subtask_id": "st1", "status": "ok", "facts": [], "gaps": [],
                    "narrative": ""}
    model["turns"] = _one_tool_turn() * 6
    model["structured"] = [
        _result_payload(), revise,          # attempt 0: has facts
        empty_result, revise,               # attempt 1: nothing
        empty_result, revise,               # attempt 2: nothing → cap
    ]

    result, _verdict, capped = _run(critic.run_with_review(
        _subtask(), reg.agents["doc_researcher"], reg, ctx,
        original_request="x", max_revisions=2, persist=False))

    assert result.facts                       # the good attempt survived
    assert result.status is SubtaskStatus.PARTIAL


def test_a_subtask_with_neither_facts_nor_gaps_gets_another_attempt(
    ctx, reg, tool_results, model
):
    """D8 ("nothing survived and nothing was declared missing") is a defect the
    agent can act on, not a property of the question. One subtask made seventeen
    tool calls, produced no facts and no gaps, and was closed as unanswered on
    the first attempt without ever being asked again."""
    empty = {"subtask_id": "st1", "status": "ok", "facts": [], "gaps": [],
             "narrative": ""}
    accept = {"decision": "accept", "criteria": []}
    model["turns"] = _one_tool_turn() * 4
    model["structured"] = [empty, _result_payload(), accept]

    result, verdict, _capped = _run(critic.run_with_review(
        _subtask(), reg.agents["doc_researcher"], reg, ctx,
        original_request="x", max_revisions=2, persist=False))

    # Two runner calls: the empty first attempt and the retry that found something.
    assert len([p for p in model["structured_prompts"] if p["tool"] == "runner"]) == 2
    assert result.facts
    assert verdict.decision is CriticDecision.ACCEPT


def test_the_last_attempt_still_ends_the_subtask(ctx, reg, tool_results, model):
    """Retrying is bounded: without that, "neither facts nor gaps" is a loop."""
    empty = {"subtask_id": "st1", "status": "ok", "facts": [], "gaps": [],
             "narrative": ""}
    model["turns"] = _one_tool_turn() * 4
    model["structured"] = [empty] * 4

    result, verdict, _capped = _run(critic.run_with_review(
        _subtask(), reg.agents["doc_researcher"], reg, ctx,
        original_request="x", max_revisions=1, persist=False))

    assert len([p for p in model["structured_prompts"] if p["tool"] == "runner"]) == 2
    assert result.status is SubtaskStatus.UNRESOLVABLE
    assert verdict.decision is CriticDecision.UNRESOLVABLE


def test_unresolvable_without_a_single_tool_call_is_sent_back(
    ctx, reg, tool_results, model
):
    """Observed: an agent declared "no web search tool is available in this
    environment", made zero calls and filed four `source_unavailable` gaps —
    while a sibling subtask made forty-four web calls in the same run with the
    same tools. Not calling a tool establishes nothing about the question."""
    no_tools_turn = [("Kein Werkzeug verfügbar.", [], "end_turn")]
    gave_up = {"subtask_id": "st1", "status": "unresolvable", "facts": [],
               "gaps": [{"question": "Gibt es Vorfälle?",
                         "reason": "source_unavailable"}],
               "narrative": ""}
    unresolvable = {"decision": "unresolvable", "criteria": [],
                    "defects": ["keine Fakten übergeben"]}
    accept = {"decision": "accept", "criteria": []}

    model["turns"] = no_tools_turn + _one_tool_turn() * 3
    model["structured"] = [gave_up, unresolvable, _result_payload(), accept]

    result, verdict, _capped = _run(critic.run_with_review(
        _subtask(), reg.agents["doc_researcher"], reg, ctx,
        original_request="x", max_revisions=2, persist=False))

    runner_prompts = [p for p in model["structured_prompts"] if p["tool"] == "runner"]
    assert len(runner_prompts) == 2                     # it was asked again
    assert "did not call a single tool" in runner_prompts[1]["messages"][0]["content"]
    assert result.facts
    assert verdict.decision is CriticDecision.ACCEPT


def test_an_agent_with_no_retrieval_tools_is_still_allowed_to_give_up(
    ctx, reg, tool_results, model
):
    """The synthesizer has no tools to call, so "zero calls" says nothing about
    it — retrying it would just burn the budget."""
    gave_up = {"subtask_id": "st1", "status": "unresolvable", "facts": [],
               "gaps": [{"question": "?", "reason": "not_found"}], "narrative": ""}
    unresolvable = {"decision": "unresolvable", "criteria": [], "defects": ["nichts"]}
    model["turns"] = [("nichts zu tun", [], "end_turn")] * 3
    model["structured"] = [gave_up, unresolvable] * 3

    result, _verdict, _capped = _run(critic.run_with_review(
        _subtask(agent="synthesizer"), reg.agents["synthesizer"], reg, ctx,
        original_request="x", max_revisions=2, persist=False))

    assert len([p for p in model["structured_prompts"] if p["tool"] == "runner"]) == 1
    assert result.status is SubtaskStatus.UNRESOLVABLE


def test_a_model_server_hiccup_does_not_cost_the_subtask(ctx, reg, tool_results, model):
    """One observed run lost a subtask to a single `500 Internal Server Error`
    from Ollama — after it had already made thirteen tool calls."""
    import httpx

    calls = {"n": 0}
    original = model["structured"]

    async def flaky(system, messages, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.HTTPStatusError(
                "500 Internal Server Error", request=httpx.Request("POST", "http://x"),
                response=httpx.Response(500),
            )
        return original.pop(0)

    monkey = pytest.MonkeyPatch()
    monkey.setattr("werkbank.v2.llm.RETRY_BACKOFF_S", 0.0)
    monkey.setattr("werkbank.llm_lane.complete_structured", flaky)
    try:
        model["turns"] = _one_tool_turn() * 3
        model["structured"] = [_result_payload(), {"decision": "accept", "criteria": []}]
        original = model["structured"]
        result, verdict, _ = _run(critic.run_with_review(
            _subtask(), reg.agents["doc_researcher"], reg, ctx,
            original_request="x", max_revisions=1, persist=False))
    finally:
        monkey.undo()

    assert result.facts
    assert verdict.decision is CriticDecision.ACCEPT


def test_a_synthesizer_handed_facts_must_produce_facts(ctx, reg, tool_results, model):
    """Seven quoted facts in, zero facts and five gaps out — so the report said
    nothing although a great deal had been established. Falling short of a
    criterion is a result, and it has to be stated."""
    gave_up = {"subtask_id": "st9", "status": "unresolvable", "facts": [],
               "gaps": [{"question": "Wer genau?", "reason": "not_found"}],
               "narrative": ""}
    unresolvable = {"decision": "unresolvable", "criteria": [], "defects": ["nichts"]}
    accept = {"decision": "accept", "criteria": []}
    derived = {"subtask_id": "st9", "status": "ok", "gaps": [], "narrative": "",
               "facts": [{"id": "st9.f1", "claim": "Die nächstliegenden Fälle sind X und Y.",
                          "evidence": "derived", "derived_from": ["st1.f1"]}]}

    model["turns"] = [("nichts zu tun", [], "end_turn")] * 4
    model["structured"] = [gave_up, unresolvable, derived, accept]

    result, verdict, _capped = _run(critic.run_with_review(
        _subtask(subtask_id="st9", agent="synthesizer"), reg.agents["synthesizer"], reg, ctx,
        original_request="x", max_revisions=2, persist=False,
        inherited_facts=[Fact(id="st1.f1", claim="Ein belegter Fall.",
                              evidence=Evidence.QUOTE,
                              sources=[Source(id="s1", type="web", quote="Ein belegter Fall")])],
        known_fact_ids={"st1.f1"}))

    runner_prompts = [p for p in model["structured_prompts"] if p["tool"] == "runner"]
    assert len(runner_prompts) == 2
    assert "produced none" in runner_prompts[1]["messages"][0]["content"]
    assert result.facts
    assert verdict.decision is CriticDecision.ACCEPT


def test_a_critic_that_cannot_answer_does_not_cost_the_subtask(
    ctx, reg, tool_results, model
):
    """The critic is the second opinion; D1–D9 have already run. One run
    discarded 21 searches and 18 fetched pages because the critic call came back
    without a tool call three times."""
    async def broken_review(*args, **kwargs):
        raise ValueError("OpenAI-compat model did not call tool 'fact_critic'")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(critic, "review", broken_review)
    try:
        model["turns"] = _one_tool_turn() * 2
        model["structured"] = [_result_payload()]
        result, verdict, _ = _run(critic.run_with_review(
            _subtask(), reg.agents["doc_researcher"], reg, ctx,
            original_request="x", max_revisions=1, persist=False))
    finally:
        monkey.undo()

    assert result.facts                          # the research survives
    assert verdict.decision is CriticDecision.ACCEPT
    assert "could not be reached" in verdict.defects[0]


def test_the_hit_count_of_a_search_comes_from_the_call(reg, tool_results):
    """`Source.hits` is stripped from whatever the model wrote and nothing was
    filling it back in, so "I ran this query and it returned nothing" — the one
    honest way to state a negative finding — could never satisfy D3."""
    from werkbank.v2.tools import _hits_from

    assert _hits_from("web_search", "No results found.") == 0
    assert _hits_from("web_search", "1. [A](https://a)\n2. [B](https://b)") == 2
    assert _hits_from("search", "Document #118: Versicherung\nDocument #22: Vertrag") == 2
    assert _hits_from("search", "irgendein Fließtext") is None
    # A tool that does not search has no hit count to state.
    assert _hits_from("web_fetch_page", "1. [A](https://a)\n2. [B](https://b)") is None


def test_a_tool_call_with_typographic_quotes_is_still_read(ctx):
    """Same repair one layer up: when the *whole* tool-call payload is broken by
    a German quote, every fact in it is lost, not just one field."""
    import json

    from werkbank.llm_lane import _json_from_text

    broken = '{"facts": [], "narrative": "Der Titel lautet „Prion" und passt."}'
    parsed = _json_from_text(f"Hier das Ergebnis:\n```json\n{broken}\n```")

    assert parsed is not None
    assert parsed["narrative"].startswith("Der Titel lautet")
    # …and a payload that was fine all along is read unchanged.
    fine = json.dumps({"facts": [], "narrative": "alles gut"})
    assert _json_from_text(fine)["narrative"] == "alles gut"
