"""werkbank/export.py — exports a completed task's result to Paperless as PDF.

Called by the UI after the user approves AWAITING_REVIEW content.

Flow:
  assemble_markdown()  →  upload_pdf()  →  poll_task_result()
  →  repository.update_task_result()  →  COMPLETED

`upload_pdf()` is also used by the chat page for generate_chat_pdf tool results.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime

from config.settings import local_tz, settings
from i18n import format_datetime
from services.pdf_generator import generate_chat_pdf  # used by upload_pdf()
from werkbank import repository
from werkbank.models import TaskStatus

_FILENAME_UNSAFE = re.compile(r"[^\w\s\-]")

# Header labels are archive-level (the PDF lands in the shared Paperless
# archive), so they follow ARCHIVE_LANGUAGE, not the per-user UI language.
_HEADER_LABELS = {
    "en": {"created": "Created", "model": "Model", "subtasks": "Subtasks",
           "request": "Request", "default_title": "AI Workbench Result"},
    "de": {"created": "Erstellt", "model": "Modell", "subtasks": "Teilaufgaben",
           "request": "Auftrag", "default_title": "KI-Werkbank Ergebnis"},
}


def _sanitize(text: str, max_len: int = 40) -> str:
    """Return a filesystem-safe slug from text."""
    slug = _FILENAME_UNSAFE.sub("", text[:max_len]).strip().replace(" ", "_")
    return slug or "Werkbank"


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

    Shared by the werkbank export flow and the chat generate_chat_pdf tool.

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


def _build_title(task: repository.Task) -> str:
    """Derive a human-readable document title from the task goal."""
    if getattr(task, "short_title", None):
        return task.short_title
    goal = (task.refined_request or task.original_request or "").strip()
    first_line = goal.split("\n")[0][:60].rstrip(".")
    if first_line:
        return first_line
    labels = _HEADER_LABELS.get(settings.archive_language, _HEADER_LABELS["en"])
    return labels["default_title"]


def assemble_markdown(task: repository.Task, subtasks: list[repository.SubTask]) -> str:
    """Prepend a metadata header to result_md for the PDF export."""
    lang = settings.archive_language
    labels = _HEADER_LABELS.get(lang, _HEADER_LABELS["en"])
    dt_str = format_datetime(datetime.now(tz=local_tz()), lang)
    done = sum(1 for s in subtasks if s.status.value == "DONE")
    total = len(subtasks)

    header = (
        f"# {_build_title(task)}\n\n"
        f"_{labels['created']}: {dt_str} · {labels['model']}: {task.model} · "
        f"{labels['subtasks']}: {done}/{total}_\n\n"
        f"**{labels['request']}:** {task.refined_request or task.original_request}\n\n"
        "---\n\n"
    )
    return header + (task.result_md or "")


async def export_to_paperless(
    task_id: int,
    user_id: str,
    token: str = "",
) -> tuple[int | None, str | None]:
    """Export task result to Paperless as a styled PDF.

    Args:
        task_id:  agent_tasks.id
        user_id:  Paperless username (used for PDF metadata and DB scoping).

    Returns:
        (paperless_doc_id, paperless_url) — either may be None if upload or
        polling fails. The task row is updated regardless of polling outcome.

    Raises:
        ValueError: if the task has no result_md or does not exist.
    """
    from services.clients import paperless as _admin_paperless
    from services.paperless import PaperlessClient

    # Use user's own token so document is owned by the correct Paperless user.
    paperless = PaperlessClient(settings.paperless_url, token) if token else _admin_paperless

    task = repository.get_task(task_id, user_id)
    if not task:
        raise ValueError(f"Task {task_id} not found for user {user_id}")
    if not task.result_md:
        raise ValueError(f"Task {task_id} has no result_md — cannot export")

    subtasks = repository.get_subtasks(task_id, user_id)
    title = _build_title(task)
    final_md = assemble_markdown(task, subtasks)

    # ── PDF generation + upload ───────────────────────────────────────
    upload_task_id, _filename = await upload_pdf(
        content_markdown=final_md,
        title=title,
        username=user_id,
        model_name=task.model,
        filename_slug=_sanitize(title),
        paperless_client=paperless,
        filename_suffix="Werkbank_AI",
    )

    # ── Poll for document ID ──────────────────────────────────────────
    doc_id: int | None = None
    doc_url: str | None = None

    if upload_task_id and upload_task_id != "ok":
        doc_id = await paperless.poll_task_result(upload_task_id, timeout=90)

    if doc_id:
        doc_url = f"{settings.paperless_url.rstrip('/')}/documents/{doc_id}/details/"

    # ── Persist + mark COMPLETED ──────────────────────────────────────
    repository.update_task_result(task_id, user_id, task.result_md, doc_id, doc_url)
    repository.update_task_status(task_id, user_id, TaskStatus.COMPLETED)

    return doc_id, doc_url
