"""Voice-memo write primitive.

The load-bearing parts are the ones a later change could silently break:
memos must land in the memo subfolder (not the brain one — that would route
them into the wrong Chroma collection and skip chunking), the filename must
carry date+time+topic, and two memos recorded in the same minute must not
overwrite each other.
"""

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from config.settings import settings
from vault.memo_writer import VaultMemoWriter, _slug


class FakeChroma:
    """Captures upserts instead of embedding — the writer's contract with the
    vault collection is what matters here, not the vector maths."""

    def __init__(self) -> None:
        self.upserts: list[dict] = []

    async def upsert(self, **kwargs) -> None:
        self.upserts.append(kwargs)


@pytest.fixture
def writer(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_root", tmp_path, raising=False)
    monkeypatch.setattr(settings, "app_path", tmp_path, raising=False)
    return VaultMemoWriter(vault_chroma=FakeChroma())


def _run(coro):
    return asyncio.run(coro)


def _memo_dir(tmp_path: Path) -> Path:
    return tmp_path / "alice" / settings.memo_subfolder


# ── Filename ──────────────────────────────────────────────────────────────────


def test_filename_carries_date_time_and_topic(writer, tmp_path):
    _run(writer.create_memo(
        "Klempner kommt Dienstag.", "alice",
        topic="Klempner Termin", when=datetime(2026, 8, 8, 14, 32),
    ))
    files = list(_memo_dir(tmp_path).glob("*.md"))
    assert [f.name for f in files] == ["2026-08-08 1432 Klempner Termin.md"]


def test_same_minute_same_topic_does_not_overwrite(writer, tmp_path):
    when = datetime(2026, 8, 8, 14, 32)
    for text in ("Erste Notiz.", "Zweite Notiz."):
        _run(writer.create_memo(text, "alice", topic="Klempner", when=when))
    names = sorted(f.name for f in _memo_dir(tmp_path).glob("*.md"))
    assert names == ["2026-08-08 1432 Klempner-1.md", "2026-08-08 1432 Klempner.md"]


def test_topic_falls_back_to_the_text(writer, tmp_path):
    _run(writer.create_memo(
        "Gebäudeversicherung prüfen.", "alice", when=datetime(2026, 8, 8, 9, 5)
    ))
    (f,) = list(_memo_dir(tmp_path).glob("*.md"))
    assert f.name.startswith("2026-08-08 0905 Gebäudeversicherung")


@pytest.mark.parametrize("raw", ["a/b:c*d?", 'e"f<g>h', "i|j#k^l[m]n"])
def test_slug_strips_characters_illegal_in_filenames(raw):
    assert not set(_slug(raw)) & set('\\/:*?"<>|#^[]')


def test_slug_truncates_on_a_word_boundary():
    slug = _slug("Steuererklärung 2024 mit Mieteinkünften und Abschreibung prüfen lassen")
    assert len(slug) <= 48
    assert not slug.endswith(" ")
    # Truncation must not leave a half-word
    assert slug.split()[-1] in "Steuererklärung 2024 mit Mieteinkünften und Abschreibung".split()


def test_empty_memo_is_refused(writer):
    with pytest.raises(ValueError):
        _run(writer.create_memo("   \n ", "alice"))


# ── Routing and indexing ──────────────────────────────────────────────────────


def test_memos_do_not_land_in_the_brain_folder(writer, tmp_path):
    """Under the brain subfolder they would go to the `brain` collection
    unchunked — the wrong collection for the user's own notes."""
    _run(writer.create_memo("Eine Notiz.", "alice"))
    brain_dir = tmp_path / "alice" / settings.brain_subfolder
    assert not list(brain_dir.glob("*.md"))
    assert list(_memo_dir(tmp_path).glob("*.md"))


def test_chunks_are_upserted_with_vault_collection_keys(writer, tmp_path):
    pbrain_id, rel = _run(writer.create_memo(
        "Klempner kommt Dienstag.", "alice", topic="Klempner",
        when=datetime(2026, 8, 8, 14, 32),
    ))
    (call,) = writer._c.upserts
    assert call["ids"] == [f"{pbrain_id}:0"]
    meta = call["metadatas"][0]
    assert meta["pbrain_id"] == pbrain_id
    assert meta["user"] == "alice"
    # Deletions are driven by path, so it must be stored (see vault/sync.py)
    assert meta["path"] == rel
    assert settings.memo_subfolder in rel


def test_note_name_is_embedded_alongside_the_body(writer):
    """Without the filename in the embedded text, a query on the topic never
    matches — the chunk body alone does not carry it."""
    _run(writer.create_memo("Kurzer Inhalt.", "alice", topic="Gebäudereinigung"))
    (call,) = writer._c.upserts
    assert "Gebäudereinigung" in call["embed_documents"][0]
    assert call["documents"] == ["Kurzer Inhalt."]  # stored snippet stays clean


def test_frontmatter_carries_id_source_and_the_opt_out_flag(writer, tmp_path):
    pbrain_id, _ = _run(writer.create_memo("Inhalt.", "alice"))
    (f,) = list(_memo_dir(tmp_path).glob("*.md"))
    text = f.read_text(encoding="utf-8")
    assert f"pbrain_id: {pbrain_id}" in text
    assert "source: memo" in text
    # Must exist so the note stays toggleable from Obsidian like any vault note
    assert "dont_ingest: false" in text
    assert text.rstrip().endswith("Inhalt.")


def test_a_committed_memo_leaves_nothing_for_sync_to_redo(writer, tmp_path):
    """The writer commits inline. sync_user() diffs against HEAD, so an
    already-committed memo must not show up as pending work."""
    from vault.git_wrapper import diff_name_status

    _run(writer.create_memo("Inhalt.", "alice"))
    assert diff_name_status(tmp_path / "alice") == []
