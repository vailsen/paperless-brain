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
    # Server-side download URL, built from PAPERLESS_URL. Usable from the
    # backend only — handing it to a browser breaks behind a reverse proxy
    # (internal address, and no Paperless session on that origin). The UI
    # downloads via PaperlessClient.download_document_named() instead.
    pdf_url: str
