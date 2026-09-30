"""`BrainService.get_shared_by_others` — the listing behind the note explorer's
read-only "Shared by others" section."""

import asyncio

from services.brain_service import BrainService


class _FakeChroma:
    def __init__(self, items):
        self.items = items
        self.where = None

    async def count(self):
        return len(self.items)

    async def get(self, where=None, **_):
        self.where = where
        return self.items


def _item(fid, user, common=True, path="PaperSage Memory/x.md", text="fact"):
    return {
        "id": fid,
        "document": text,
        "metadata": {"user": user, "common": common, "path": path, "updated": "2026-09-01"},
    }


def test_asks_the_index_for_other_users_common_facts_only():
    chroma = _FakeChroma([])
    asyncio.run(BrainService(chroma).get_shared_by_others("alice"))
    # count() == 0 short-circuits, so seed one item to see the filter.
    chroma.items = [_item("1", "bob")]
    asyncio.run(BrainService(chroma).get_shared_by_others("alice"))
    assert chroma.where == {
        "$and": [{"common": {"$eq": True}}, {"user": {"$ne": "alice"}}]
    }


def test_drops_own_ownerless_and_empty_entries_and_sorts():
    chroma = _FakeChroma([
        _item("1", "carol", path="PaperSage Memory/b.md"),
        _item("2", "alice"),                      # own fact — never listed
        _item("3", ""),                           # no owner
        _item("4", "bob", text=""),               # nothing to show
        _item("5", "bob", path="PaperSage Memory/a.md"),
    ])
    out = asyncio.run(BrainService(chroma).get_shared_by_others("alice"))
    assert [(e["user"], e["id"]) for e in out] == [("bob", "5"), ("carol", "1")]
    assert out[0]["path"] == "PaperSage Memory/a.md"


def test_index_failure_yields_empty_list():
    class _Broken(_FakeChroma):
        async def get(self, **_):
            raise RuntimeError("chroma down")

    assert asyncio.run(BrainService(_Broken([_item("1", "bob")])).get_shared_by_others("a")) == []
