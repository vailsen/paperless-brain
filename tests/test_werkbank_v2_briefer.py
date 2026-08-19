"""Werkbank v2 — BRIEFER and the LLM wrapper.

The brief is the contract the whole run is judged against, so the tests here are
about the two things that make it worth anything: the user's own wording
survives untouched, and a criterion nobody could check is sent back rather than
accepted.

No real model. The stub returns whatever the test prescribes, which is what
lets these run in CI and what lets a schema violation be tested at all.
"""

import asyncio
import json

import pytest

from werkbank.v2 import briefer, llm
from werkbank.v2.models import Brief, DepthBudget


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def ctx(tmp_path):
    return llm.LLMContext(model="stub", user_id="alice", run_id="r1", log_dir=tmp_path)


@pytest.fixture
def replies(monkeypatch):
    """Queue of payloads the fake model returns, and a record of the calls."""
    box = {"queue": [], "calls": []}

    async def fake(system, messages, **kw):
        box["calls"].append({"system": system, "messages": messages, **kw})
        return box["queue"].pop(0) if box["queue"] else {}

    monkeypatch.setattr("werkbank.llm_lane.complete_structured", fake)
    return box


def _payload(**kw) -> dict:
    base = {
        "original_request": "…",
        "goal": "Alle Kündigungsfristen der laufenden Verträge benennen",
        "deliverable_format": "Bericht",
        "assumptions": ["'laufend' meint zum heutigen Datum aktive Verträge"],
        "acceptance_criteria": ["nennt jede Frist mit Datum und Quelldokument"],
        "depth_budget": "standard",
    }
    base.update(kw)
    return base


# ── The user's wording survives ───────────────────────────────────────────────


def test_the_original_request_is_restored_even_when_the_model_tidies_it(ctx, replies):
    """Both critics are later held against the user's words, not a paraphrase."""
    raw = "  welche fristen hab ich denn so??  \n\nund was ist mit §4  "
    replies["queue"] = [_payload(original_request="Welche Fristen bestehen? Und §4?")]
    brief = _run(briefer.build_brief(raw, ctx))
    assert brief.original_request == raw


def test_an_invalid_depth_budget_falls_back_to_standard(ctx, replies):
    replies["queue"] = [_payload(depth_budget="standard")]
    assert _run(briefer.build_brief("x", ctx)).depth_budget is DepthBudget.STANDARD


# ── Criteria have to be checkable ─────────────────────────────────────────────


@pytest.mark.parametrize("bad", [
    "gibt eine umfassende Antwort",
    "analysiert das Thema gut",
    "berücksichtigt alle relevanten Aspekte",
    "gives a comprehensive answer",
    "is detailed and precise",
    "kurz",
    "Welche Fristen gibt es?",
])
def test_a_criterion_nobody_could_check_is_named_as_a_defect(bad):
    assert briefer.criterion_problem(bad)


@pytest.mark.parametrize("good", [
    "nennt jede Frist mit Datum und Quelldokument",
    "unterscheidet vertraglich fixierte von gesetzlichen Fristen",
    "lists only products serving the same function, and states that function",
])
def test_a_checkable_criterion_passes(good):
    assert briefer.criterion_problem(good) == ""


def test_a_vague_brief_is_sent_back_with_the_defect_named(ctx, replies):
    replies["queue"] = [
        _payload(acceptance_criteria=["gibt eine umfassende Antwort"]),
        _payload(acceptance_criteria=["nennt jede Frist mit Datum und Quelldokument"]),
    ]
    brief = _run(briefer.build_brief("Welche Fristen?", ctx))
    assert brief.acceptance_criteria == ["nennt jede Frist mit Datum und Quelldokument"]
    # The retry has to say what was wrong; a bare "try again" reproduces it.
    retry_prompt = replies["calls"][1]["messages"][0]["content"]
    assert "umfassende Antwort" in retry_prompt


def test_a_brief_that_stays_vague_is_still_returned_for_the_user_to_fix(ctx, replies):
    """Refusing to start over a phrasing would be worse than letting the user edit it."""
    replies["queue"] = [_payload(acceptance_criteria=["gibt eine umfassende Antwort"])] * 2
    brief = _run(briefer.build_brief("Welche Fristen?", ctx))
    assert brief.acceptance_criteria == ["gibt eine umfassende Antwort"]


