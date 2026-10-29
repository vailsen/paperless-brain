"""Proactive Phase 9: findings and dossiers in retrieval — filtered, marked,
never evidence."""

import asyncio
from pathlib import Path

import pytest

FINDING = """---
pbrain_id: 11111111-1111-1111-1111-111111111111
finding_id: abcdefg
kind: insight
title: Riester-Kosten prüfen
status: {status}
topic: riester
doc_ids: [5]
---
## Summary

Die Verwaltungskosten des Riester-Vertrags sind hoch.
"""

DOSSIER = """---
pbrain_id: 22222222-2222-2222-2222-222222222222
topic: bausparvertrag
doc_ids: [10, 11]
---
## Current state

Guthaben 12.400 EUR.
"""


class FakeChroma:
    def __init__(self):
        self.upserts, self.deletes = [], []

    async def delete(self, where):
        self.deletes.append(where)

    async def upsert(self, **kw):
        self.upserts.append(kw)


@pytest.fixture
def vault(proactive_on, tmp_path, monkeypatch):
    from config.settings import settings

    root = tmp_path / "vaults"
    base = root / "alice" / "PaperlessBrain Findings"
    (base / "Notices").mkdir(parents=True)
    (base / "Dossiers").mkdir(parents=True)
    monkeypatch.setattr(settings, "vault_root", root)
    return root / "alice"


def _embed(vault, rel):
    from vault import sync

    chroma = FakeChroma()
    ok = asyncio.run(sync._embed_file("alice", vault, Path(rel), FakeChroma(), chroma))
    assert ok
    return chroma


@pytest.mark.parametrize("status,indexed", [
    ("surfaced", True), ("interested", True), ("researched", True), ("done", True),
    ("new", False), ("held", False), ("snoozed", False), ("dismissed", False),
])
def test_only_surfaced_kinds_of_findings_are_indexed(vault, status, indexed):
    rel = "PaperlessBrain Findings/Notices/x.md"
    p = vault / rel
    p.write_text(FINDING.format(status=status))
    before = p.read_text()
    chroma = _embed(vault, rel)
    assert chroma.deletes  # old chunks always go first
    assert bool(chroma.upserts) is indexed
    assert p.read_text() == before  # never written to by the sync
    if indexed:
        meta = chroma.upserts[0]["metadatas"][0]
        assert meta["source_type"] == "finding" and meta["trust"] == "low"
        assert meta["user"] == "alice" and meta["finding_status"] == status


def test_dismissing_in_obsidian_removes_it_from_the_index(vault):
    rel = "PaperlessBrain Findings/Notices/x.md"
    p = vault / rel
    p.write_text(FINDING.format(status="surfaced"))
    assert _embed(vault, rel).upserts
    p.write_text(FINDING.format(status="dismissed"))
    chroma = _embed(vault, rel)
    assert chroma.upserts == [] and chroma.deletes == [{"path": {"$eq": rel}}]


def test_dossiers_indexed_interests_never(vault):
    (vault / "PaperlessBrain Findings/Dossiers/bausparvertrag.md").write_text(DOSSIER)
    meta = _embed(vault, "PaperlessBrain Findings/Dossiers/bausparvertrag.md").upserts[0]["metadatas"][0]
    assert meta["source_type"] == "dossier" and meta["trust"] == "low"
    (vault / "PaperlessBrain Findings/_interests.md").write_text("---\nmuted: []\n---\n| topic |\n")
    assert _embed(vault, "PaperlessBrain Findings/_interests.md").upserts == []


def test_nothing_is_indexed_while_disabled(vault, monkeypatch):
    from config import proactive as cfgmod

    rel = "PaperlessBrain Findings/Notices/x.md"
    (vault / rel).write_text(FINDING.format(status="surfaced"))
    monkeypatch.setenv("PROACTIVE_ENABLED", "false")
    cfgmod.proactive_settings.cache_clear()
    assert _embed(vault, rel).upserts == []


def test_hits_are_marked_as_not_evidence():
    from services.chat_service import _finding_mark

    assert "finding — not evidence" in _finding_mark("PaperlessBrain Findings/Notices/x.md")
    assert "dossier — not evidence" in _finding_mark("PaperlessBrain Findings/Dossiers/b.md")
    assert _finding_mark("To-Dos/Bremsen.md") == ""


def test_prompt_rule_only_when_enabled(proactive_on, monkeypatch):
    from config import proactive as cfgmod
    from config.chat_prompts import FINDINGS_RULE, build_system_prompt

    assert FINDINGS_RULE in build_system_prompt()
    monkeypatch.setenv("PROACTIVE_ENABLED", "false")
    cfgmod.proactive_settings.cache_clear()
    assert FINDINGS_RULE not in build_system_prompt()


def test_retrieval_queries_are_user_scoped():
    """Every vault query in the chat filters by the user — findings included."""
    src = (Path(__file__).resolve().parents[1] / "services" / "chat_service.py").read_text()
    queries = src.count("vault_chroma.query(")
    assert queries >= 3
    assert src.count('where={"user": {"$eq": ') >= queries
