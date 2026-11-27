"""Note explorer back end (docs/ux-overhaul-plan.md, Phases 4–5): managed
folders, content search, links and their rewrite after a move."""

import asyncio
import subprocess

import pytest

from vault import links, managed
from vault import search as vsearch


@pytest.fixture
def vault(tmp_path, monkeypatch):
    from config.settings import settings

    monkeypatch.setattr(settings, "vault_root", tmp_path)
    root = tmp_path / "alice"
    files = {
        "Projects/Tax 2025.md": "---\ntags: [steuer, 2025]\n---\n# Tax 2025\n\n- [ ] Belege sammeln\n"
                                "See [[Insurance]] and [[Insurance|my policy]] and [[Insurance#Kfz]].\n"
                                "Also [this](../Insurance.md).\n",
        "Insurance.md": "# Insurance\n\nKfz-Versicherung bei der Müller AG.\n#kfz #vertrag\n",
        "Archive/Insurance.md": "old copy\n",
        "Daily/Note.md": "Back to [[Projects/Tax 2025]].\n```\n[[Insurance]] in code is still a link in Obsidian\n```\n",
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


# ── Managed folders ──────────────────────────────────────────────────────────

def test_managed_roots_and_locks():
    from config.settings import settings

    mem, memos = settings.brain_subfolder, settings.memo_subfolder
    assert managed.managed_root(f"{mem}/fact.md") == "memory"
    assert managed.is_locked(f"{mem}/fact.md")
    assert managed.managed_root(f"{memos}/26-11-20 x.md") == "memos"
    assert not managed.is_locked(f"{memos}/26-11-20 x.md")  # memos edit freely
    assert managed.is_root(memos) and managed.is_root(mem)
    assert not managed.is_root(f"{mem}/sub")
    assert managed.managed_root("Projects/x.md") is None
    assert managed.managed_root("PaperlessBrain Findings/Dossiers/a.md") == "findings"


# ── Search ───────────────────────────────────────────────────────────────────

def test_search_words_accents_and_snippets(vault):
    hits = vsearch.search("alice", "muller")
    assert [h.rel for h in hits] == ["Insurance.md"]
    assert hits[0].lines == [(3, "Kfz-Versicherung bei der Müller AG.")]


def test_search_name_hits_first_and_exclusion(vault):
    hits = vsearch.search("alice", "insurance")
    assert hits[0].name_hit and hits[0].rel in ("Insurance.md", "Archive/Insurance.md")
    assert "Archive/Insurance.md" not in [h.rel for h in vsearch.search("alice", "insurance -old")]


def test_search_tags_paths_and_phrases(vault):
    assert [h.rel for h in vsearch.search("alice", "tag:steuer")] == ["Projects/Tax 2025.md"]
    assert [h.rel for h in vsearch.search("alice", "tag:kfz")] == ["Insurance.md"]
    assert [h.rel for h in vsearch.search("alice", "path:daily back")] == ["Daily/Note.md"]
    assert vsearch.search("alice", '"belege sammeln"')[0].rel == "Projects/Tax 2025.md"
    assert vsearch.search("alice", '"sammeln belege"') == []
    assert "steuer" in vsearch.all_tags("alice") and "kfz" in vsearch.all_tags("alice")


# ── Links ────────────────────────────────────────────────────────────────────

def test_resolve_follows_obsidian():
    paths = ["Insurance.md", "Archive/Insurance.md", "Projects/Tax 2025.md"]
    assert links.resolve("Insurance", paths) == "Insurance.md"  # the shortest path
    assert links.resolve("Archive/Insurance", paths) == "Archive/Insurance.md"
    assert links.resolve("tax 2025", paths) == "Projects/Tax 2025.md"
    assert links.resolve("Nope", paths) is None


def test_backlinks(vault):
    back = links.backlinks("alice", "Insurance.md")
    assert {(r, n) for r, n, _t in back} >= {("Projects/Tax 2025.md", 7), ("Projects/Tax 2025.md", 8)}
    assert [r for r, _n, _t in links.backlinks("alice", "Projects/Tax 2025.md")] == ["Daily/Note.md"]


def test_rename_rewrites_links_keeping_label_and_heading(vault, monkeypatch):
    from vault import notes

    old = links.note_paths("alice")
    new_rel = asyncio.run(notes.rename("alice", "Insurance.md", "Versicherung"))
    assert new_rel == "Versicherung.md"
    n = asyncio.run(links.rewrite_after_move("alice", links.moved_map(old, "Insurance.md", new_rel), old))
    text = (vault / "Projects/Tax 2025.md").read_text()
    assert "[[Versicherung]] and [[Versicherung|my policy]] and [[Versicherung#Kfz]]" in text
    assert "[this](../Versicherung.md)" in text
    assert n >= 1
    # [[Insurance]] elsewhere resolved to the renamed note (the shortest path
    # of the two named so) and follows it; the archive copy is untouched.
    assert "[[Versicherung]]" in (vault / "Daily/Note.md").read_text()
    assert (vault / "Archive/Insurance.md").read_text() == "old copy\n"


def test_moving_a_folder_rewrites_links_into_it_and_out_of_it(vault):
    from vault import notes

    old = links.note_paths("alice")
    asyncio.run(notes.create_folder("alice", "", "Work"))
    new_rel = asyncio.run(notes.move("alice", "Projects", "Work"))
    n = asyncio.run(links.rewrite_after_move("alice", links.moved_map(old, "Projects", new_rel), old))
    assert "[[Work/Projects/Tax 2025]]" in (vault / "Daily/Note.md").read_text()
    # The moved note's own relative link now climbs one more folder.
    assert "[this](../../Insurance.md)" in (vault / "Work/Projects/Tax 2025.md").read_text()
    assert n == 2


def test_an_untouched_note_keeps_its_bytes(vault):
    old = links.note_paths("alice")
    idx = links.build_index(old)
    text = (vault / "Projects/Tax 2025.md").read_text()
    assert links.rewrite_text(text, "Projects/Tax 2025.md", {"Other.md": "Else.md"}, idx, idx) == text
