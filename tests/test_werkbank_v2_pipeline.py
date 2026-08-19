"""Werkbank v2 — the run pipeline, and what the cutover to v2 must keep true.

Three things are worth pinning here:

- **The report is persisted, not held in memory.** A run outlives the page that
  started it; if the markdown lived only in the tab, closing it would throw away
  everything the run cost.
- **A run syncs the vault before it retrieves anything.** The note editor writes
  files without indexing them, so without this a run reads whatever the last
  chat turn happened to index — a note written minutes ago would be invisible to
  the run started to act on it.
- **A restart leaves runs resumable, never auto-resumed.** Each subtask costs
  model calls, so restarting one is the user's decision.
"""

import asyncio

import pytest

from config.settings import settings
from werkbank.v2 import pipeline, store
from werkbank.v2.models import Brief, DepthBudget


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


# ── The report survives the page that produced it ─────────────────────────────


def test_the_report_is_stored_with_the_run(db):
    store.create_run("r1", "alice", _brief(), model="stub")
    store.save_report("r1", "alice", "# Ergebnis\n\nDie Frist beträgt sechs Wochen [st1.f1].")

    assert "sechs Wochen" in store.load_report("r1", "alice")


def test_another_user_cannot_read_the_report(db):
    store.create_run("r1", "alice", _brief(), model="stub")
    store.save_report("r1", "alice", "vertraulich")

    assert store.load_report("r1", "bob") == ""


def test_writing_the_report_is_what_marks_a_run_done(db):
    store.create_run("r1", "alice", _brief(), model="stub")
    store.set_run_status("r1", "alice", "running")

    store.save_report("r1", "alice", "# Ergebnis")

    assert store.list_runs("alice")[0]["status"] == "done"


# ── Restart behaviour ─────────────────────────────────────────────────────────


def test_a_run_interrupted_by_a_restart_becomes_resumable(db):
    store.create_run("r1", "alice", _brief(), model="stub")
    store.set_run_status("r1", "alice", "running")

    assert store.reset_stale_runs() == 1
    assert store.list_runs("alice")[0]["status"] == "planned"


def test_a_finished_run_is_not_touched_by_the_restart_repair(db):
    store.create_run("r1", "alice", _brief(), model="stub")
    store.save_report("r1", "alice", "# Ergebnis")

    store.reset_stale_runs()

    assert store.list_runs("alice")[0]["status"] == "done"


# ── The vault is synced before anything is retrieved ──────────────────────────


def test_a_run_syncs_the_vault_before_it_plans(db, monkeypatch):
    calls: list[tuple[str, bool]] = []
    order: list[str] = []

    async def fake_sync(user, force=False):
        calls.append((user, force))
        order.append("sync")

    async def fake_plan(*args, **kwargs):
        order.append("plan")
        raise RuntimeError("stop here — the sync is what this test is about")

    monkeypatch.setattr("vault.sync.sync_user", fake_sync)
    monkeypatch.setattr(pipeline.planner, "plan_with_review", fake_plan)

    store.create_run("r1", "alice", _brief(), model="stub")
    with pytest.raises(RuntimeError):
        asyncio.run(pipeline.start_run("r1", "alice", "stub"))

    assert calls == [("alice", True)]
    assert order == ["sync", "plan"]


def test_a_broken_vault_does_not_take_the_run_down(db, monkeypatch):
    reached = []

    async def boom(user, force=False):
        raise OSError("vault mount is gone")

    async def fake_plan(*args, **kwargs):
        reached.append(True)
        raise RuntimeError("far enough")

    monkeypatch.setattr("vault.sync.sync_user", boom)
    monkeypatch.setattr(pipeline.planner, "plan_with_review", fake_plan)

    store.create_run("r1", "alice", _brief(), model="stub")
    with pytest.raises(RuntimeError, match="far enough"):
        asyncio.run(pipeline.start_run("r1", "alice", "stub"))

    assert reached == [True]


# ── The session token never reaches disk ──────────────────────────────────────


def test_the_paperless_token_is_held_in_memory_only(db):
    """The session token is what user-scoped tools authenticate with. It lives in
    a module dict for the process lifetime and is never persisted."""
    store.create_run("r1", "alice", _brief(), model="stub")
    pipeline.register_token("alice", "tok-secret")

    assert pipeline.token_for("alice") == "tok-secret"
    db_bytes = (db / "data" / "papersage.db").read_bytes()
    assert b"tok-secret" not in db_bytes


