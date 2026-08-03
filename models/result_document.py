# models/result_document.py
from pathlib import Path

from pydantic import BaseModel

from config.settings import settings
from models.paperless_document import PaperlessDocument


class DocumentResult(BaseModel):
    document: PaperlessDocument
    # ChromaDB cosine *distance* — lower is better. None for API searches.
    # Do not show this to a user as-is: sorted ascending under a "Relevance"
    # heading it reads as "least relevant first". Use `relevance` instead.
    relevance_score: float | None = None
    matched_chunks: list[str] = []  # snippet previews for semantic hits
    has_actions: bool = False  # from sidecar, pre-resolved
    text_query: str = ""  # Titel/Inhalt input — for highlighting in full_text

    @property
    def thumbnail_path(self) -> str:
        return str(
            Path(settings.app_path) / settings.thumb_path / f"{self.document.id}.jpg"
        )

    @property
    def display_title(self) -> str:
        return self.document.title or f"Document {self.document.id}"

    @property
    def display_date(self) -> str | None:
        d = self.document.created
        return d.strftime("%d.%m.%Y") if d else None

    @property
    def relevance(self) -> float | None:
        """Cosine distance turned into relevance: 1.0 = perfect match, 0.0 = none.

        Chroma's cosine distance is 1 - similarity, so it spans 0..2 and a
        negative relevance is possible in principle; clamped because a "-0.03"
        on a card is noise, not information.
        """
        if self.relevance_score is None:
            return None
        return max(0.0, min(1.0, 1.0 - self.relevance_score))

    def is_semantic_result(self) -> bool:
        return self.relevance_score is not None
