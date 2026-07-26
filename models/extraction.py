from pydantic import BaseModel


class PageImage(BaseModel):
    page_number: int
    total_pages: int
    image_bytes: bytes


class ExtractedContent(BaseModel):
    page: int
    page_text: str
    tables: list[dict]
    actions: list[dict]
    page_summary: str
    cross_references: list[dict]
    document_type: str


class JsonSidecar(BaseModel):
    paperless_id: int
    full_text: str
    full_summary: str
    full_summary_summarized: str = ""
    pages: list[dict]
    tables: list[dict]
    actions: list[dict]
    cross_refs: list[dict]
    chunks: int
    prompt_version: str
