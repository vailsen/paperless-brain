# models/result_document.py
from pathlib import Path

from pydantic import BaseModel

from config.settings import settings
from models.paperless_document import PaperlessDocument


class DocumentResult(BaseModel):
    document: PaperlessDocument
    relevance_score: float | None = None  # from ChromaDB, None for API searches
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

    def is_semantic_result(self) -> bool:
        return self.relevance_score is not None
