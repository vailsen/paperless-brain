"""A Werkbank run syncs the vault before it retrieves anything.

Workers call vault_search/brain_search, and the note editor deliberately writes
files without indexing them — the next sync does that. Without this sync a task
would run against whatever the last chat turn happened to index, so a note
written minutes ago would be invisible to the task started to act on it.
"""

import asyncio
from datetime import datetime

import pytest

from werkbank import orchestrator, repository
from werkbank.models import SubTask, SubTaskStatus, Task, TaskStatus


def _task() -> Task:
    now = datetime.now()
    return Task(
        id=1, user_id="alice", original_request="Was steht an?", refined_request="",
        status=TaskStatus.RUNNING, model="local", result_md=None, paperless_id=None,
        paperless_url=None, short_title=None, started_at=now, created_at=now,
        updated_at=now,
    )


def _subtask() -> SubTask:
    now = datetime.now()
    return SubTask(
        id=10, task_id=1, archetype_id=None, user_id="alice", instruction="x",
        success_criteria="y", status=SubTaskStatus.DONE, depends_on=[], order_index=0,
        result_raw="r", result_compacted="c", critic_verdict=None, retry_count=0,
        created_at=now, updated_at=now, started_at=now, finished_at=now,
    )


@pytest.fixture
def wired(monkeypatch):
    """Stub everything around the sync so only the wiring is under test."""
    calls: dict = {"sync": [], "status": []}

    async def _sync(username, force=False):
        calls["sync"].append((username, force))

    monkeypatch.setattr("vault.sync.sync_user", _sync)
    monkeypatch.setattr(repository, "get_task", lambda tid, uid: _task())
    monkeypatch.setattr(repository, "get_subtasks", lambda tid, uid: [_subtask()])
    monkeypatch.setattr(repository, "update_task_result", lambda *a, **k: None)
    monkeypatch.setattr(
        repository, "update_task_status",
        lambda tid, uid, status: calls["status"].append(status),
    )
    monkeypatch.setattr("werkbank.llm_lane.create_llm", lambda *a, **k: object())

    async def _synth(*a, **k):
        return "# Result"

    monkeypatch.setattr("werkbank.roles.synthesizer.run", _synth)
    return calls


def test_run_task_syncs_the_vault_before_working(wired):
    asyncio.run(orchestrator.run_task(1, "alice", "local", token=""))
    assert wired["sync"] == [("alice", True)]
    assert TaskStatus.AWAITING_REVIEW in wired["status"]


def test_a_failing_sync_does_not_abort_the_task(wired, monkeypatch):
    async def _boom(username, force=False):
        raise RuntimeError("vault mount gone")

    monkeypatch.setattr("vault.sync.sync_user", _boom)
    asyncio.run(orchestrator.run_task(1, "alice", "local", token=""))
    assert TaskStatus.AWAITING_REVIEW in wired["status"]


def test_a_missing_task_syncs_nothing(wired, monkeypatch):
    monkeypatch.setattr(repository, "get_task", lambda tid, uid: None)
    asyncio.run(orchestrator.run_task(999, "alice", "local", token=""))
    assert wired["sync"] == []
