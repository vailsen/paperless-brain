# services/brain_service.py
"""Persistent long-term memory — stores and retrieves BrainFacts via ChromaDB."""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from services.chroma import ChromaClient


@dataclass
class BrainFact:
    id: str
    text: str
    source_doc_id: int | None
    source_page: int | None
    confidence: float
    created_at: datetime
    tags: list[str]
    user: str   # paperless username
    common: bool
    kind: str = "fact"   # "fact" | "deadline"
    due: str = ""        # YYYY-MM-DD, only for kind="deadline"


def _parse_fact(item: dict) -> BrainFact | None:
    doc = item.get("document")
    m = item.get("metadata") or {}
    if not doc:
        return None
    try:
        return BrainFact(
            id=item["id"],
            text=doc,
            source_doc_id=m.get("source_doc_id") or None,
            source_page=m.get("source_page") or None,
            confidence=float(m.get("confidence", 1.0)),
            created_at=datetime.fromisoformat(
                m.get("created_at", datetime.utcnow().isoformat())
            ),
            tags=json.loads(m.get("tags", "[]")),
            user=m.get("user", ""),
            common=bool(m.get("common", False)),
            kind=m.get("kind", "fact"),
            due=m.get("due", "") or "",
        )
    except Exception:
        return None


class BrainService:
    def __init__(self, chroma: ChromaClient):
        self._chroma = chroma

    async def remember(
        self,
        text: str,
        tags: list[str],
        user: str,
        source_doc_id: int | None = None,
        source_page: int | None = None,
        confidence: float = 1.0,
    ) -> str:
        fact_id = str(uuid.uuid4())
        await self._chroma.add(
            documents=[text],
            ids=[fact_id],
            metadatas=[{
                "user": user,
                "common": False,
                "source_doc_id": source_doc_id or 0,
                "source_page": source_page or 0,
                "confidence": max(0.0, min(1.0, confidence)),
                "created_at": datetime.utcnow().isoformat(),
                "tags": json.dumps(tags or []),
            }],
        )
        return fact_id

    async def search(
        self, query: str, user: str, max_results: int = 5
    ) -> list[BrainFact]:
        try:
            total = await self._chroma.count()
            if total == 0:
                return []
            where = {
                "$or": [{"user": {"$eq": user}}, {"common": {"$eq": True}}]
            }
            hits = await self._chroma.query(
                query_texts=[query],
                n_results=min(max_results, total),
                where=where,
            )
        except Exception:
            return []
        return [f for h in hits[0] if (f := _parse_fact(h)) is not None]

    async def search_hints(
        self, query: str, user: str, max_results: int = 5
    ) -> list[tuple[str, str, float]]:
        """Return (fact_id, text, distance) triples. Lower distance = better match (cosine metric)."""
        try:
            total = await self._chroma.count()
            if total == 0:
                return []
            where = {"$or": [{"user": {"$eq": user}}, {"common": {"$eq": True}}]}
            hits = await self._chroma.query(
                query_texts=[query],
                n_results=min(max_results, total),
                where=where,
            )
        except Exception:
            return []
        out: list[tuple[str, str, float]] = []
        for h in hits[0]:
            doc = h.get("document")
            if not doc:
                continue
            m = h.get("metadata") or {}
            # Deadlines keep the due date in metadata, not the body — append it
            # so callers (search hints, dedup) see "bis wann".
            if m.get("kind") == "deadline" and m.get("due"):
                doc = f"{doc} (Frist: {m['due']})"
            out.append((h["id"], doc, h["distance"]))
        return out

    async def get_all(self, user: str) -> list[BrainFact]:
        """Return all real facts visible to user (own + common). For management page.

        Deadlines (kind="deadline") are excluded — they have a dedicated section.
        """
        try:
            total = await self._chroma.count()
            if total == 0:
                return []
            items = await self._chroma.get(
                where={"$or": [{"user": {"$eq": user}}, {"common": {"$eq": True}}]}
            )
        except Exception:
            return []
        facts = [f for i in items if (f := _parse_fact(i)) is not None]
        return [f for f in facts if f.kind != "deadline"]

    async def get_deadlines(self, user: str) -> list[BrainFact]:
        """Return the user's manual due-dates (kind="deadline"), sorted by due date."""
        try:
            total = await self._chroma.count()
            if total == 0:
                return []
            items = await self._chroma.get(where={"user": {"$eq": user}})
        except Exception:
            return []
        facts = [f for i in items if (f := _parse_fact(i)) is not None]
        deadlines = [f for f in facts if f.kind == "deadline"]
        deadlines.sort(key=lambda f: f.due or "9999-12-31")
        return deadlines

    async def delete(self, fact_id: str) -> None:
        await self._chroma.delete(ids=[fact_id])

    async def set_common(self, fact_id: str, common: bool) -> None:
        await self._chroma.update(ids=[fact_id], metadatas=[{"common": common}])

    async def update_text(self, fact_id: str, text: str) -> None:
        await self._chroma.update(ids=[fact_id], documents=[text])

    async def update_tags(self, fact_id: str, tags: list[str]) -> None:
        await self._chroma.update(ids=[fact_id], metadatas=[{"tags": json.dumps(tags)}])
