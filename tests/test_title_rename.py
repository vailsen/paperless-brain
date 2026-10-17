"""Titling documents from a user template after AI analysis."""

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from pipelines import title_rename as tr
from pipelines.title_rename import DEFAULT_TEMPLATE, MAX_TITLE_LENGTH, render_title

CREATED = datetime(2026, 10, 17, 9, 30)


# ── Rendering ─────────────────────────────────────────────────────────────────


def test_the_users_schema():
    assert render_title(
        DEFAULT_TEMPLATE, created=CREATED, doc_type="Rechnung",
        description="CPU", correspondent="Amazon",
    ) == "2026_10_17_Rechnung_CPU_Amazon"


def test_an_empty_value_takes_one_separator_with_it():
    assert render_title(DEFAULT_TEMPLATE, created=CREATED, doc_type="Rechnung",
                        description="CPU") == "2026_10_17_Rechnung_CPU"
    assert render_title(DEFAULT_TEMPLATE, created=CREATED, description="CPU",
                        correspondent="Amazon") == "2026_10_17_CPU_Amazon"
    assert render_title("{doc_type} - {description}", created=None,
                        description="CPU") == "CPU"


def test_characters_that_break_a_path_are_removed():
    """A "/" would become a directory under PAPERLESS_FILENAME_FORMAT={{ title }}."""
    title = render_title("{description}", created=None, description='A/B: "C"?')
    assert "/" not in title and ":" not in title and '"' not in title


def test_unknown_placeholders_stay_visible():
    assert render_title("{year}_{typo}", created=CREATED) == "2026_{typo}"


def test_titles_fit_paperless():
    title = render_title("{description}", created=None, description="word " * 60)
    assert len(title) <= MAX_TITLE_LENGTH and not title.endswith(" ")


# ── Planning and renaming ─────────────────────────────────────────────────────


def _doc(doc_id, title, doc_type=None, correspondent="Amazon"):
    return SimpleNamespace(id=doc_id, title=title, created=CREATED,
                           document_type=doc_type, correspondent=correspondent)


class _Paperless:
    def __init__(self, docs):
        self.docs = docs
        self.patched: list[tuple[int, str]] = []

    async def list_documents(self, ids=None):
        return [d for d in self.docs if ids is None or d.id in ids]

    async def update_document_title(self, doc_id, title):
        self.patched.append((doc_id, title))


class _Sidecars:
    def __init__(self, summaries):
        self.summaries = summaries

    def load_sidecar(self, doc_id):
        text = self.summaries.get(doc_id)
        return None if text is None else {"full_summary_summarized": text}


class _Describer:
    def __init__(self, answer=("Rechnung", "CPU")):
        self.answer = answer
        self.calls = 0

    async def __call__(self, summary):
        self.calls += 1
        return self.answer


@pytest.fixture
def state(tmp_path):
    return tmp_path / "title_renames.json"


def test_the_dry_run_changes_nothing_in_paperless(state):
    pl = _Paperless([_doc(1, "scan_0001"), _doc(2, "scan_0002")])
    plans, total = asyncio.run(tr.preview_renames(
        pl, _Sidecars({1: "s", 2: "s"}), _Describer(), DEFAULT_TEMPLATE, path=state,
    ))
    assert total == 2
    assert [p.new_title for p in plans] == ["2026_10_17_Rechnung_CPU_Amazon"] * 2
    assert pl.patched == []


def test_the_real_run_reuses_the_dry_runs_answers(state):
    pl, sidecars, describe = _Paperless([_doc(1, "scan_0001")]), _Sidecars({1: "s"}), _Describer()
    asyncio.run(tr.preview_renames(pl, sidecars, describe, DEFAULT_TEMPLATE, path=state))
    asyncio.run(tr.rename_documents(pl, sidecars, describe, DEFAULT_TEMPLATE, path=state))
    assert describe.calls == 1
    assert pl.patched == [(1, "2026_10_17_Rechnung_CPU_Amazon")]


