"""The archive hint on web searches only quotes documents the user may read.

The index holds every user's documents with no owner field. The hint used to
quote the nearest chunks into any user's web search — snippets of other users'
private documents included. Paperless, asked with the user's own token, decides.
"""

import asyncio
from types import SimpleNamespace

from services import chat_service

HITS = [[
    {"metadata": {"paperless_id": 1}, "document": "Mietvertrag Wohnung Alice", "distance": 0.1},
    {"metadata": {"paperless_id": 2}, "document": "Kontoauszug Bob geheim", "distance": 0.1},
]]


class _Chroma:
    async def query(self, **_):
        return HITS


class _UserPaperless:
    def __init__(self, readable):
        self.readable = set(readable)
        self.asked = None

    async def list_documents(self, ids=None, **_):
        self.asked = ids
        return [SimpleNamespace(id=i) for i in ids if i in self.readable]


def test_only_readable_documents_are_quoted(monkeypatch):
    user = _UserPaperless(readable={1})
    monkeypatch.setattr(chat_service, "chroma", _Chroma())
    monkeypatch.setattr(chat_service, "_user_paperless", lambda: user)
    hint = asyncio.run(chat_service._web_search_archive_hint("Mietvertrag"))
    assert "#1" in hint and "Alice" in hint
    assert "#2" not in hint and "Bob" not in hint
    assert user.asked == [1, 2]


def test_nothing_readable_means_no_hint(monkeypatch):
    monkeypatch.setattr(chat_service, "chroma", _Chroma())
    monkeypatch.setattr(chat_service, "_user_paperless", lambda: _UserPaperless(readable=set()))
    assert asyncio.run(chat_service._web_search_archive_hint("x")) == ""


def test_no_user_context_means_no_hint(monkeypatch):
    def _no_user():
        raise chat_service.NoUserContext

    monkeypatch.setattr(chat_service, "chroma", _Chroma())
    monkeypatch.setattr(chat_service, "_user_paperless", _no_user)
    assert asyncio.run(chat_service._web_search_archive_hint("x")) == ""
