"""Werkbank v2 — persistence.

Two properties earn their tests here: a run survives a restart without redoing
work that already cost model calls, and every query is scoped to a user. The
second one is not a nicety — background tasks are exactly where a missing
`WHERE user_id` leaks one person's documents into another person's report.
"""

import pytest

from config.settings import settings
from werkbank.v2 import store
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


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_path", tmp_path, raising=False)
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "data" / "papersage.db")
    return tmp_path


def _brief() -> Brief:
    return Brief(
        original_request="Welche Fristen stehen im Mietvertrag?",
        goal="Alle Fristen mit Datum und Quelle nennen",
        acceptance_criteria=["nennt jede Frist mit Datum und Quelldokument"],
        depth_budget=DepthBudget.STANDARD,
    )


def _plan() -> list[Subtask]:
    return [
        Subtask(subtask_id="st1", question="Welche Fristen?", agent="doc_researcher",
                covers_criteria=[0]),
        Subtask(subtask_id="st2", question="Widersprüche?", agent="contradiction_checker",
                depends_on=["st1"]),
    ]


def _result(subtask_id="st1", status=SubtaskStatus.OK) -> SubtaskResult:
    return SubtaskResult(
        subtask_id=subtask_id,
        status=status,
        question="Welche Fristen?",
        agent="doc_researcher",
        facts=[Fact(
            id=f"{subtask_id}.f1",
            claim="Die Frist beträgt drei Monate.",
            evidence=Evidence.QUOTE,
            sources=[Source(id="s1", type="paperless",
                            quote="Die Kündigungsfrist beträgt drei Monate.")],
        )],
    )


def test_a_run_round_trips(db):
    store.create_run("r1", "alice", _brief(), model="qwen")
    store.save_plan("r1", "alice", _plan())
    store.save_result("r1", "alice", _result())

    state = store.load_state("r1", "alice")
    assert state.brief.original_request == "Welche Fristen stehen im Mietvertrag?"
    assert [s.subtask_id for s in state.subtasks] == ["st1", "st2"]
    assert state.results["st1"].facts[0].claim.startswith("Die Frist")


def test_the_original_request_is_stored_byte_identical(db):
    """It is carried verbatim through the whole pipeline — including storage."""
    raw = "  Welche   Fristen?\n\nUnd was ist mit §4?  "
    store.create_run("r1", "alice", Brief(original_request=raw, goal="x"))
    assert store.load_state("r1", "alice").brief.original_request == raw


def test_a_finished_subtask_is_not_pending_again(db):
    """The resume contract: a completed subtask never costs a second model call."""
    store.create_run("r1", "alice", _brief())
    store.save_plan("r1", "alice", _plan())
    assert store.pending_subtask_ids("r1", "alice") == ["st1", "st2"]

    store.save_result("r1", "alice", _result("st1", SubtaskStatus.OK))
    assert store.pending_subtask_ids("r1", "alice") == ["st2"]


@pytest.mark.parametrize("status", [SubtaskStatus.OK, SubtaskStatus.PARTIAL,
                                    SubtaskStatus.UNRESOLVABLE])
def test_every_terminal_status_ends_the_work(db, status):
    """`unresolvable` is a result too — retrying it forever is the bug it prevents."""
    store.create_run("r1", "alice", _brief())
    store.save_plan("r1", "alice", _plan())
    store.set_status("r1", "alice", "st1", status)
    assert "st1" not in store.pending_subtask_ids("r1", "alice")


def test_replanning_keeps_the_status_of_work_already_done(db):
    """A re-saved plan must not silently reset a finished subtask to todo."""
    store.create_run("r1", "alice", _brief())
    store.save_plan("r1", "alice", _plan())
    store.save_result("r1", "alice", _result("st1", SubtaskStatus.OK))

    store.save_plan("r1", "alice", _plan())          # e.g. after a replanning round
    assert store.pending_subtask_ids("r1", "alice") == ["st2"]
    assert store.load_state("r1", "alice").results["st1"].facts


