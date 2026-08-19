"""Werkbank v2 — the scheduler.

What has to hold: independent subtasks run together, dependent ones wait and
receive **only facts**, the concurrency limit is respected because the GPU is
the limiting resource, a crashing subtask does not take the run with it, and an
interrupted run resumes instead of restarting.
"""

import asyncio

import pytest

from werkbank.v2 import registry, scheduler, store
from werkbank.v2.llm import LLMContext
from werkbank.v2.models import (
    Brief,
    CriticDecision,
    CriticVerdict,
    DepthBudget,
    Evidence,
    Fact,
    RunState,
    Source,
    Subtask,
    SubtaskResult,
    SubtaskStatus,
)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def ctx(tmp_path):
    return LLMContext(model="stub", user_id="alice", run_id="", log_dir=tmp_path)


@pytest.fixture
def reg():
    return registry.available_agents({"paperless", "vault"})


def _sub(sid, agent="doc_researcher", deps=()) -> Subtask:
    return Subtask(subtask_id=sid, question=f"Frage {sid}", agent=agent,
                   acceptance_criteria=["c"], covers_criteria=[0], depends_on=list(deps))


def _fact(sid) -> Fact:
    return Fact(id=f"{sid}.f1", claim=f"Ergebnis {sid}", evidence=Evidence.QUOTE,
                sources=[Source(id="s1", type="paperless", quote=f"Beleg {sid}")])


def _state(subtasks) -> RunState:
    return RunState(
        run_id="r1", user_id="alice", model="stub",
        brief=Brief(original_request="x", goal="y", acceptance_criteria=["c"],
                    depth_budget=DepthBudget.STANDARD),
        subtasks=subtasks,
    )


@pytest.fixture
def review(monkeypatch):
    """Stub the whole run-and-review of a subtask; records concurrency."""
    box = {"seen": [], "inherited": {}, "live": 0, "peak": 0, "fail": set(), "delay": 0.0}

    async def fake(subtask, spec, reg, ctx, **kw):
        box["live"] += 1
        box["peak"] = max(box["peak"], box["live"])
        box["seen"].append(subtask.subtask_id)
        box["inherited"][subtask.subtask_id] = list(kw.get("inherited_facts") or [])
        try:
            await asyncio.sleep(box["delay"])
            if subtask.subtask_id in box["fail"]:
                raise RuntimeError("model exploded")
            result = SubtaskResult(
                subtask_id=subtask.subtask_id, status=SubtaskStatus.OK,
                agent=subtask.agent, depends_on=subtask.depends_on,
                facts=[_fact(subtask.subtask_id)],
                narrative=f"Prosa von {subtask.subtask_id}",
            )
            return result, CriticVerdict(decision=CriticDecision.ACCEPT), False
        finally:
            box["live"] -= 1

    monkeypatch.setattr("werkbank.v2.critic.run_with_review", fake)
    return box


# ── Order and parallelism ─────────────────────────────────────────────────────


def test_independent_subtasks_run_together_and_dependents_wait(ctx, reg, review):
    review["delay"] = 0.02
    state = _state([_sub("st1"), _sub("st2"), _sub("st3", deps=["st1", "st2"])])
    _run(scheduler.run_plan(state, reg, ctx, persist=False))
    assert set(review["seen"][:2]) == {"st1", "st2"}
    assert review["seen"][2] == "st3"
    assert review["peak"] == 2                    # the first two really overlapped


def test_a_chain_runs_strictly_in_order(ctx, reg, review):
    review["delay"] = 0.01
    state = _state([_sub("st1"), _sub("st2", deps=["st1"]), _sub("st3", deps=["st2"])])
    _run(scheduler.run_plan(state, reg, ctx, persist=False))
    assert review["seen"] == ["st1", "st2", "st3"]
    assert review["peak"] == 1


def test_the_concurrency_limit_is_respected(ctx, reg, review, monkeypatch):
    """The GPU is the limiting resource, not the number of open questions."""
    monkeypatch.setattr(scheduler, "concurrency_for", lambda *a: 2)
    review["delay"] = 0.02
    state = _state([_sub(f"st{i}") for i in range(1, 7)])
    _run(scheduler.run_plan(state, reg, ctx, persist=False))
    assert review["peak"] <= 2
    assert len(review["seen"]) == 6


def test_a_cloud_backend_may_run_more_at_once():
    assert scheduler.CONCURRENCY["cloud"] > scheduler.CONCURRENCY["local"]


# ── What a dependent subtask inherits ─────────────────────────────────────────


