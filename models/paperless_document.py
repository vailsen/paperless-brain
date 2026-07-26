from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from models.paperless_note import PaperlessNote


class PaperlessDocument(BaseModel):
    id: int
    correspondent: Optional[str] = None
    document_type: Optional[str] = None
    original_file_name: str
    owner: Optional[int] = None
    owner_name: Optional[str] = None
    notes: list[PaperlessNote]
    page_count: Optional[int] = None
    mime_type: str
    title: str
    tags: list[str]
    added: datetime
    created: datetime
    modified: datetime
    content: Optional[str] = None
    pdf_url: str
