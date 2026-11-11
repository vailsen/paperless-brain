"""A folder of the dossier tree has an overview (`proactive/folder_overview.py`):
out of date is computed from the dossiers inside, the model writes text only,
and the note is never a dossier and never indexed."""

import asyncio

import pytest

from tests.test_proactive_dossiers import _run, env  # noqa: F401


def _setup():
    from proactive import dossiers

    async def go():
        a = await dossiers.create_manual("alice", "Bauspar", "Geld")
        b = await dossiers.create_manual("alice", "Riester", "Geld/Rente")
        await dossiers.create_manual("alice", "Strom", "")
        for d, state in ((a, "Guthaben 12.400 EUR."), (b, "Zulage beantragt.")):
            d.current_state = state
            await dossiers.write("alice", d)

    asyncio.run(go())


def test_stale_until_built_and_again_after_a_change(env, monkeypatch):
    from proactive import dossiers, folder_overview as fo, llm

    _setup()
    assert fo.stale_folders("alice") == ["Geld", "Geld/Rente"]
    calls = []

    async def structured(model, user, token, **kw):
        calls.append(kw["messages"][0]["content"])
        return {"overview": "Two matters on savings. Balance 99.999 EUR.",   # invented number: dropped
                "dossiers": [{"id": "bauspar", "line": "Balance 12.400 EUR."},
                             {"id": "strom", "line": "not in this folder"}]}

    monkeypatch.setattr(llm, "structured", structured)
    _run("folder_overview", "alice", {"folder": "Geld"})
    ov = fo.load("alice", "Geld")
    assert ov.text == "Two matters on savings."
    assert ov.lines == {"bauspar": "Balance 12.400 EUR."}
    assert "Riester" in calls[0] and "Strom" not in calls[0]          # the subtree, nothing else
    assert not fo.is_stale("alice", "Geld")
    text = (dossiers._root("alice") / "Geld" / fo.FILE).read_text()
    assert "[[" in text and "|Bauspar]]" in text

    # an overview is never a dossier
    assert {d.topic for d in dossiers.list_dossiers("alice")} == {"bauspar", "riester", "strom"}

    # nothing changed: the job does nothing
    _run("folder_overview", "alice", {"folder": "Geld"})
    assert len(calls) == 1

    d = dossiers.load("alice", "riester")
    d.open_questions = ["Zulage erhalten?"]
    asyncio.run(dossiers.write("alice", d))
    assert fo.is_stale("alice", "Geld")


def test_never_indexed(env):
    from proactive import folder_overview as fo, retrieval
    from proactive.paths import DOSSIERS, findings_rel

    rel = f"{findings_rel()}/{DOSSIERS}/Geld/{fo.FILE}"
    assert retrieval.index_meta(rel, {fo.MARK: True, "overview": "x"}) is None


def test_delete_folder_takes_its_overview_along(env):
    from proactive import dossiers, folder_overview as fo

    _setup()
    asyncio.run(fo.write("alice", "Geld/Rente", fo.Overview(built="2026-11-11", text="x")))
    assert asyncio.run(dossiers.delete_folder("alice", "Geld/Rente")) == 1
    assert "Geld/Rente" not in dossiers.folders("alice")


def test_waits_for_a_rescan_of_a_dossier_inside(env, monkeypatch):
    from proactive import dossiers, folder_overview as fo, jobs, llm

    _setup()

    async def structured(model, user, token, **kw):
        raise AssertionError("must wait")

    monkeypatch.setattr(llm, "structured", structured)
    jobs.enqueue("dossier_relook", "alice", {"topic": "riester"}, delay=__import__("datetime").timedelta(hours=1),
                 dedupe_key=dossiers.relook_key("alice", "riester"))
    _run("folder_overview", "alice", {"folder": "Geld"})
    assert fo.load("alice", "Geld") is None
