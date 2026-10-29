"""Research reports as vault notes (`vault/research.py`): every finished
Werkbank run is a note in `PaperlessBrain Research/`, which outlives the run,
and opens as a PDF from the findings page."""

import asyncio
import types
from datetime import datetime

import pytest

from config.settings import settings
from werkbank.v2 import store
from werkbank.v2.models import Brief, DepthBudget, RunState

REPORT = "# Riester-Vertrag prüfen\n\nDie Kosten liegen bei 2,5 %. [^12]\n\n## Quellen\n\n- https://a.example/x\n"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_root", tmp_path / "vaults")
    monkeypatch.setattr(settings, "app_path", tmp_path, raising=False)
    monkeypatch.setattr(store, "_DB_PATH", tmp_path / "data" / "papersage.db")
    (tmp_path / "vaults" / "alice").mkdir(parents=True)
    return tmp_path / "vaults" / "alice"


def _run(run_id="run1", goal="Riester-Vertrag prüfen", report=REPORT, user="alice"):
    brief = Brief(original_request="Lohnt sich mein Riester-Vertrag noch?", goal=goal,
                  acceptance_criteria=["nennt Kosten"], depth_budget=DepthBudget.STANDARD)
    store.create_run(run_id, user, brief, model="qwen")
    store.save_state(run_id, user, RunState(run_id=run_id, user_id=user, model="qwen", brief=brief,
                                            finished_at="2026-10-28T20:00:00+00:00"))
    store.save_report(run_id, user, report)


def test_a_finished_run_becomes_a_note_that_outlives_the_run(vault):
    from vault import frontmatter, research

    _run()
    rel = asyncio.run(research.save_run("alice", "run1", extra={"finding": "[[PaperlessBrain Findings/Notices/x|X]]"}))
    assert rel == "PaperlessBrain Research/26-10-28 Riester-Vertrag prüfen.md"
    meta, body = frontmatter.read(vault / rel)
    assert meta["werkbank_run"] == "run1" and meta["source"] == "research" and meta["model"] == "qwen"
    assert meta["finding"] == "[[PaperlessBrain Findings/Notices/x|X]]"
    assert "pbrain_id" not in meta  # sync mints it, like for any note
    assert body.startswith("# Riester-Vertrag prüfen\n\nDie Kosten")
    assert "Lohnt sich" not in body  # the report carries the assignment; the note does not repeat it
    assert "2,5 %" in body
    store.delete_run("run1", "alice")
    assert (vault / rel).exists()
    assert asyncio.run(research.note_for_run("alice", "run1")) == rel  # found without the run


def test_the_note_is_named_after_the_reports_title_not_the_briefs_goal(vault):
    from vault import research

    _run(goal="Für den konkreten Riester-Vertrag (Dokument 12) wird eine Einschätzung geliefert, ob")
    rel = asyncio.run(research.save_run("alice", "run1"))
    assert rel == "PaperlessBrain Research/26-10-28 Riester-Vertrag prüfen.md"


def test_the_same_run_again_rewrites_its_note_even_renamed(vault):
    from vault import research

    _run()
    rel = asyncio.run(research.save_run("alice", "run1"))
    renamed = vault / "PaperlessBrain Research" / "Mein Riester.md"
    (vault / rel).rename(renamed)
    store.save_report("run1", "alice", REPORT + "\nNachtrag.\n")
    assert asyncio.run(research.save_run("alice", "run1")) == "PaperlessBrain Research/Mein Riester.md"
    assert "Nachtrag." in renamed.read_text()
    assert len(list((vault / "PaperlessBrain Research").glob("*.md"))) == 1


def test_two_runs_with_one_goal_on_one_day_get_two_notes(vault):
    from vault import research

    _run("run1")
    _run("run2")
    a = asyncio.run(research.save_run("alice", "run1"))
    b = asyncio.run(research.save_run("alice", "run2"))
    assert a != b and b.endswith("prüfen-1.md")


@pytest.fixture
def indexed(monkeypatch):
    """Calls of `research.index` — the real one runs a vault sync (Chroma)."""
    from vault import research

    calls = []

    async def index(user):
        calls.append(user)

    monkeypatch.setattr(research, "index", index)
    return calls


