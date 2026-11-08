"""werkbank/export.py — `upload_pdf()`: a styled PDF from Markdown, uploaded to
Paperless. Used by the chat page for `generate_chat_pdf` tool results."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime

from config.settings import local_tz
from services.pdf_generator import generate_chat_pdf  # used by upload_pdf()

_FILENAME_UNSAFE = re.compile(r"[^\w\s\-]")


async def upload_pdf(
    content_markdown: str,
    title: str,
    username: str,
    model_name: str,
    filename_slug: str,
    paperless_client,
    *,
    filename_suffix: str = "AI_GENERATED",
) -> tuple[str | None, str]:
    """Generate a styled PDF from markdown and upload it to Paperless.

    Returns:
        (upload_task_id_or_ok, filename) — upload_task_id may be None if the
        client returns no task token (e.g. older Paperless versions).
    """
    from werkbank.settings_store import (
        get_ingest_correspondent,
        get_ingest_doc_type,
        get_tag_ai_generated,
        get_tag_inbox,
    )

    dt = datetime.now(tz=local_tz())
    pdf_bytes = await asyncio.to_thread(
        generate_chat_pdf, content_markdown, title, username, model_name, dt
    )
    slug_clean = _FILENAME_UNSAFE.sub("", filename_slug[:30]).strip().replace(" ", "_") or "Document"
    filename = f"{dt.strftime('%Y%m%d')}_{slug_clean}_{filename_suffix}.pdf"

    upload_task_id = await paperless_client.upload_document(
        pdf_bytes=pdf_bytes,
        filename=filename,
        title=title,
        tag_names=[get_tag_inbox(), get_tag_ai_generated()],
        correspondent_name=get_ingest_correspondent(),
        document_type_name=get_ingest_doc_type(),
    )
    return upload_task_id, filename
