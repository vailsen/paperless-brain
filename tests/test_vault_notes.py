"""Filesystem CRUD behind the note editor.

Two things here are security- or data-critical and everything else is
convenience: `resolve()` must never hand back a path outside the user's vault,
and `save_note()` must never overwrite a change it has not seen.
"""

import asyncio

import pytest

from config.settings import settings
from vault import notes
from vault.git_wrapper import ensure_repo, status_porcelain
from vault.notes import NoteConflict, VaultPathError

USER = "alice"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_root", tmp_path, raising=False)
    monkeypatch.setattr(settings, "app_path", tmp_path, raising=False)
    root = tmp_path / USER
    (root / "To-Dos").mkdir(parents=True)
    (root / "To-Dos" / "Einkauf.md").write_text("---\ntitle: X\n---\nMilch\n", encoding="utf-8")
    (root / "Urlaubslog.md").write_text("Sommer\n", encoding="utf-8")
    return root


def _run(coro):
    return asyncio.run(coro)


# ── Path safety ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("rel", [
    "../../etc/passwd",
    "/etc/passwd",
    "To-Dos/../../../etc/passwd",
    "evil\0.md",
    "..",
])
def test_resolve_rejects_escapes(vault, rel):
    with pytest.raises(VaultPathError):
        notes.resolve(USER, rel)