def test_every_revision_is_kept(db):
    """The audit trail is the point: verdicts are appended, never overwritten."""
    store.create_run("r1", "alice", _brief())
    store.save_plan("r1", "alice", _plan())
    store.save_verdict("r1", "alice", "st1", 0,
                       CriticVerdict(decision=CriticDecision.REVISE, defects=["kein Beleg"]))
    store.save_verdict("r1", "alice", "st1", 1,
                       CriticVerdict(decision=CriticDecision.ACCEPT))
    with store.connect() as conn:
        rows = conn.execute(
            "SELECT revision FROM wb2_verdicts WHERE run_id='r1' ORDER BY revision"
        ).fetchall()
    assert [r[0] for r in rows] == [0, 1]


# ── Raw text for D2 ───────────────────────────────────────────────────────────


def test_the_retrieved_text_is_kept_for_the_quote_check(db):
    """Without this table D2 cannot run at all — it is the evidence of record."""
    store.create_run("r1", "alice", _brief())
    store.log_tool_call(
        "r1", "alice", "st1", source_id="s1", tool="get_document_page_text",
        args={"document_id": 12}, raw_text="Die Kündigungsfrist beträgt drei Monate.",
        trust="authoritative", ref="doc:12#p1",
    )
    assert store.raw_texts_for("r1", "alice", "st1") == {
        "s1": "Die Kündigungsfrist beträgt drei Monate."
    }
    assert store.tool_call_count("r1", "alice", "st1") == 1


def test_deleting_a_run_takes_the_scratch_text_with_it(db):
    store.create_run("r1", "alice", _brief())
    store.log_tool_call("r1", "alice", "st1", source_id="s1", tool="search",
                        args={}, raw_text="…", trust="authoritative")
    store.delete_run("r1", "alice")
    assert store.load_state("r1", "alice") is None
    assert store.raw_texts_for("r1", "alice", "st1") == {}


# ── User scoping ──────────────────────────────────────────────────────────────


def test_another_user_cannot_load_the_run(db):
    store.create_run("r1", "alice", _brief())
    store.save_plan("r1", "alice", _plan())
    assert store.load_state("r1", "bob") is None


def test_another_user_sees_neither_the_subtasks_nor_the_retrieved_text(db):
    store.create_run("r1", "alice", _brief())
    store.save_plan("r1", "alice", _plan())
    store.log_tool_call("r1", "alice", "st1", source_id="s1", tool="search",
                        args={}, raw_text="vertraulich", trust="authoritative")
    assert store.pending_subtask_ids("r1", "bob") == []
    assert store.raw_texts_for("r1", "bob", "st1") == {}
    assert store.list_runs("bob") == []


def test_another_user_cannot_delete_the_run(db):
    store.create_run("r1", "alice", _brief())
    store.delete_run("r1", "bob")
    assert store.load_state("r1", "alice") is not None


def test_run_state_survives_a_reload_with_its_coverage_and_caps(db):
    store.create_run("r1", "alice", _brief())
    state = RunState(run_id="r1", user_id="alice", model="qwen",
                     capped_subtasks=["st4"], flagged_paragraphs=["ein Absatz"])
    store.save_state("r1", "alice", state)
    loaded = store.load_state("r1", "alice")
    assert loaded.capped_subtasks == ["st4"]
    assert loaded.flagged_paragraphs == ["ein Absatz"]
    assert loaded.model == "qwen"


def test_a_running_subtask_survives_a_reload_as_running(db):
    """The board reads the row's status when there is no result yet — that is
    the whole difference between "waiting" and "working on it right now"."""
    store.create_run("r1", "alice", _brief(), model="stub")
    store.save_plan("r1", "alice", [
        Subtask(subtask_id="st1", question="Q1", agent="doc_researcher",
                acceptance_criteria=["c"], covers_criteria=[0]),
        Subtask(subtask_id="st2", question="Q2", agent="doc_researcher",
                acceptance_criteria=["c"], covers_criteria=[0]),
    ])
    store.set_status("r1", "alice", "st1", SubtaskStatus.RUNNING)

    state = store.load_state("r1", "alice")

    assert state.status_of("st1") is SubtaskStatus.RUNNING
    assert state.status_of("st2") is SubtaskStatus.TODO