# ── Deleting a run actually deletes it ────────────────────────────────────────


def test_a_deleted_run_stays_deleted_even_if_its_task_is_still_writing(db):
    """The background task can be mid-subtask when the row is dropped, and
    `INSERT OR REPLACE` on a subtask would put the run back in the list —
    which reads as the delete button not working."""
    from werkbank.v2.models import Subtask, SubtaskResult, SubtaskStatus

    store.create_run("r1", "alice", _brief(), model="stub")
    store.save_plan("r1", "alice", [
        Subtask(subtask_id="st1", question="Q", agent="doc_researcher",
                acceptance_criteria=["c"], covers_criteria=[0]),
    ])
    store.delete_run("r1", "alice")

    # …the task finishes a subtask a second later:
    store.save_result("r1", "alice", SubtaskResult(
        subtask_id="st1", agent="doc_researcher", status=SubtaskStatus.OK))
    store.set_status("r1", "alice", "st1", SubtaskStatus.OK)
    store.log_tool_call("r1", "alice", "st1", source_id="s1", tool="search",
                        args={}, raw_text="x", trust="authoritative", ref="")

    assert store.list_runs("alice") == []
    assert store.load_state("r1", "alice") is None
    assert not store.run_exists("r1", "alice")


def test_cancelling_an_unknown_run_is_not_an_error(db):
    assert pipeline.cancel_run("nope") is False


# ── The run exists before the briefer does ────────────────────────────────────


def test_a_run_is_in_the_list_before_its_brief_exists(db):
    """Formulating an assignment takes the better part of a minute. While it
    ran, the request lived only inside one open dialog: navigating away threw it
    away, so the user had to sit and watch a screen doing nothing for them."""
    run_id = pipeline.create_draft("Recherchiere Vorfälle durch ETCS-Antennen", "alice", "stub")

    runs = store.list_runs("alice")
    assert len(runs) == 1
    assert runs[0]["status"] == pipeline.STATUS_BRIEFING

    state = store.load_state(run_id, "alice")
    assert state.brief.original_request == "Recherchiere Vorfälle durch ETCS-Antennen"
    assert state.brief.goal == ""            # not known yet, and not pretended


def test_the_finished_brief_lands_on_the_waiting_row(db, monkeypatch):
    run_id = pipeline.create_draft("Welche Fristen gelten?", "alice", "stub")

    async def fake_brief(request, ctx, **kw):
        return Brief(original_request=request, goal="Alle Fristen nennen",
                     acceptance_criteria=["nennt jede Frist mit Quelle"])

    monkeypatch.setattr(pipeline.briefer, "build_brief", fake_brief)
    asyncio.run(pipeline.brief_draft(run_id, "alice", "stub"))

    assert store.list_runs("alice")[0]["status"] == pipeline.STATUS_DRAFT
    state = store.load_state(run_id, "alice")
    assert state.brief.goal == "Alle Fristen nennen"
    assert state.brief.original_request == "Welche Fristen gelten?"


def test_a_failed_briefing_keeps_the_request(db, monkeypatch):
    """The user's words are the one thing that cannot be regenerated."""
    run_id = pipeline.create_draft("Welche Fristen gelten?", "alice", "stub")

    async def boom(request, ctx, **kw):
        raise RuntimeError("model down")

    monkeypatch.setattr(pipeline.briefer, "build_brief", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(pipeline.brief_draft(run_id, "alice", "stub"))

    state = store.load_state(run_id, "alice")
    assert state.brief.original_request == "Welche Fristen gelten?"
    assert store.list_runs("alice")[0]["status"] == "briefing_failed"


def test_confirming_a_draft_stores_the_edited_brief(db):
    """What the user corrected in the dialog is what the run is measured
    against — not what the briefer originally proposed."""
    run_id = pipeline.create_draft("x", "alice", "stub")
    edited = Brief(original_request="x", goal="Korrigiertes Ziel",
                   acceptance_criteria=["nennt die Quelle"])

    pipeline.confirm_draft(run_id, "alice", edited)

    assert store.list_runs("alice")[0]["status"] == "planned"
    assert store.load_state(run_id, "alice").brief.goal == "Korrigiertes Ziel"