def test_hand_edited_titles_are_renamed_too_but_only_once(state):
    """Every untreated document is renamed on request — a title typed by hand
    included. After that it is never touched again, so a later edit stays."""
    pl, sidecars = _Paperless([_doc(1, "My careful title")]), _Sidecars({1: "s"})
    renamed, failed = asyncio.run(
        tr.rename_documents(pl, sidecars, _Describer(), DEFAULT_TEMPLATE, path=state))
    assert (renamed, failed) == (1, 0)
    pl.docs[0].title = "Edited again by hand"
    asyncio.run(tr.rename_documents(pl, sidecars, _Describer(), "{description}", path=state))
    assert len(pl.patched) == 1
    record = tr.load_state(state)["done"]["1"]
    assert record["from"] == "My careful title"


def test_the_paperless_type_wins_over_the_models(state):
    pl = _Paperless([_doc(1, "x", doc_type="Invoice")])
    asyncio.run(tr.rename_documents(pl, _Sidecars({1: "s"}), _Describer(),
                                    "{doc_type}_{description}", path=state))
    assert pl.patched == [(1, "Invoice_CPU")]


def test_documents_without_analysis_or_subject_are_left_alone(state):
    pl = _Paperless([_doc(1, "a"), _doc(2, "b")])
    renamed, _ = asyncio.run(tr.rename_documents(
        pl, _Sidecars({1: "s"}), _Describer(("Rechnung", "")), DEFAULT_TEMPLATE, path=state))
    assert renamed == 0 and pl.patched == []
    assert tr.load_state(state)["done"] == {}     # can be retried later


def test_the_sync_step_touches_only_the_documents_it_ingested(state):
    pl = _Paperless([_doc(1, "a"), _doc(2, "b")])
    asyncio.run(tr.rename_documents(pl, _Sidecars({1: "s", 2: "s"}), _Describer(),
                                    "{description}", doc_ids=[2], path=state))
    assert [d for d, _ in pl.patched] == [2]


def test_an_empty_id_list_renames_nothing(state):
    """An empty id filter must not turn into "all documents"."""
    pl = _Paperless([_doc(1, "a")])
    asyncio.run(tr.rename_documents(pl, _Sidecars({1: "s"}), _Describer(),
                                    "{description}", doc_ids=[], path=state))
    assert pl.patched == []


def test_a_failed_patch_is_not_recorded_as_done(state):
    class _Refusing(_Paperless):
        async def update_document_title(self, doc_id, title):
            raise RuntimeError("403 Forbidden")

    pl = _Refusing([_doc(1, "a")])
    renamed, failed = asyncio.run(tr.rename_documents(
        pl, _Sidecars({1: "s"}), _Describer(), "{description}", path=state))
    assert (renamed, failed) == (0, 1)
    assert tr.load_state(state)["done"] == {}


def test_stop_halts_the_backfill(state):
    pl = _Paperless([_doc(i, "a") for i in range(1, 6)])
    stop = {"now": False}

    def _progress(done, total):
        stop["now"] = done >= 2

    asyncio.run(tr.rename_documents(
        pl, _Sidecars({i: "s" for i in range(1, 6)}), _Describer(), "{description}",
        path=state, progress=_progress, should_stop=lambda: stop["now"]))
    assert len(pl.patched) == 2


# ── The model's answer ────────────────────────────────────────────────────────


@pytest.mark.parametrize("reply,expected", [
    ("TYPE: Rechnung\nSUBJECT: Grafikkarte", ("Rechnung", "Grafikkarte")),
    ("**TYPE:** invoice\n**SUBJECT:** graphics card", ("invoice", "graphics card")),
    ("Here you go:\nTYPE: Vertrag\nSUBJECT: Mietwohnung", ("Vertrag", "Mietwohnung")),
    ("TYPE: Счёт\nSUBJECT: карта", ("", "")),
    ("no labels at all", ("", "")),
])
def test_describe_for_title_parses_the_two_lines(monkeypatch, reply, expected):
    from services import vision

    class _Fake(vision.VisionClient):
        async def _vision_json(self, prompt, image_b64): ...
        async def _vision_text(self, prompt, image_b64): ...

        async def _text(self, prompt):
            return reply

    monkeypatch.setattr(vision, "_archive_language_name", lambda: "German (Deutsch)")
    monkeypatch.setattr(vision, "_wol_touch", lambda: None)
    assert asyncio.run(_Fake().describe_for_title("summary")) == expected
