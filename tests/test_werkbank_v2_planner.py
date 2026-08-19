"""Werkbank v2 — registry, plan validation, planner, plan critic.

The rule this file exists to defend: **an agent whose source the user has not
configured does not exist for that run.** Everything else follows from it — a
planner that can see `comms_researcher` will assign it, the run will find no
mail tool, and the model will answer from parametric knowledge instead of
recording a gap. That is the failure mode, and it is prevented by filtering,
not by asking nicely in a prompt.
"""

import asyncio

import pytest

from werkbank.v2 import llm, plan_checks, planner, registry
from werkbank.v2.models import (
    Brief,
    CoverageCheck,
    CoverageVerdict,
    DepthBudget,
    Subtask,
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def ctx(tmp_path):
    return llm.LLMContext(model="stub", user_id="alice", run_id="r1", log_dir=tmp_path)


@pytest.fixture
def replies(monkeypatch):
    box = {"queue": [], "calls": []}

    async def fake(system, messages, **kw):
        box["calls"].append({"system": system, "messages": messages, "tool": kw.get("tool_name")})
        return box["queue"].pop(0) if box["queue"] else {}

    monkeypatch.setattr("werkbank.llm_lane.complete_structured", fake)
    return box


def _brief(**kw) -> Brief:
    base = dict(
        original_request="Welche Fristen stehen in meinen Verträgen?",
        goal="Alle Fristen benennen",
        acceptance_criteria=["nennt jede Frist mit Datum und Quelldokument"],
        depth_budget=DepthBudget.STANDARD,
    )
    base.update(kw)
    return Brief(**base)


def _sub(sid, agent="doc_researcher", **kw) -> Subtask:
    base = dict(
        subtask_id=sid, question=f"Frage {sid}", agent=agent,
        acceptance_criteria=["nennt die Frist"], covers_criteria=[0],
    )
    base.update(kw)
    return Subtask(**base)


# ── Registry ──────────────────────────────────────────────────────────────────


def test_the_five_default_archetypes_exist():
    assert set(registry.load_defaults().agents) == {
        "doc_researcher", "web_researcher", "comms_researcher",
        "synthesizer", "contradiction_checker",
    }


def test_no_agent_can_write_anything():
    """A research run must not change the user's data — not even by accident."""
    reg = registry.load_defaults()
    writes = {"create_note", "create_deadline", "remember_fact",
              "update_brain_fact", "delete_brain_fact", "create_kanban_task"}
    for spec in reg.agents.values():
        assert not (set(spec.tools) & writes), spec.id


def test_no_agent_gets_a_tool_that_needs_a_browser_dialog():
    reg = registry.load_defaults()
    dialogs = {"trigger_docx_generation", "create_email", "generate_chat_pdf"}
    for spec in reg.agents.values():
        assert not (set(spec.tools) & dialogs), spec.id


def test_an_unconfigured_source_removes_its_agent():
    """The acceptance criterion from the tasks document, as a test."""
    reg = registry.available_agents({"paperless", "vault"})
    assert "comms_researcher" not in reg.agents
    assert "web_researcher" not in reg.agents
    assert "doc_researcher" in reg.agents


def test_configuring_mail_brings_the_agent_back():
    reg = registry.available_agents({"paperless", "vault", "mail"})
    assert "comms_researcher" in reg.agents


def test_fact_only_agents_survive_every_configuration():
    """They need no source of their own — their input is other subtasks' facts."""
    reg = registry.available_agents(set())
    assert {"synthesizer", "contradiction_checker"} <= set(reg.agents)


def test_trust_is_attached_to_the_tool_not_the_store():
    """Same document, different tool: the wording is authoritative, the ingest
    summary is a model's paraphrase of it."""
    reg = registry.load_defaults()
    assert reg.trust_for("get_document_page_text") == "authoritative"
    assert reg.trust_for("get_document_details") == "derived"
    assert reg.trust_for("vault_search") == "user_asserted"
    assert reg.trust_for("web_search") == "external"


def test_an_unknown_tool_is_worth_nothing():
    assert registry.load_defaults().trust_for("some_new_tool") == "model"


def test_a_user_archetype_cannot_grant_itself_a_forbidden_tool():
    """User archetypes stay editable; the write ban is not theirs to lift."""
    spec = registry.AgentSpec(id="mine", label="Mine",
                              tools=["search", "remember_fact"], user_defined=True)
    reg = registry.available_agents({"paperless", "vault"}, user_agents=[spec])
    assert reg.agents["mine"].tools == ["search"]


def test_a_default_can_always_be_recovered():
    """What was missing in v1: a broken default prompt had no way back."""
    original = registry.default_spec("doc_researcher")
    assert original and "search" in original.tools

    edited = registry.AgentSpec(id="doc_researcher", label="x", tools=["calculate"])
    assert registry.diverges_from_default(edited)
    assert not registry.diverges_from_default(original)


def test_a_user_defined_agent_has_no_default_to_return_to():
    assert registry.default_spec("mine") is None
    assert not registry.is_default("mine")


# ── Plan validation ───────────────────────────────────────────────────────────


AGENTS = {"doc_researcher", "web_researcher", "synthesizer", "contradiction_checker"}


def test_a_sound_plan_passes():
    plan = [_sub("st1"), _sub("st2", "synthesizer", depends_on=["st1"])]
    assert plan_checks.validate_plan(plan, _brief(), AGENTS).ok


def test_a_cycle_is_rejected():
    plan = [_sub("st1", depends_on=["st2"]), _sub("st2", depends_on=["st1"])]
    report = plan_checks.validate_plan(plan, _brief(), AGENTS)
    assert not report.ok and any("cycle" in d for d in report.defects)


def test_an_unavailable_agent_is_rejected():
    plan = [_sub("st1", "comms_researcher")]
    report = plan_checks.validate_plan(plan, _brief(), AGENTS)
    assert not report.ok and any("not available" in d for d in report.defects)


def test_a_dependency_on_a_subtask_that_does_not_exist_is_rejected():
    report = plan_checks.validate_plan([_sub("st1", depends_on=["st7"])], _brief(), AGENTS)
    assert not report.ok and any("st7" in d for d in report.defects)


def test_more_subtasks_than_the_budget_allows_is_rejected():
    plan = [_sub(f"st{i}") for i in range(1, 5)]
    report = plan_checks.validate_plan(plan, _brief(depth_budget=DepthBudget.QUICK), AGENTS)
    assert not report.ok and any("budget" in d for d in report.defects)


def test_an_uncovered_brief_criterion_is_rejected():
    brief = _brief(acceptance_criteria=["nennt jede Frist", "unterscheidet die Fristarten"])
    report = plan_checks.validate_plan([_sub("st1", covers_criteria=[0])], brief, AGENTS)
    assert not report.ok and any("criterion 1" in d for d in report.defects)


def test_a_subtask_without_its_own_criteria_is_rejected():
    plan = [_sub("st1", acceptance_criteria=[])]
    report = plan_checks.validate_plan(plan, _brief(), AGENTS)
    assert not report.ok and any("acceptance_criteria" in d for d in report.defects)


def test_a_fact_only_agent_without_dependencies_is_rejected():
    """A synthesizer with nothing to synthesize would have to invent."""
    plan = [_sub("st1"), _sub("st2", "synthesizer")]
    report = plan_checks.validate_plan(plan, _brief(), AGENTS)
    assert not report.ok and any("must depend" in d for d in report.defects)


def test_duplicate_subtask_ids_are_rejected():
    report = plan_checks.validate_plan([_sub("st1"), _sub("st1")], _brief(), AGENTS)
    assert not report.ok and any("not unique" in d for d in report.defects)


# ── The contradiction pass is appended, not requested ─────────────────────────


def test_the_contradiction_checker_is_appended_when_the_planner_forgets_it():
    plan = plan_checks.ensure_contradiction_checker([_sub("st1"), _sub("st2")])
    checkers = [s for s in plan if s.agent == "contradiction_checker"]
    assert len(checkers) == 1
    assert set(checkers[0].depends_on) == {"st1", "st2"}


def test_it_depends_on_the_leaves_not_on_everything():
    plan = plan_checks.ensure_contradiction_checker(
        [_sub("st1"), _sub("st2", depends_on=["st1"])]
    )
    checker = next(s for s in plan if s.agent == "contradiction_checker")
    assert checker.depends_on == ["st2"]


def test_an_existing_checker_is_reused_and_its_dependencies_corrected():
    plan = plan_checks.ensure_contradiction_checker([
        _sub("st1"), _sub("st2"),
        _sub("st3", "contradiction_checker", depends_on=["st1"]),
    ])
    checkers = [s for s in plan if s.agent == "contradiction_checker"]
    assert len(checkers) == 1
    assert set(checkers[0].depends_on) == {"st1", "st2"}


# ── Execution levels ──────────────────────────────────────────────────────────


def test_independent_subtasks_share_a_level():
    levels = plan_checks.topological_order(
        [_sub("st1"), _sub("st2"), _sub("st3", depends_on=["st1", "st2"])]
    )
    assert levels == [["st1", "st2"], ["st3"]]


# ── Planner ───────────────────────────────────────────────────────────────────


def _plan_payload(*subtasks) -> dict:
    return {"subtasks": [s.model_dump() for s in subtasks]}


def test_the_planner_only_ever_sees_the_available_agents(ctx, replies):
    reg = registry.available_agents({"paperless", "vault"})
    replies["queue"] = [_plan_payload(_sub("st1"))]
    _run(planner.build_plan(_brief(), reg, ctx))
    prompt = replies["calls"][0]["messages"][0]["content"]
    assert "doc_researcher" in prompt
    assert "comms_researcher" not in prompt
    assert "web_researcher" not in prompt


def test_a_defective_plan_triggers_exactly_one_replan(ctx, replies):
    reg = registry.available_agents({"paperless", "vault"})
    replies["queue"] = [
        _plan_payload(_sub("st1", depends_on=["st9"])),   # unknown dependency
        _plan_payload(_sub("st1")),
    ]
    subtasks, report = _run(planner.build_plan(_brief(), reg, ctx))
    assert report.ok
    assert len(replies["calls"]) == 2
    assert "st9" in replies["calls"][1]["messages"][0]["content"]


def test_a_plan_that_stays_broken_is_returned_with_its_defects(ctx, replies):
    reg = registry.available_agents({"paperless", "vault"})
    replies["queue"] = [_plan_payload(_sub("st1", depends_on=["st9"]))] * 2
    _subtasks, report = _run(planner.build_plan(_brief(), reg, ctx))
    assert not report.ok and report.defects


def test_quick_depth_skips_the_plan_critic(ctx, replies):
    """Acceptance criterion from the tasks document."""
    reg = registry.available_agents({"paperless", "vault"})
    replies["queue"] = [_plan_payload(_sub("st1"))]
    _subtasks, _report, coverage = _run(
        planner.plan_with_review(_brief(depth_budget=DepthBudget.QUICK), reg, ctx)
    )
    assert coverage == []
    assert [c["tool"] for c in replies["calls"]] == ["planner"]


def test_standard_depth_runs_the_plan_critic(ctx, replies):
    reg = registry.available_agents({"paperless", "vault"})
    replies["queue"] = [
        _plan_payload(_sub("st1")),
        {"criteria": [{"criterion_index": 0, "verdict": "covered", "subtask_ids": ["st1"]}]},
    ]
    _subtasks, _report, coverage = _run(planner.plan_with_review(_brief(), reg, ctx))
    assert [c["tool"] for c in replies["calls"]] == ["planner", "plan_critic"]
    assert coverage[0].verdict is CoverageVerdict.COVERED


def test_an_uncovered_criterion_triggers_a_replan(ctx, replies):
    reg = registry.available_agents({"paperless", "vault"})
    brief = _brief(acceptance_criteria=["nennt jede Frist", "unterscheidet die Fristarten"])
    replies["queue"] = [
        _plan_payload(_sub("st1", covers_criteria=[0, 1])),
        {"criteria": [
            {"criterion_index": 0, "verdict": "covered", "subtask_ids": ["st1"]},
            {"criterion_index": 1, "verdict": "uncovered", "subtask_ids": []},
        ]},
        _plan_payload(_sub("st1", covers_criteria=[0]), _sub("st2", covers_criteria=[1])),
        {"criteria": [
            {"criterion_index": 0, "verdict": "covered", "subtask_ids": ["st1"]},
            {"criterion_index": 1, "verdict": "covered", "subtask_ids": ["st2"]},
        ]},
    ]
    subtasks, report, coverage = _run(planner.plan_with_review(brief, reg, ctx))
    assert report.ok
    assert {s.subtask_id for s in subtasks} >= {"st1", "st2"}
    assert not planner.uncovered_criteria(coverage)


def test_a_covered_verdict_without_evidence_is_downgraded(ctx, replies):
    """A verdict naming no subtask is an opinion, not a judgement."""
    reg = registry.available_agents({"paperless", "vault"})
    replies["queue"] = [
        _plan_payload(_sub("st1")),
        {"criteria": [{"criterion_index": 0, "verdict": "covered", "subtask_ids": []}]},
    ]
    _s, _r, coverage = _run(planner.plan_with_review(_brief(), reg, ctx))
    assert coverage[0].verdict is CoverageVerdict.PARTIAL


def test_the_plan_critic_is_told_which_tools_each_agent_actually_has(ctx, replies):
    """Its job is judging fit between question and source — it needs the source."""
    reg = registry.available_agents({"paperless", "vault"})
    replies["queue"] = [
        _plan_payload(_sub("st1")),
        {"criteria": [{"criterion_index": 0, "verdict": "covered", "subtask_ids": ["st1"]}]},
    ]
    _run(planner.plan_with_review(_brief(), reg, ctx))
    critic_prompt = replies["calls"][1]["messages"][0]["content"]
    assert "agent_tools" in critic_prompt and "get_document_page_text" in critic_prompt


def test_the_planner_prompt_carries_the_comparison_rule():
    """Rule 7 — the AirEx failure: without a definition step the run compares names."""
    text = planner.PLANNER_PROMPT.read_text(encoding="utf-8")
    assert "comparison needs a definition subtask" in text.lower()
