"""Permission scoping for actions and for the user-scoped Paperless client.

`index.json` is built by the superuser sync and therefore aggregates the actions
of every document in the archive, across all owners. The deadline descriptions in
it are extracted document content, so anything that renders them — the
`get_actions` chat tool and the dashboard — must first drop the ones whose source
document the current user cannot read.

The second half covers the client that performs that check: it must fail closed.
Falling back to the superuser client when no session token is in scope would make
every permission check in the chat tools pass unconditionally.
"""

import asyncio
from dataclasses import dataclass

import pytest

from services.sidecar_service import _ID_BATCH, filter_visible_actions


@dataclass
class _Doc:
    id: int


class _FakePaperless:
    """Stands in for a token-scoped PaperlessClient.

    `visible` is what this user's token is allowed to see; anything else is
    silently absent from the response, exactly as Paperless-ngx behaves.
    """

    def __init__(self, visible: set[int], fail: bool = False):
        self.visible = visible
        self.fail = fail
        self.batches: list[list[int]] = []

    async def list_documents(self, ids=None, **_):
        self.batches.append(list(ids or []))
        if self.fail:
            raise RuntimeError("paperless unreachable")
        return [_Doc(i) for i in (ids or []) if i in self.visible]


def _actions(*ids: int) -> list[dict]:
    return [
        {"paperless_id": i, "deadline": "2026-01-01", "description": f"pay bill {i}"}
        for i in ids
    ]


def test_drops_actions_of_foreign_documents():
    pl = _FakePaperless(visible={1, 3})
    kept = asyncio.run(filter_visible_actions(_actions(1, 2, 3), pl))
    assert [a["paperless_id"] for a in kept] == [1, 3]


def test_foreign_description_never_survives():
    """The leak was the description text, not the id — assert on the text."""
    pl = _FakePaperless(visible=set())
    kept = asyncio.run(filter_visible_actions(_actions(7), pl))
    assert kept == []


def test_api_error_fails_closed():
    """An unreachable Paperless must hide deadlines, not show all of them."""
    pl = _FakePaperless(visible={1, 2}, fail=True)
    assert asyncio.run(filter_visible_actions(_actions(1, 2), pl)) == []


def test_ids_are_batched():
    """A whole archive of ids in one URL gets rejected before Paperless sees it."""
    ids = list(range(1, _ID_BATCH * 2 + 5))
    pl = _FakePaperless(visible=set(ids))
    kept = asyncio.run(filter_visible_actions(_actions(*ids), pl))
    assert len(kept) == len(ids)
    assert len(pl.batches) == 3
    assert all(len(b) <= _ID_BATCH for b in pl.batches)


def test_actions_without_document_are_untouched():
    """Manually created deadlines carry no paperless_id and need no check."""
    manual = [{"paperless_id": None, "description": "call the landlord"}]
    pl = _FakePaperless(visible=set())
    assert asyncio.run(filter_visible_actions(manual, pl)) == manual


# ── The client that backs every permission check ──────────────────────────────


def test_user_paperless_fails_closed_without_token():
    from services.chat_service import NoUserContext, _current_token, _user_paperless

    token = _current_token.set(None)
    try:
        with pytest.raises(NoUserContext):
            _user_paperless()
    finally:
        _current_token.reset(token)


def test_user_paperless_uses_the_session_token():
    from services.chat_service import _current_token, _user_paperless

    token = _current_token.set("user-token")
    try:
        client = _user_paperless()
    finally:
        _current_token.reset(token)
    assert "user-token" in str(client.headers)


def test_execute_tool_reports_missing_session_instead_of_raising():
    from services.chat_service import _current_token, execute_tool

    token = _current_token.set(None)
    try:
        text, docs, extras = asyncio.run(
            execute_tool("get_document_details", {"document_id": 1})
        )
    finally:
        _current_token.reset(token)
    assert "sign in" in text.lower()
    assert docs == [] and extras == []
