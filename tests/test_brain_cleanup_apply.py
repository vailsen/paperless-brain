"""Dreaming's write path, end to end against a real vault.

"Dreaming" (dashboard) asks a model which memory entries to merge, shorten or
drop, then applies the accepted actions through `VaultBrainWriter`. That writer
is file-backed, so a refactor of the vault layer can break memory maintenance
without touching a line of the dream code — which is exactly what this covers.
`set_common` was removed with the old fact-card UI; these three must not follow
it.
"""

import asyncio

import pytest

from config.settings import settings
from services.brain_cleanup import CleanupAction, apply
from vault.brain_writer import VaultBrainWriter
from vault.frontmatter import read as fm_read

USER = "alice"


class FakeChroma:
    """Stands in for the brain collection: remembers what the writer stored so
    path/user lookups resolve the way the real one would."""

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}

    async def upsert(self, ids, documents=None, metadatas=None, embed_documents=None):
        for i, _id in enumerate(ids):
            self.items[_id] = {
                "document": documents[i] if documents else "",
                "metadata": dict(metadatas[i]) if metadatas else {},
            }

    async def get(self, ids=None, where=None, **kw):
        return [self.items[i] for i in (ids or []) if i in self.items]

    async def update(self, ids, documents=None, metadatas=None, embed_documents=None):
        for i, _id in enumerate(ids):
            if documents:
                self.items[_id]["document"] = documents[i]
            if metadatas:
                self.items[_id]["metadata"].update(metadatas[i])

    async def delete(self, ids=None, where=None):
        for i in ids or []:
            self.items.pop(i, None)


@pytest.fixture
def writer(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "vault_root", tmp_path, raising=False)
    monkeypatch.setattr(settings, "app_path", tmp_path, raising=False)
    return VaultBrainWriter(brain_chroma=FakeChroma())


def _run(coro):
    return asyncio.run(coro)


def _brain_dir(tmp_path):
    return tmp_path / USER / settings.brain_subfolder


def _make_fact(writer, text="Der Peugeot Bus gehört Valentin.", tags=("auto",)):
    return _run(writer.create_memory(text, list(tags), USER))


def _action(kind: str, fact_id: str, **kw) -> CleanupAction:
    """CleanupAction carries UI-only fields (display index, original text) that
    the apply path ignores; fill them so the test states only what it means."""
    return CleanupAction(
        action=kind, fact_id=fact_id, fact_idx="0", reason="test",
        original_text="Der Peugeot Bus gehört Valentin.", original_tags=["auto"], **kw
    )


def test_dream_update_rewrites_the_file(writer, tmp_path, monkeypatch):
    monkeypatch.setattr("services.clients.vault_brain_writer", writer, raising=False)
    fact_id = _make_fact(writer)
    _run(apply(
        [_action("update", fact_id, new_text="Peugeot Bus: Valentin.")],
        brain=None,
    ))
    path = next(_brain_dir(tmp_path).glob("*.md"))
    meta, body = fm_read(path)
    assert body.strip() == "Peugeot Bus: Valentin."
    assert meta["pbrain_id"] == fact_id


def test_dream_update_tags_rewrites_frontmatter(writer, tmp_path, monkeypatch):
    monkeypatch.setattr("services.clients.vault_brain_writer", writer, raising=False)
    fact_id = _make_fact(writer)
    _run(apply(
        [_action("update_tags", fact_id, new_tags=["peugeot", "bus"])],
        brain=None,
    ))
    meta, _body = fm_read(next(_brain_dir(tmp_path).glob("*.md")))
    assert meta["tags"] == ["peugeot", "bus"]


def test_dream_delete_removes_the_file_and_the_entry(writer, tmp_path, monkeypatch):
    monkeypatch.setattr("services.clients.vault_brain_writer", writer, raising=False)
    fact_id = _make_fact(writer)
    _run(apply(
        [_action("delete", fact_id)],
        brain=None,
    ))
    assert list(_brain_dir(tmp_path).glob("*.md")) == []
    assert writer._c.items == {}


def test_unselected_actions_are_not_applied(writer, tmp_path, monkeypatch):
    monkeypatch.setattr("services.clients.vault_brain_writer", writer, raising=False)
    fact_id = _make_fact(writer)
    deleted, updated = _run(apply(
        [_action("delete", fact_id, selected=False)],
        brain=None,
    ))
    assert (deleted, updated) == (0, 0)
    assert len(list(_brain_dir(tmp_path).glob("*.md"))) == 1