def test_backfill_writes_each_finished_run_once(vault, indexed):
    from vault import research

    _run("run1")
    _run("run2")
    store.create_run("run3", "alice", Brief(original_request="x", goal="offen", acceptance_criteria=["y"],
                                            depth_budget=DepthBudget.STANDARD))  # not finished
    assert asyncio.run(research.backfill("alice")) == 2
    assert asyncio.run(research.backfill("alice")) == 0
    assert indexed == ["alice"]  # one sync for the batch, none when nothing was written


def test_a_note_written_for_an_older_run_is_indexed_at_once(vault, indexed):
    from vault import research

    _run()
    assert asyncio.run(research.note_for_run("alice", "run1"))
    assert asyncio.run(research.note_for_run("alice", "run1"))  # found: nothing to index
    assert indexed == ["alice"]


def test_indexing_is_the_chat_turns_sync_and_never_fails_the_caller(monkeypatch):
    import sys
    import types

    from vault import research

    seen = []

    async def sync_user(user, force=False):
        seen.append((user, force))
        raise RuntimeError("chroma down")

    monkeypatch.setitem(sys.modules, "vault.sync", types.SimpleNamespace(sync_user=sync_user))
    asyncio.run(research.index("alice"))
    assert seen == [("alice", True)]


def test_the_pipeline_writes_the_note_after_the_report(vault, monkeypatch):
    """`start_run`'s last step; checked on the call, not on a full run."""
    import inspect

    from werkbank.v2 import pipeline

    src = inspect.getsource(pipeline.start_run)
    # report → note → embedded → "done" message: the chat after it finds the report
    assert (src.index("store.save_report(") < src.index("research.save_run(")
            < src.index("research.index(") < src.index("enqueue_event("))


def test_only_notes_in_the_research_folder_are_served(vault):
    from vault import research

    (vault / "Private.md").write_text("geheim")
    for rel in ("Private.md", "PaperlessBrain Research/../Private.md", "PaperlessBrain Research/nope.md"):
        with pytest.raises(Exception):
            research.read_note("alice", rel)


def test_wikilinks_for_the_pdf_and_for_the_findings_page():
    from app_ui.pages.findings import _wikilinks
    from vault import research

    link = research.wikilink("PaperlessBrain Research/26-10-28 Riester.md", "Full report")
    assert link == "[[PaperlessBrain Research/26-10-28 Riester|Full report]]"
    assert research.strip_wikilinks(link + " and [[Notes/Todo]]") == "Full report and Todo"
    html = _wikilinks(link + " and [[Notes/Todo]]")
    assert html == ("[Full report](/api/research/pdf?path=PaperlessBrain%20Research%2F26-10-28%20Riester.md)"
                    " and Todo")


def test_an_older_findings_werkbank_link_becomes_a_link_to_the_note():
    from proactive.findings import link_research_note

    body = "## Recherche\n\n[Vollständiger Bericht](https://brain.lan/werkbank?run=run1)\n\n### Zusammenfassung\n"
    out = link_research_note(body, "run1", "PaperlessBrain Research/26-10-28 Riester.md")
    assert "[[PaperlessBrain Research/26-10-28 Riester|Vollständiger Bericht]]" in out and "werkbank" not in out
    assert link_research_note(body, "other", "x.md") == body


def test_the_pdf_route_renders_the_note_for_its_owner_only(vault, monkeypatch):
    from app_ui import research_routes
    from vault import research

    _run()
    rel = asyncio.run(research.save_run("alice", "run1", extra={"finding": "[[PaperlessBrain Findings/Notices/x|X]]"}))
    rendered = []

    def fake_pdf(md, title, user, model, dt):
        rendered.append((md, title, user, model, dt))
        return b"%PDF-1.7"

    import services.pdf_generator as gen

    monkeypatch.setattr(gen, "generate_chat_pdf", fake_pdf)
    monkeypatch.setattr(research_routes, "get_session_token", lambda: "t")
    for user, expected in (("alice", 200), ("bob", 404)):
        monkeypatch.setattr(research_routes, "ng_app", types.SimpleNamespace(
            storage=types.SimpleNamespace(user={"paperless_user": user})))
        (settings.vault_root / user).mkdir(exist_ok=True)
        resp = asyncio.run(research_routes.research_pdf(rel))
        assert resp.status_code == expected
    md, title, user, model, dt = rendered[0]
    assert title == "Riester-Vertrag prüfen" and not md.lstrip().startswith("# ")  # the title block has it
    assert user == "alice" and model == "qwen" and isinstance(dt, datetime)
    assert resp.status_code == 404