def test_resolve_rejects_a_symlink_pointing_outside(vault, tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text("nope", encoding="utf-8")
    (vault / "link.md").symlink_to(outside)
    with pytest.raises(VaultPathError):
        notes.resolve(USER, "link.md")


def test_resolve_accepts_umlauts_and_spaces(vault):
    (vault / "Mülltütengrößen für Küche.md").write_text("x", encoding="utf-8")
    assert notes.resolve(USER, "Mülltütengrößen für Küche.md").exists()


def test_resolve_of_the_root_itself_is_allowed(vault):
    assert notes.resolve(USER, "") == vault.resolve()


@pytest.mark.parametrize("name", ["", ".", "..", ".hidden", "a/b", "a:b", "x" * 300])
def test_check_name_rejects(name):
    with pytest.raises(VaultPathError):
        notes.check_name(name)


# ── Reads ─────────────────────────────────────────────────────────────────────


def test_read_note_returns_text_and_hash(vault):
    snap = _run(notes.read_note(USER, "Urlaubslog.md"))
    assert snap.text == "Sommer\n"
    assert snap.sha == notes.sha_of("Sommer\n")


def test_read_note_of_a_missing_file_is_a_deleted_conflict(vault):
    with pytest.raises(NoteConflict) as exc:
        _run(notes.read_note(USER, "gone.md"))
    assert exc.value.kind == "deleted"


def test_tree_sorts_folders_first_and_hides_noise(vault):
    (vault / ".gitignore").write_text("*", encoding="utf-8")
    (vault / "draft.md.tmp").write_text("x", encoding="utf-8")
    (vault / "Note.conflict.md").write_text("x", encoding="utf-8")
    (vault / ".obsidian").mkdir()
    tree = _run(notes.list_tree(USER))
    assert [n["label"] for n in tree] == ["To-Dos", "Urlaubslog.md"]
    assert tree[0]["dir"] and tree[0]["children"][0]["label"] == "Einkauf.md"


def test_tree_marks_pending_paths(vault):
    tree = _run(notes.list_tree(USER, pending={"Urlaubslog.md"}))
    assert [n["label"] for n in tree if n["pending"]] == ["Urlaubslog.md"]


def test_attachments_appear_but_are_not_notes(vault):
    (vault / "scan.pdf").write_bytes(b"%PDF")
    node = next(n for n in _run(notes.list_tree(USER)) if n["label"] == "scan.pdf")
    assert node["note"] is False and node["dir"] is False


def test_find_by_pbrain_id(vault):
    (vault / "id.md").write_text("---\npbrain_id: abc-123\n---\nx\n", encoding="utf-8")
    assert _run(notes.find_by_pbrain_id(USER, "abc-123")) == "id.md"
    assert _run(notes.find_by_pbrain_id(USER, "nope")) is None


# ── Saving ────────────────────────────────────────────────────────────────────


def test_save_with_the_expected_hash_writes(vault):
    snap = _run(notes.read_note(USER, "Urlaubslog.md"))
    out = _run(notes.save_note(USER, "Urlaubslog.md", "Winter\n", expected_sha=snap.sha))
    assert (vault / "Urlaubslog.md").read_text(encoding="utf-8") == "Winter\n"
    assert out.sha == notes.sha_of("Winter\n") and out.merged is False


def test_save_with_a_stale_hash_raises_and_carries_the_disk_text(vault):
    snap = _run(notes.read_note(USER, "Urlaubslog.md"))
    (vault / "Urlaubslog.md").write_text("Von Obsidian\n", encoding="utf-8")
    with pytest.raises(NoteConflict) as exc:
        _run(notes.save_note(USER, "Urlaubslog.md", "Meins\n", expected_sha=snap.sha))
    assert exc.value.kind == "modified"
    assert exc.value.disk_text == "Von Obsidian\n"
    assert (vault / "Urlaubslog.md").read_text(encoding="utf-8") == "Von Obsidian\n"


def test_save_without_an_expected_hash_overwrites(vault):
    (vault / "Urlaubslog.md").write_text("Von Obsidian\n", encoding="utf-8")
    _run(notes.save_note(USER, "Urlaubslog.md", "Meins\n", expected_sha=None))
    assert (vault / "Urlaubslog.md").read_text(encoding="utf-8") == "Meins\n"


def test_save_auto_merges_a_disjoint_outside_change(vault):
    base = "---\ntitle: X\n---\nMilch\n"
    snap = _run(notes.read_note(USER, "To-Dos/Einkauf.md"))
    (vault / "To-Dos" / "Einkauf.md").write_text(
        "---\npbrain_id: abc\ntitle: X\n---\nMilch\n", encoding="utf-8"
    )
    out = _run(notes.save_note(
        USER, "To-Dos/Einkauf.md", "---\ntitle: X\n---\nMilch, Brot\n",
        expected_sha=snap.sha, base_text=base,
    ))
    assert out.merged is True
    assert "pbrain_id: abc" in out.text and "Milch, Brot" in out.text


def test_save_of_a_deleted_file_raises_deleted(vault):
    snap = _run(notes.read_note(USER, "Urlaubslog.md"))
    (vault / "Urlaubslog.md").unlink()
    with pytest.raises(NoteConflict) as exc:
        _run(notes.save_note(USER, "Urlaubslog.md", "x", expected_sha=snap.sha))
    assert exc.value.kind == "deleted"


def test_open_and_save_with_no_edits_leaves_the_file_byte_identical(vault):
    """The autosave regression test: opening a note must not rewrite it."""
    original = (vault / "To-Dos" / "Einkauf.md").read_bytes()
    snap = _run(notes.read_note(USER, "To-Dos/Einkauf.md"))
    _run(notes.save_note(USER, "To-Dos/Einkauf.md", snap.text, expected_sha=snap.sha))
    assert (vault / "To-Dos" / "Einkauf.md").read_bytes() == original


def test_concurrent_saves_serialise(vault):
    async def go():
        snap = await notes.read_note(USER, "Urlaubslog.md")
        results = await asyncio.gather(
            notes.save_note(USER, "Urlaubslog.md", "A\n", expected_sha=snap.sha),
            notes.save_note(USER, "Urlaubslog.md", "B\n", expected_sha=snap.sha),
            return_exceptions=True,
        )
        return results

    results = _run(go())
    # One wins, the other sees the hash it expected is gone.
    assert sum(isinstance(r, NoteConflict) for r in results) == 1
    assert (vault / "Urlaubslog.md").read_text(encoding="utf-8") in ("A\n", "B\n")


# ── Create / rename / move / delete ───────────────────────────────────────────


def test_create_note_adds_the_suffix_and_starts_empty(vault):
    rel = _run(notes.create_note(USER, "To-Dos", "Bremsen"))
    assert rel == "To-Dos/Bremsen.md"
    assert (vault / rel).read_text(encoding="utf-8") == ""


def test_create_note_refuses_an_existing_name(vault):
    with pytest.raises(VaultPathError):
        _run(notes.create_note(USER, "To-Dos", "Einkauf"))


def test_create_folder(vault):
    assert _run(notes.create_folder(USER, "", "Reisen")) == "Reisen"
    assert (vault / "Reisen").is_dir()


def test_rename_keeps_the_md_suffix(vault):
    assert _run(notes.rename(USER, "Urlaubslog.md", "Reisetagebuch")) == "Reisetagebuch.md"
    assert (vault / "Reisetagebuch.md").exists()


def test_rename_refuses_to_overwrite(vault):
    (vault / "Zweitnotiz.md").write_text("x", encoding="utf-8")
    with pytest.raises(VaultPathError):
        _run(notes.rename(USER, "Urlaubslog.md", "Zweitnotiz"))
    assert (vault / "Zweitnotiz.md").read_text(encoding="utf-8") == "x"


def test_move_into_a_folder(vault):
    assert _run(notes.move(USER, "Urlaubslog.md", "To-Dos")) == "To-Dos/Urlaubslog.md"


def test_move_refuses_a_folder_into_itself(vault):
    with pytest.raises(VaultPathError):
        _run(notes.move(USER, "To-Dos", "To-Dos"))


def test_delete_note(vault):
    _run(notes.delete(USER, "Urlaubslog.md"))
    assert not (vault / "Urlaubslog.md").exists()


def test_delete_non_empty_folder_needs_recursive(vault):
    with pytest.raises(OSError):
        _run(notes.delete(USER, "To-Dos"))
    _run(notes.delete(USER, "To-Dos", recursive=True))
    assert not (vault / "To-Dos").exists()


def test_delete_refuses_the_vault_root(vault):
    with pytest.raises(VaultPathError):
        _run(notes.delete(USER, "", recursive=True))


# ── Pending / git ─────────────────────────────────────────────────────────────


def test_pending_is_empty_and_creates_nothing_without_a_repo(vault, tmp_path):
    assert _run(notes.pending_rel_paths(USER)) == set()
    assert not (tmp_path / "data" / "vault_git" / USER).exists()


def test_pending_lists_uncommitted_notes_without_staging(vault):
    ensure_repo(vault)
    pending = _run(notes.pending_rel_paths(USER))
    assert "Urlaubslog.md" in pending and "To-Dos/Einkauf.md" in pending
    from vault.git_wrapper import _git
    assert _git(vault, "diff", "--cached", "--name-only").strip() == ""


def test_pending_clears_after_a_commit(vault):
    ensure_repo(vault)
    from vault.git_wrapper import _git, commit
    _git(vault, "add", "-A")
    commit(vault, "test")
    assert status_porcelain(vault) == set()