def test_a_dependent_subtask_gets_facts_and_nothing_else(ctx, reg, review):
    state = _state([_sub("st1"), _sub("st2", deps=["st1"])])
    _run(scheduler.run_plan(state, reg, ctx, persist=False))
    inherited = review["inherited"]["st2"]
    assert [f.id for f in inherited] == ["st1.f1"]
    assert all(isinstance(f, Fact) for f in inherited)
    # The predecessor's prose exists, and is not part of what st2 was given.
    assert state.results["st1"].narrative
    assert not any("Prosa" in f.claim for f in inherited)


def test_facts_of_an_unresolved_predecessor_are_not_inherited(ctx, reg, review):
    """Nothing survived there — passing it on would launder a hole into a source."""
    state = _state([_sub("st1"), _sub("st2", deps=["st1"])])
    state.results["st1"] = SubtaskResult(
        subtask_id="st1", status=SubtaskStatus.UNRESOLVABLE, facts=[])
    _run(scheduler.run_plan(state, reg, ctx, persist=False))
    assert review["inherited"]["st2"] == []


def test_only_the_named_predecessors_are_inherited(ctx, reg, review):
    state = _state([_sub("st1"), _sub("st2"), _sub("st3", deps=["st1"])])
    _run(scheduler.run_plan(state, reg, ctx, persist=False))
    assert [f.id for f in review["inherited"]["st3"]] == ["st1.f1"]


# ── Failure policy ────────────────────────────────────────────────────────────


def test_a_crashing_subtask_does_not_stop_the_run(ctx, reg, review):
    """It becomes a named hole; the branches that worked still produce a report."""
    review["fail"] = {"st1"}
    state = _state([_sub("st1"), _sub("st2")])
    _run(scheduler.run_plan(state, reg, ctx, persist=False))
    assert state.results["st1"].status is SubtaskStatus.UNRESOLVABLE
    assert state.results["st1"].gaps          # the hole is named, not silent
    assert state.results["st2"].status is SubtaskStatus.OK


def test_an_agent_that_vanished_mid_run_becomes_a_gap(ctx, reg, review):
    """A credential removed between planning and execution."""
    state = _state([_sub("st1", agent="comms_researcher")])
    _run(scheduler.run_plan(state, reg, ctx, persist=False))
    assert state.results["st1"].status is SubtaskStatus.UNRESOLVABLE
    assert "not available" in state.results["st1"].gaps[0].suggested_source


def test_a_dependent_subtask_still_runs_after_its_predecessor_failed(ctx, reg, review):
    review["fail"] = {"st1"}
    state = _state([_sub("st1"), _sub("st2", deps=["st1"])])
    _run(scheduler.run_plan(state, reg, ctx, persist=False))
    assert "st2" in review["seen"]
    assert state.results["st2"].status is SubtaskStatus.OK


# ── Resume ────────────────────────────────────────────────────────────────────


def test_finished_subtasks_are_not_run_again(ctx, reg, review):
    state = _state([_sub("st1"), _sub("st2", deps=["st1"])])
    state.results["st1"] = SubtaskResult(
        subtask_id="st1", status=SubtaskStatus.OK, facts=[_fact("st1")])
    _run(scheduler.run_plan(state, reg, ctx, persist=False))
    assert review["seen"] == ["st2"]
    # …and the earlier result is still what the dependent one builds on.
    assert [f.id for f in review["inherited"]["st2"]] == ["st1.f1"]


@pytest.mark.parametrize("status", [SubtaskStatus.OK, SubtaskStatus.PARTIAL,
                                    SubtaskStatus.UNRESOLVABLE])
def test_every_terminal_status_counts_as_done(ctx, reg, review, status):
    state = _state([_sub("st1")])
    state.results["st1"] = SubtaskResult(subtask_id="st1", status=status)
    _run(scheduler.run_plan(state, reg, ctx, persist=False))
    assert review["seen"] == []