# ── Comparison tasks need a definition ────────────────────────────────────────


def test_a_competitor_task_without_a_definition_criterion_is_a_defect():
    """The AirEx failure: without this the run compares names, not functions."""
    brief = Brief(
        original_request="Finde Wettbewerbsprodukte zum AirEx von Argo-Hytos",
        goal="Wettbewerbsprodukte auflisten",
        acceptance_criteria=["nennt für jedes Produkt den Hersteller"],
    )
    assert briefer.missing_definition_criterion(brief)
    assert any("comparable" in d for d in briefer.brief_defects(brief))


def test_a_competitor_task_with_a_function_criterion_is_fine():
    brief = Brief(
        original_request="Finde Wettbewerbsprodukte zum AirEx von Argo-Hytos",
        goal="Wettbewerbsprodukte auflisten",
        acceptance_criteria=[
            "nennt nur Produkte mit derselben Funktion (Be- und Entlüftung von Hydrauliktanks)",
            "nennt für jedes Produkt den Hersteller",
        ],
    )
    assert not briefer.missing_definition_criterion(brief)


def test_a_task_that_is_not_a_comparison_needs_no_definition_criterion():
    brief = Brief(
        original_request="Welche Fristen stehen im Mietvertrag?",
        goal="Fristen nennen",
        acceptance_criteria=["nennt jede Frist mit Datum und Quelldokument"],
    )
    assert not briefer.missing_definition_criterion(brief)


# ── The LLM wrapper ───────────────────────────────────────────────────────────


def test_a_schema_violation_is_retried_with_the_exact_errors(ctx, replies):
    replies["queue"] = [{"goal": "x"}, _payload()]          # first one lacks required fields
    log = llm.PromptLog()
    _run(briefer.build_brief("Welche Fristen?", ctx, prompt_log=log))
    retry = replies["calls"][1]["messages"][-1]["content"]
    assert "did not match the schema" in retry
    assert "original_request" in retry


def test_giving_up_after_the_retry_cap_raises_rather_than_returning_junk(ctx, replies):
    replies["queue"] = [{"goal": "x"}] * 5
    with pytest.raises(ValueError, match="schema-valid"):
        _run(briefer.build_brief("Welche Fristen?", ctx))


def test_every_prompt_carries_the_current_date(ctx, replies):
    """A model without today's date dates 'last quarter' from its training cut-off."""
    replies["queue"] = [_payload()]
    _run(briefer.build_brief("Welche Fristen?", ctx))
    assert "Current date:" in replies["calls"][0]["system"]
    assert "calendar week" in replies["calls"][0]["system"]


def test_the_judging_roles_run_colder_than_the_runner():
    assert llm.ROLE_TEMPERATURE["fact_critic"] < llm.ROLE_TEMPERATURE["runner"]
    assert llm.ROLE_TEMPERATURE["plan_critic"] <= 0.1


def test_the_call_is_written_to_the_prompt_log(ctx, replies, tmp_path):
    """Phase 4 has to prove what a role did and did not see. An assertion about
    a prompt is worth nothing without the prompt."""
    replies["queue"] = [_payload()]
    log = llm.PromptLog()
    _run(briefer.build_brief("Welche Fristen?", ctx, prompt_log=log))

    assert log.systems_for("briefer")
    lines = (tmp_path / "r1.jsonl").read_text(encoding="utf-8").strip().splitlines()
    entry = json.loads(lines[0])
    assert entry["role"] == "briefer" and entry["run_id"] == "r1"
    assert "Welche Fristen?" in entry["messages"][0]["content"]


def test_a_broken_log_directory_does_not_take_the_run_down(ctx, replies, tmp_path):
    replies["queue"] = [_payload()]
    ctx.log_dir = tmp_path / "not-writable" / "deeper"
    ctx.log_dir.parent.mkdir()
    ctx.log_dir.parent.chmod(0o500)
    try:
        assert _run(briefer.build_brief("Welche Fristen?", ctx)).goal
    finally:
        ctx.log_dir.parent.chmod(0o700)
