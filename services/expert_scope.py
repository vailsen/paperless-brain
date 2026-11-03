"""What part of the archive an expert sees (docs/experts.md §5.2).

A scope is opt-in per dimension: an empty dimension does not restrict, filled
dimensions are AND-combined, values within one are OR-combined. A document
without a correspondent is outside a scope that lists correspondents.

Each dimension can also exclude ("never these"): a document whose
correspondent or type is excluded, or that carries any excluded tag, is out —
whatever the opt-in part says. Exclusions alone narrow an otherwise full view.

The scope is a focus, not a security boundary — that stays the user's own
Paperless token. It is still enforced here, in the client every document tool
goes through (`services.chat_service._user_paperless`), never in the prompt:
an expert told "insurance only" that can still search everything drifts, and
an answer citing a document outside its scope is quiet untrustworthiness.

Out of scope looks exactly like "no such document": `list_documents` drops
it, `get_document` raises the same IndexError an unknown id raises.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from services.paperless import PaperlessClient


@dataclass(frozen=True)
class Scope:
    correspondents: frozenset[int] = field(default_factory=frozenset)
    tags: frozenset[int] = field(default_factory=frozenset)
    document_types: frozenset[int] = field(default_factory=frozenset)
    not_correspondents: frozenset[int] = field(default_factory=frozenset)
    not_tags: frozenset[int] = field(default_factory=frozenset)
    not_document_types: frozenset[int] = field(default_factory=frozenset)

    @classmethod
    def from_meta(cls, meta: dict | None) -> "Scope":
        """From an expert file's `scope:` mapping. Entries are `{id, name}`
        dicts or bare ids; anything without an integer id is ignored."""
        meta = meta or {}

        def ids(key: str) -> frozenset[int]:
            out = set()
            for item in meta.get(key) or []:
                raw = item.get("id") if isinstance(item, dict) else item
                try:
                    out.add(int(raw))
                except (TypeError, ValueError):
                    continue
            return frozenset(out)

        return cls(ids("correspondents"), ids("tags"), ids("document_types"),
                   ids("exclude_correspondents"), ids("exclude_tags"), ids("exclude_document_types"))

    @property
    def unrestricted(self) -> bool:
        return not (self.correspondents or self.tags or self.document_types
                    or self.not_correspondents or self.not_tags or self.not_document_types)

    def allows(self, doc) -> bool:
        """`doc` is a PaperlessDocument (ids from `_parse_document`)."""
        if self.correspondents and getattr(doc, "correspondent_id", None) not in self.correspondents:
            return False
        if self.document_types and getattr(doc, "document_type_id", None) not in self.document_types:
            return False
        tag_ids = set(getattr(doc, "tag_ids", None) or [])
        if self.tags and not (tag_ids & self.tags):
            return False
        if self.not_correspondents and getattr(doc, "correspondent_id", None) in self.not_correspondents:
            return False
        if self.not_document_types and getattr(doc, "document_type_id", None) in self.not_document_types:
            return False
        if self.not_tags and tag_ids & self.not_tags:
            return False
        return True


def _narrow(asked: list[int] | None, allowed: frozenset[int]) -> list[int] | None:
    """Caller filter ∩ scope. None = no filter; [] = nothing can match."""
    if not allowed:
        return asked
    if asked is None:
        return sorted(allowed)
    return [x for x in asked if x in allowed]


class ScopedPaperlessClient(PaperlessClient):
    """The user's Paperless client, narrowed to an expert's scope."""

    def __init__(self, base_url: str, token: str, scope: Scope):
        super().__init__(base_url, token)
        self.scope = scope

    async def list_documents(self, *args, **kwargs):
        if self.scope.unrestricted:
            return await super().list_documents(*args, **kwargs)
        corr = _narrow(kwargs.get("correspondent"), self.scope.correspondents)
        types = _narrow(kwargs.get("document_type"), self.scope.document_types)
        if corr == [] or types == []:
            return []
        kwargs["correspondent"] = corr
        kwargs["document_type"] = types
        # tags: the caller's any-of (tag_ids_any) and all-of (tags) semantics
        # cannot be combined with the scope's any-of in one Paperless query.
        # Server-side only when the caller asked for no tag any-of itself.
        if self.scope.tags and not kwargs.get("tag_ids_any"):
            kwargs["tag_ids_any"] = sorted(self.scope.tags)
        docs = await super().list_documents(*args, **kwargs)
        # Re-checked in code: Paperless answers an unknown filter as if
        # unfiltered, and nothing here may rest on that not happening.
        return [d for d in docs if self.scope.allows(d)]

    async def _require(self, doc_id: int) -> None:
        if not self.scope.unrestricted:
            await self.get_document(doc_id)  # IndexError when out of scope

    async def download_document(self, doc_id: int) -> bytes:
        await self._require(doc_id)
        return await super().download_document(doc_id)

    async def download_document_named(self, doc_id: int) -> tuple[bytes, str]:
        await self._require(doc_id)
        return await super().download_document_named(doc_id)

    async def get_thumbnail(self, doc_id: int) -> bytes:
        await self._require(doc_id)
        return await super().get_thumbnail(doc_id)

    async def create_note(self, doc_id: int, note: str) -> str:
        await self._require(doc_id)
        return await super().create_note(doc_id, note)


def scoped_client(base_url: str, token: str, scope: Scope | None) -> PaperlessClient:
    if scope is None or scope.unrestricted:
        return PaperlessClient(base_url, token)
    return ScopedPaperlessClient(base_url, token, scope)