def test_an_interrupted_run_resumes_from_the_database(ctx, reg, review, tmp_path, monkeypatch):
    """The whole point of persisting each result the moment it exists.

    "Interrupted" means the process died between levels — not that a subtask
    failed. A failure is a *result* (`unresolvable`) and is never retried; a
    process that never got to st2 leaves nothing persisted for it.
    """
    from config.settings import settings

    monkeypatch.setattr(settings, "app_path", tmp_path, raising=False)
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "data" / "papersage.db")
    ctx.run_id = "r1"

    brief = Brief(original_request="x", goal="y", acceptance_criteria=["c"])
    plan = [_sub("st1"), _sub("st2", deps=["st1"])]
    store.create_run("r1", "alice", brief)
    store.save_plan("r1", "alice", plan)

    # Level 1 finished and was persisted; then the process went away.
    first_leg = _state([_sub("st1")])
    _run(scheduler.run_plan(first_leg, reg, ctx, persist=True))
    assert store.load_state("r1", "alice").results["st1"].status is SubtaskStatus.OK

    # Restart from the database: only the subtask that never ran runs now.
    review["seen"].clear()
    resumed = store.load_state("r1", "alice")
    resumed.subtasks = plan
    resumed.brief = brief
    _run(scheduler.run_plan(resumed, reg, ctx, persist=True))
    assert review["seen"] == ["st2"]
    # …and it built on the facts that survived the restart.
    assert [f.id for f in review["inherited"]["st2"]] == ["st1.f1"]
    assert store.load_state("r1", "alice").results["st2"].status is SubtaskStatus.OK


def test_a_failed_subtask_is_not_retried_on_resume(ctx, reg, review, tmp_path, monkeypatch):
    """`unresolvable` is a result. Retrying it forever is the loop the caps exist to stop."""
    from config.settings import settings

    monkeypatch.setattr(settings, "app_path", tmp_path, raising=False)
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "data" / "papersage.db")
    ctx.run_id = "r2"
    brief = Brief(original_request="x", goal="y", acceptance_criteria=["c"])
    plan = [_sub("st1")]
    store.create_run("r2", "alice", brief)
    store.save_plan("r2", "alice", plan)

    review["fail"] = {"st1"}
    _run(scheduler.run_plan(_state(plan), reg, ctx, persist=True))

    review["fail"] = set()
    review["seen"].clear()
    resumed = store.load_state("r2", "alice")
    resumed.subtasks, resumed.brief = plan, brief
    _run(scheduler.run_plan(resumed, reg, ctx, persist=True))
    assert review["seen"] == []


# ── Prompts of the remaining agents ───────────────────────────────────────────


def test_the_web_researcher_is_told_a_snippet_is_not_a_source():
    text = (registry.default_spec("web_researcher").prompt_path()).read_text(encoding="utf-8")
    assert "A search result is not a source" in text
    assert "web_fetch_page" in text


def test_the_synthesizer_prompt_has_no_tools_and_demands_derived_from():
    spec = registry.default_spec("synthesizer")
    assert set(spec.tools) <= {"calculate", "get_current_date"}
    text = spec.prompt_path().read_text(encoding="utf-8")
    assert "derived_from" in text
    assert "no access to" in text.lower()


def test_the_comms_researcher_is_warned_about_localised_folders():
    """The trap that turns 'I looked in the wrong folder' into 'there is nothing'."""
    text = (registry.default_spec("comms_researcher").prompt_path()).read_text(encoding="utf-8")
    assert "\\All" in text
    assert "time zone" in text.lower() or "zeitzone" in text.lower()


def test_every_default_agent_has_a_prompt_file():
    for agent_id in registry.default_ids():
        spec = registry.default_spec(agent_id)
        assert spec.prompt_path().is_file(), agent_id


# ── A subtask says it started, not only that it finished ──────────────────────


def test_a_subtask_is_marked_running_before_it_runs(ctx, reg, monkeypatch):
    """Without this the board shows five times "waiting" and then jumps to done.
    A subtask can take minutes; silence for all of it reads as a hang."""
    state = _state([_sub("st1")])
    seen: list[tuple[str, SubtaskStatus]] = []

    async def fake(subtask, spec, reg_, ctx_, **kw):
        # What the board would show while the work is happening.
        seen.append(("during", state.status_of("st1")))
        return (
            SubtaskResult(subtask_id="st1", status=SubtaskStatus.OK,
                          agent=subtask.agent, facts=[_fact("st1")]),
            CriticVerdict(decision=CriticDecision.ACCEPT),
            False,
        )

    monkeypatch.setattr("werkbank.v2.critic.run_with_review", fake)
    done = _run(scheduler.run_plan(
        state, reg, ctx, persist=False,
        progress=lambda sid, status: seen.append((sid, status)),
    ))

    assert ("during", SubtaskStatus.RUNNING) in seen
    assert ("st1", SubtaskStatus.RUNNING) in seen
    # …and it stops being "running" the moment there is a result to show.
    assert done.status_of("st1") is SubtaskStatus.OK
    assert "st1" not in done.statuses


def test_a_subtask_with_no_result_yet_reads_as_waiting():
    state = _state([_sub("st1")])

    assert state.status_of("st1") is SubtaskStatus.TODO
