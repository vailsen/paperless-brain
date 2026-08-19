# services/chat_service.py

from __future__ import annotations

import asyncio
import contextvars
import html as _html_lib
import json
import logging
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncGenerator

import httpx

from config.settings import settings
from models.brain_fact_result import BrainFactResult
from models.result_document import DocumentResult
from models.vault_note_result import VaultNoteResult
from models.web_result import WebSearchResult
from pipelines.search import search
from services.clients import chroma, cross_ref_index, paperless, sidecar_service, vision
from services.ollama_watchdog import touch as _wol_touch
from services.paperless import PaperlessClient as _PaperlessClient

_log = logging.getLogger(__name__)

# Set at the start of each chat turn to scope searches to the logged-in user.
_current_owner: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "owner_username", default=None
)
_current_token: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "user_token", default=None
)
# "hybrid" = trafilatura first, crawl4ai fallback | "crawl4ai" = crawl4ai only
_web_fetch_mode: contextvars.ContextVar[str] = contextvars.ContextVar(
    "web_fetch_mode", default="hybrid"
)

_PDF_CACHE = str(settings.app_path / "data" / "pdf_cache")
os.makedirs(_PDF_CACHE, exist_ok=True)


class NoUserContext(Exception):
    """No session token in scope — every document tool must refuse."""


def _user_paperless() -> _PaperlessClient:
    """Per-request Paperless client scoped to the current user's token.

    Fails closed. The user's token IS the permission boundary: Paperless-ngx
    filters the archive by it, and every document tool relies on that filtering
    to decide what may be shown. Falling back to the superuser client here would
    silently hand out the whole archive — so a missing token (expired session, or
    STORAGE_SECRET rotated so the sealed token no longer decrypts) raises and the
    user logs in again.
    """
    token = _current_token.get()
    if not token:
        raise NoUserContext
    return _PaperlessClient(settings.paperless_url, token)


MAX_ITERATIONS = 16

# Returned to the LLM when the session token is gone. Phrased so the model tells
# the user to sign in rather than retrying the tool.
_NO_USER_MSG = (
    "No active user session — the tool cannot check your document permissions. "
    "Tell the user to sign in again; do not retry."
)


async def check_ollama_available(base_url: str) -> bool:
    """Quick reachability probe for the Ollama server (2 s timeout)."""
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            r = await client.get(f"{base_url.rstrip('/')}/api/tags")
            return r.status_code == 200
    except Exception:
        return False


# ── Events ────────────────────────────────────────────────────────────────────


_THINK_RE = re.compile(
    r"<think(?:ing)?>(.*?)</think(?:ing)?>", re.DOTALL | re.IGNORECASE
)


# Runaway-thinking guard: soft cap triggers only on verbatim repetition (a real
# loop signal), hard cap catches paraphrasing loops. Long *productive* reasoning
# below the hard cap is never cut.
_THINK_SOFT_CAP = 6_000   # chars — below this, never intervene
_THINK_HARD_CAP = 20_000  # chars — above this, always cut


def _thinking_runaway(thinking: str) -> bool:
    """True if accumulated thinking looks like a loop rather than progress."""
    n = len(thinking)
    if n > _THINK_HARD_CAP:
        return True
    if n < _THINK_SOFT_CAP:
        return False
    probe = thinking[-240:]
    return probe in thinking[:-240]


_LOOP_CORRECTION_MSG = (
    "[System notice] You are stuck in a thinking loop. Call a tool NOW or answer directly — no further deliberation."
)


def _extract_thinking(content: str, reasoning_content: str = "") -> tuple[str, str]:
    """Return (thinking_text, clean_content). Checks reasoning_content field first,
    then falls back to stripping <think>...</think> from content."""
    if reasoning_content:
        # Still strip any <think> tags the model also put in the content itself
        clean = _THINK_RE.sub("", content).strip()
        return reasoning_content.strip(), clean
    m = _THINK_RE.search(content)
    if m:
        thinking = m.group(1).strip()
        clean = _THINK_RE.sub("", content).strip()
        return thinking, clean
    return "", content


@dataclass
class ThinkingEvent:
    text: str


@dataclass
class TextTokenEvent:
    text: str


@dataclass
class ToolCallEvent:
    label: str
    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)


@dataclass
class ToolResultEvent:
    tool_output: str  # first 2000 chars of the raw tool result


@dataclass
class DocsRetrievedEvent:
    results: list[DocumentResult]


@dataclass
class IterationEvent:
    iteration: int  # 1-based, out of MAX_ITERATIONS


@dataclass
class DoneEvent:
    input_tokens: int = 0
    context_window: int = 0


@dataclass
class DocxRequestEvent:
    params: dict  # all inputs from trigger_docx_generation call


@dataclass
class EmailRequestEvent:
    params: dict  # recipient_email, recipient_name, subject, salutation, body_paras, closing


@dataclass
class PdfSaveRequestEvent:
    params: dict  # title, content_markdown, filename_topic


@dataclass
class KanbanTaskRequestEvent:
    params: dict  # request, title


@dataclass
class DownloadRequestEvent:
    params: dict  # document_id


@dataclass
class VaultNotesRetrievedEvent:
    notes: list[VaultNoteResult]


@dataclass
class BrainFactsRetrievedEvent:
    facts: list[BrainFactResult]


@dataclass
class WebResultsRetrievedEvent:
    results: list[WebSearchResult]


ChatEvent = (
    ThinkingEvent
    | TextTokenEvent
    | ToolCallEvent
    | ToolResultEvent
    | DocsRetrievedEvent
    | VaultNotesRetrievedEvent
    | BrainFactsRetrievedEvent
    | WebResultsRetrievedEvent
    | IterationEvent
    | DoneEvent
    | DocxRequestEvent
    | EmailRequestEvent
    | PdfSaveRequestEvent
    | KanbanTaskRequestEvent
    | DownloadRequestEvent
)

# ── Tool definitions (canonical Claude format) ────────────────────────────────

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "search",
        "description": (
            "Searches the document archive via semantic embedding search. Finds documents by content — even when official terms differ from everyday wording (e.g. 'car papers' finds 'vehicle registration certificate'). Returns document IDs, titles, type, correspondent, date and summaries."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "semantic_query": {
                    "type": "string",
                    "description": "Content description of the document you are looking for — phrase it the way one would describe a document, not like a search keyword.",
                },
                "created_after": {
                    "type": "string",
                    "description": "Only documents after this date (YYYY-MM-DD)",
                },
                "created_before": {
                    "type": "string",
                    "description": "Only documents before this date (YYYY-MM-DD)",
                },
            },
            "required": ["semantic_query"],
        },
    },
    {
        "name": "search_exact",
        "description": (
            "Analytical search over HARD criteria — metadata and/or exact text. NOT a semantic/content search (use 'search' for that). All parameters are optional and AND-combined; at least one must be set.\nUse when the user names concrete, hard criteria:\n• tags (e.g. 'tax return', '2025'), correspondent, document type\n• date ranges\n• literal identifiers contained in the document: invoice numbers, IBANs, license plates, contract/file numbers, phone numbers (via 'text')\n'text' searches the extracted document content (substring, multiple words = OR) AND the structured cross-references — thereby finds all documents sharing the same reference."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Exact text/substring in the document content, or a reference (e.g. 'RE-2024-001', 'DE89370400440532013000', 'KA-ZY 244'). Multiple words are OR-combined.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tag names as in Paperless (e.g. ['tax return', '2025']). Case-insensitive.",
                },
                "tag_match": {
                    "type": "string",
                    "enum": ["all", "any"],
                    "description": "'all' = the document must carry ALL named tags (intersection), 'any' = at least one. Derive from the user's phrasing; ask the user if unclear. Default: 'all'.",
                },
                "correspondent": {
                    "type": "string",
                    "description": "Name of the correspondent/sender (e.g. 'Deutsche Telekom').",
                },
                "document_type": {
                    "type": "string",
                    "description": "Document type (e.g. 'Invoice', 'Contract', 'Notice').",
                },
                "created_after": {
                    "type": "string",
                    "description": "Only documents after this date (YYYY-MM-DD)",
                },
                "created_before": {
                    "type": "string",
                    "description": "Only documents before this date (YYYY-MM-DD)",
                },
            },
        },
    },
    {
        "name": "get_document_details",
        "description": (
            "Returns full details for a document: Paperless metadata (title, correspondent, tags, date, page count), user notes, and AI-extracted content (summary, actions/deadlines, cross-references, tables, page summaries). Always use this tool for document details, metadata or notes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "integer",
                    "description": "Paperless ID of the document",
                },
            },
            "required": ["document_id"],
        },
    },
    {
        "name": "get_document_table",
        "description": (
            "Returns a COMPLETE extracted table of a document (all rows). get_document_details shows only a preview per table — use this tool when you need concrete values/rows of a table (amounts, line items). table_index comes from the table preview in get_document_details ([Table N]). For very large tables, page through with offset/limit."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "integer",
                    "description": "Paperless ID of the document",
                },
                "table_index": {
                    "type": "integer",
                    "description": "Index of the table (0-based, from get_document_details)",
                },
                "offset": {
                    "type": "integer",
                    "description": "First row to return (0-based). Default 0.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of rows. Default 100.",
                },
            },
            "required": ["document_id", "table_index"],
        },
    },
    {
        "name": "get_document_page_text",
        "description": (
            "Returns the extracted text of a specific page. Use this for precise content: amounts, clauses, specific wording."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {"type": "integer"},
                "page_number": {
                    "type": "integer",
                    "description": "Page number (1-based)",
                },
            },
            "required": ["document_id", "page_number"],
        },
    },
    {
        "name": "view_document_page",
        "description": (
            "Has the vision model analyze a document page directly as an image. Use for: visual elements (logos, stamps, signatures, diagrams, tables as graphics), handwriting, poorly readable OCR, layout questions, colors/symbols/graphics. Also useful when the user explicitly asks about the visual appearance. Slower than the text tools — check get_document_page_text first for pure text content. question MUST contain the concrete visual question (e.g. 'Describe the company logo: colors, symbol, lettering.')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {"type": "integer"},
                "page_number": {
                    "type": "integer",
                    "description": "Page number (1-based)",
                },
                "question": {
                    "type": "string",
                    "description": "Concrete visual question — what exactly should the vision model recognize or describe on the image? Be as specific as possible (e.g. 'Describe the logo: colors, symbols, lettering.' or 'Is a signature present?').",
                },
            },
            "required": ["document_id", "page_number", "question"],
        },
    },
    {
        "name": "download_document",
        "description": (
            "Starts the download of a Paperless document's original file directly in the user's browser. ONLY call when the user EXPLICITLY wants to download a document (e.g. 'download the invoice for me', 'give me the PDF', 'download document #123'). NEVER use for information search or reading content. If the document_id is unknown, determine it via search first. For multiple documents call the tool several times (once per document)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "integer",
                    "description": "Paperless document ID of the document to download",
                },
            },
            "required": ["document_id"],
        },
    },
    {
        "name": "create_note",
        "description": (
            "Attaches a comment to a PAPERLESS DOCUMENT (not a note in the user's vault — you cannot write vault notes at all). Requires a document_id and is visible in Paperless-ngx on that document. Use it to record what happened with a document: 'paid on 12.05.2026', 'cancelled by phone', 'forwarded to the tax advisor'. Only call when the user actually wants something recorded about a document — never to store your own intermediate results, and never for facts about the user (use remember_fact for those). Determine an unknown document_id via search first, and keep the note short and factual."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "integer",
                    "description": "Paperless document ID the note is attached to",
                },
                "note": {
                    "type": "string",
                    "description": "Note text. Short and factual, in the user's language, with a date where one is relevant (e.g. 'Paid on 12.05.2026 via bank transfer').",
                },
            },
            "required": ["document_id", "note"],
        },
    },
    {
        "name": "trigger_docx_generation",
        "description": (
            "Creates a DIN 5008-compliant business letter (German standard) as DOCX. ONLY call when the user EXPLICITLY wants to compose a letter (e.g. 'write a letter to …', 'draft a formal letter', 'compose an inquiry to …'). NEVER use for information search or general questions. Collect all necessary information (recipient, subject, content) before calling the tool. After the call a download dialog opens."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recipient_name": {
                    "type": "string",
                    "description": "Full name / company of the recipient",
                },
                "recipient_street": {
                    "type": "string",
                    "description": "Street and house number of the recipient",
                },
                "recipient_postcode": {
                    "type": "string",
                    "description": "Postal code of the recipient",
                },
                "recipient_city": {
                    "type": "string",
                    "description": "City of the recipient",
                },
                "subject": {
                    "type": "string",
                    "description": "Subject line of the letter",
                },
                "salutation": {
                    "type": "string",
                    "description": "Salutation, e.g. 'Sehr geehrte Damen und Herren,' (German letters keep German salutations)",
                },
                "body_paras": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Paragraphs of the letter body (each entry = one paragraph)",
                },
                "closing": {
                    "type": "string",
                    "description": "Closing phrase, default: 'Mit freundlichen Grüßen'",
                },
                "source_cross_ref": {
                    "type": "string",
                    "description": "Your reference ('Ihr Zeichen'): full designation with type prefix, e.g. 'Rechn. Nr. 260412' or 'Az. 2026-04-X' — always incl. the prefix, never just the number (optional)",
                },
                "src_doc_info": {
                    "type": "string",
                    "description": "Reference line: short title of the referenced document, e.g. 'Rechnung Nr. 260412 vom 12.05.2026' (optional, leave empty if unknown)",
                },
                "reference_doc_id": {
                    "type": "integer",
                    "description": "Paperless document ID as reference (optional)",
                },
            },
            "required": [
                "recipient_name",
                "recipient_street",
                "recipient_postcode",
                "recipient_city",
                "subject",
                "salutation",
                "body_paras",
            ],
        },
    },
    {
        "name": "create_email",
        "description": (
            "Creates an email template for copying. ONLY call when the user EXPLICITLY wants to write or send an email (e.g. 'write an email to …', 'draft an email', 'I want to inquire about … by email'). NEVER use for information search, questions or general tasks. After the call a dialog opens where the user can copy address and text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recipient_email": {
                    "type": "string",
                    "description": "Email address of the recipient (determine from documents/emails or ask the user)",
                },
                "recipient_name": {
                    "type": "string",
                    "description": "Name of the recipient",
                },
                "subject": {"type": "string", "description": "Subject of the email"},
                "salutation": {
                    "type": "string",
                    "description": "Salutation, e.g. 'Sehr geehrte Damen und Herren,' (German letters keep German salutations)",
                },
                "body_paras": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Paragraphs of the email body (each entry = one paragraph). The last paragraph should contain an appropriate closing.",
                },
            },
            "required": ["recipient_email", "subject", "salutation", "body_paras"],
        },
    },
    {
        "name": "web_search",
        "description": (
            "Searches the web for current information. Returns titles, short descriptions, publication dates (if available) and URLs, plus direct answers/infoboxes from the search engine."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "max_results": {
                    "type": "integer",
                    "description": "Number of results (default 5, max. 10)",
                },
                "language": {
                    "type": "string",
                    "description": (
                        "Search language, e.g. 'de', 'en' or 'auto'. Default: the ARCHIVE_LANGUAGE setting. Use 'en' for international or technical topics."
                    ),
                },
                "time_range": {
                    "type": "string",
                    "enum": ["day", "week", "month", "year"],
                    "description": (
                        "Only results from this time range. Set 'day' or 'week' for current events; omit for timeless topics."
                    ),
                },
                "category": {
                    "type": "string",
                    "enum": ["general", "news", "science"],
                    "description": (
                        "'news' for news/current events — returns real article links with dates instead of overview pages. 'science' searches academic sources (PubMed, Crossref, OpenAlex, arXiv, Semantic Scholar) — use it for studies, case reports, measurements and anything where a peer-reviewed source is the right kind of evidence; general web search rarely surfaces these. 'general' (default) for everything else. When time_range is set, 'news' is used automatically anyway."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "web_fetch_page",
        "description": (
            "Loads the full text content of a web page (URL from web_search results). Use after web_search when you want to read the exact content of a page — e.g. for prices, technical details, articles or how-tos."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL of the web page to load",
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "calculate",
        "description": (
            "Evaluates a mathematical expression and returns the exact result. ALWAYS use this for every calculation — never do mental arithmetic. Supports +, -, *, /, **, %, parentheses and all math.* functions (sqrt, log, sin, cos, pi, e etc.)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "Mathematical expression, e.g. '1234.56 * 1.19' or 'sqrt(2) * pi'",
                },
            },
            "required": ["expression"],
        },
    },
    {
        "name": "get_current_date",
        "description": "Returns the current date and time.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_emails",
        "description": (
            "Searches the user's emails via IMAP (inbox and sent mail). Use when the user asks about an email, order, booking confirmation or correspondence. Requires a configured IMAP account (settings). On Gmail all parameters run as native Gmail search (fast, precise). For sent emails (e.g. 'when did I write/send X'): set to_addr. For 'latest/newest email': leave all fields empty, set max_results=1 — returns the newest email without filtering. The default search ALREADY covers every folder (all mail) — 'folder' only narrows it down. If a search returns nothing, do NOT guess folder names: retry with a shorter or different search term, or with from_addr. IMPORTANT: NEVER reference emails with #N (e.g. '#3', '#15') — that syntax is reserved exclusively for Paperless documents and would open wrong documents. Instead use 'email 3', 'the third email', '[E3]' or similar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Full-text search term (subject + body). On Gmail: Gmail syntax allowed.",
                },
                "subject": {
                    "type": "string",
                    "description": "Search in the subject only",
                },
                "from_addr": {
                    "type": "string",
                    "description": "Filter by sender address or name (e.g. 'amazon.de')",
                },
                "to_addr": {
                    "type": "string",
                    "description": "Filter by recipient address or name — use when searching for sent emails (e.g. 'ead' or 'boss@company.com')",
                },
                "since": {
                    "type": "string",
                    "description": "Only emails after this date (YYYY-MM-DD)",
                },
                "before": {
                    "type": "string",
                    "description": "Only emails before this date (YYYY-MM-DD)",
                },
                "unseen_only": {
                    "type": "boolean",
                    "description": "Only unread emails",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Number of emails per page (default: 20). Combine with 'offset' for pagination.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Start position for pagination (default: 0). If the result shows '[N more available — offset=X]': call again with offset=X.",
                },
                "detail": {
                    "type": "string",
                    "enum": ["headers", "snippet", "full"],
                    "description": (
                        "Level of detail of the email content. 'headers': only date/from/subject, no body text — fastest overview for many hits. 'snippet': first ~300 characters of the body (default). 'full': complete email text for detailed analysis."
                    ),
                },
                "folder": {
                    "type": "string",
                    "description": (
                        "Restrict the search to one IMAP folder, in plain text exactly as 'list_folders_only' printed it (e.g. 'INBOX', 'Archive', 'Bestellvorgänge'). Default (recommended): omit — all mail is searched anyway."
                    ),
                },
                "list_folders_only": {
                    "type": "boolean",
                    "description": (
                        "List all available IMAP folders (no search). Only useful to restrict a search that already works — it is never the fix for a search that found nothing."
                    ),
                },
            },
        },
    },
    {
        "name": "search_calendar",
        "description": (
            "Searches the user's calendar entries (title, description, location, date). For time-based questions (e.g. 'in June', 'next week') set date_from+date_to and leave query empty. For keywords (e.g. 'doctor', 'orthopedist') set query. Requires a configured calendar account (settings)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search term for event name, description or location (optional)",
                },
                "date_from": {
                    "type": "string",
                    "description": "Start date YYYY-MM-DD (optional, e.g. '2026-06-01' for June)",
                },
                "date_to": {
                    "type": "string",
                    "description": "End date YYYY-MM-DD (optional, e.g. '2026-06-30' for June)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_actions",
        "description": (
            "Returns actions / deadlines from all documents (from the index.json). Use 'upcoming_only=true' for future deadlines, or filter with 'after' / 'before' (YYYY-MM-DD) for a time range. Returns document ID, description, deadline and whether the deadline date is certain."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "upcoming_only": {
                    "type": "boolean",
                    "description": "Only deadlines in the future (from today)",
                },
                "after": {
                    "type": "string",
                    "description": "Only deadlines after this date (YYYY-MM-DD)",
                },
                "before": {
                    "type": "string",
                    "description": "Only deadlines before this date (YYYY-MM-DD)",
                },
                "certain_only": {
                    "type": "boolean",
                    "description": "Only deadlines with a certain date",
                },
            },
        },
    },
    {
        "name": "create_deadline",
        "description": (
            "Creates a manual deadline / appointment in the user's memory (appears on the dashboard under deadlines). Use this when the user asks for a reminder or an appointment ('remind me about …', 'the deadline for … is …'). Do not use for deadlines that already come from documents."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "What it is about (e.g. 'renew passport').",
                },
                "due": {
                    "type": "string",
                    "description": "Due date in YYYY-MM-DD format.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional keywords.",
                },
            },
            "required": ["text", "due"],
        },
    },
    {
        "name": "remember_fact",
        "description": (
            "Stores a fact in YOUR OWN long-term memory — a separate store you curate, not the user's notes (those are read-only for you, see vault_search). Use this tool when the user explicitly tells you something, or when you draw a reliable conclusion from documents that would be useful in future conversations. This is also the right tool when the user wants something 'noted down' about themselves, their contracts or their belongings. Do NOT use for speculative assumptions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "The fact as one clear, complete sentence",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Relevant keywords, e.g. [\"insurance\", \"family\", \"deadline\"]",
                },
                "source_doc_id": {
                    "type": "integer",
                    "description": "Paperless ID of the source document if derived from a document (optional)",
                },
                "source_page": {
                    "type": "integer",
                    "description": "Page number in the source document (optional)",
                },
                "confidence": {
                    "type": "number",
                    "description": "Certainty of the fact, 0.0–1.0",
                },
                "force": {
                    "type": "boolean",
                    "description": "true = always store, even if a very similar fact exists",
                },
                "filename_topic": {
                    "type": "string",
                    "description": (
                        "Short topic title for the filename — ONLY the topic, NOT the content. E.g. 'Anna birthday', 'Max allergy', 'car insurance'. Stays stable across updates of the fact."
                    ),
                },
            },
            "required": ["text", "tags", "confidence", "filename_topic"],
        },
    },
    {
        "name": "search_memory",
        "description": (
            "Semantic search in long-term memory. Call this tool FIRST when the question is factual or personal — e.g. deadlines, amounts, owners, people, things already said. Skip it for pure document searches like 'show all Telekom invoices'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What you are looking for",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Number of results (default: 5)",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "update_brain_fact",
        "description": (
            "Updates the text of a fact in your own long-term memory — never one of the user's notes, which you cannot modify. Use this to correct outdated or wrong information. You get the fact ID from search_memory (NOT from vault_search; a pbrain_id from a vault note is not valid here)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fact_id": {
                    "type": "string",
                    "description": "Full fact ID from search_memory",
                },
                "new_text": {
                    "type": "string",
                    "description": "Corrected/updated text of the fact",
                },
            },
            "required": ["fact_id", "new_text"],
        },
    },
    {
        "name": "delete_brain_fact",
        "description": (
            "Permanently deletes an outdated or wrong fact from your own long-term memory. It cannot delete a note of the user's — you have no tool for that. You get the fact ID from search_memory (NOT from vault_search)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fact_id": {
                    "type": "string",
                    "description": "Full fact ID from search_memory",
                },
            },
            "required": ["fact_id"],
        },
    },
    {
        "name": "vault_search",
        "description": (
            "READ-ONLY search of the user's personal notes (Obsidian vault). These notes belong to the user: there is no tool to create, edit, append to, rename or delete one, so never offer to write anything into a note — say where it would go and let the user do it, or offer remember_fact instead. Two modes: (1) search via 'query' — matches both by meaning (semantic) and by note title/filename; notes whose title matches are listed first and marked '(title match)'. (2) full retrieval via 'pbrain_id' — reads all chunks of one specific note completely. Not for Paperless documents or memory facts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Semantic search query (mode 1)",
                },
                "pbrain_id": {
                    "type": "string",
                    "description": "The pbrain_id of a note — reads all chunks completely (mode 2). This is the UUID printed as 'pbrain_id: …' in a previous search result; copy that value verbatim. NEVER pass the distance score (the number in parentheses).",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Number of results for mode 1 (default: 5)",
                },
            },
        },
    },
    {
        "name": "generate_chat_pdf",
        "description": (
            "Creates a structured PDF document from the current conversation content and stores it in Paperless as an AI-generated information document. Use this tool when the user wants to permanently archive information from the chat — e.g. email analyses, research results, summaries or deadline overviews. Compose a complete, well-structured Markdown text with all relevant information before calling the tool. If it is unclear what should be saved, ask the user first. After the call a review dialog opens before saving."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Document title, e.g. 'Email history John Miller' or 'Calendar overview May 2026'",
                },
                "content_markdown": {
                    "type": "string",
                    "description": "Complete document content in Markdown. Well structured with headings, lists and tables.",
                },
                "filename_topic": {
                    "type": "string",
                    "description": "Short filename part without special characters, max. 30 characters, e.g. 'Email_History_J_Miller'",
                },
            },
            "required": ["title", "content_markdown", "filename_topic"],
        },
    },
    {
        "name": "create_kanban_task",
        "description": (
            "Creates an autonomous deep-research task in the background. Use this tool when the user mentions a complex task that requires multi-step research, document analysis or synthesis work — i.e. too extensive for a single chat step. IMPORTANT: Write a short, goal-oriented request (3–6 sentences). No detailed plan, no method prescriptions, no pre-researched data — the agent researches on its own. Provide only the goal, context and relevant Paperless document IDs. CRITICAL: After calling this tool, do NOTHING ELSE. No web search, no own research, no generate_chat_pdf — the deep-research agent takes over the work completely. After the call a confirmation dialog opens — the user can still adjust the request and pick the model before it starts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short title, max. 6 words, e.g. 'Insurance contract overview 2026'",
                },
                "request": {
                    "type": "string",
                    "description": (
                        "Short work order, max. 6 sentences. Contains: what should be determined/created, which Paperless document IDs are relevant (if known), the desired output format. Does NOT contain: research methodology, step-by-step plans, pre-researched data, comparison criteria — the agent works that out itself."
                    ),
                },
            },
            "required": ["title", "request"],
        },
    },
]


def _parse_tool_arguments(raw: str) -> dict:
    """Tool-call arguments from a model, repaired before they are given up on.

    Local models write German into their own arguments — `query="Rechnung
    „Milbich" 2023"` — which is not valid JSON, and the previous handling
    replaced the entire argument object with `{}`. The tool then ran with no
    arguments: a search for nothing, an empty answer, and nothing in any log
    saying why. Unrepairable arguments are handed to the tool instead, which
    rejects them visibly.
    """
    from werkbank.v2.models import repair_json

    for candidate in (raw, repair_json(raw)):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    _log.warning("unparseable tool arguments from the model: %s", raw[:200])
    return {"_unparsed_arguments": raw[:500]}


def _tool_label(name: str) -> str:
    """Translated activity-bar label for a tool call. Built at call time so the
    literals are extractable and resolve against the current user's language.
    (Emitted during a chat turn, which runs inside the page request context.)"""
    from i18n import get_translator

    _ = get_translator()
    labels = {
        "search": _("🔍 Searching documents..."),
        "search_exact": _("🔎 Analytical search..."),
        "get_document_details": _("📋 Loading document details..."),
        "get_document_table": _("📊 Loading table..."),
        "get_document_page_text": _("📄 Reading page text..."),
        "view_document_page": _("👁 Visual analysis..."),
        "web_search": _("🌐 Web search..."),
        "web_fetch_page": _("🌐 Loading web page..."),
        "calculate": _("🧮 Calculating..."),
        "get_current_date": _("📅 Getting date..."),
        "get_actions": _("📌 Loading deadlines..."),
        "create_deadline": _("📌 Creating deadline..."),
        "create_note": _("🗒 Saving note..."),
        "search_emails": _("📧 Searching emails..."),
        "search_calendar": _("📅 Searching calendar..."),
        "trigger_docx_generation": _("📝 Creating letter template..."),
        "create_email": _("📧 Creating email template..."),
        "remember_fact": _("🧠 Saving fact..."),
        "search_memory": _("🧠 Searching memory..."),
        "update_brain_fact": _("🧠 Updating fact..."),
        "delete_brain_fact": _("🧠 Deleting fact..."),
        "vault_search": _("📓 Searching notes..."),
        "generate_chat_pdf": _("💾 Creating document..."),
        "download_document": _("⬇️ Preparing download..."),
        "create_kanban_task": _("⚙️ Starting deep research..."),
    }
    return labels.get(name, name)


# ── Tool executor ─────────────────────────────────────────────────────────────


async def execute_tool(
    name: str, inputs: dict
) -> tuple[str, list[DocumentResult], list]:
    """Execute a tool call. Returns (text_for_llm, doc_results_for_ui, list[extra_events])."""
    if "_unparsed_arguments" in (inputs or {}):
        # Running the tool with the arguments dropped is what this replaces: a
        # search with no query answers "nothing found", and the model believes it.
        return (
            f"Tool '{name}' was not run: its arguments were not valid JSON. "
            "Send the call again with properly escaped arguments — a quote "
            "inside a string value needs a backslash.",
            [],
            [],
        )
    try:
        return await _execute_tool_dispatch(name, inputs)
    except NoUserContext:
        return _NO_USER_MSG, [], []


async def _execute_tool_dispatch(
    name: str, inputs: dict
) -> tuple[str, list[DocumentResult], list]:
    if name == "trigger_docx_generation":
        text = (
            "Letter parameters received. A dialog window opens where you can adjust sender details and download the finished letter."
        )
        return text, [], [DocxRequestEvent(params=inputs)]
    if name == "create_email":
        text = (
            "Email template created. A dialog opens where you can copy address, subject and message to the clipboard."
        )
        return text, [], [EmailRequestEvent(params=inputs)]
    if name == "generate_chat_pdf":
        text = (
            "Document prepared. A dialog opens where you can review title and content and save the document to Paperless."
        )
        return text, [], [PdfSaveRequestEvent(params=inputs)]
    if name == "create_kanban_task":
        return "Task recorded.", [], [KanbanTaskRequestEvent(params=inputs)]
    if name == "download_document":
        text = (
            f"A download button for document #{inputs.get('document_id')} is now shown in the chat. "
            "The download starts only after the user clicks it. Do NOT claim the download has already "
            "started — tell the user to click the button to download the file."
        )
        return text, [], [DownloadRequestEvent(params=inputs)]
    if name == "search_memory":
        text, facts, vault_notes = await _tool_search_memory(inputs)
        extras = []
        if facts:
            extras.append(BrainFactsRetrievedEvent(facts=facts))
        if vault_notes:
            extras.append(VaultNotesRetrievedEvent(notes=vault_notes))
        return text, [], extras
    if name == "vault_search":
        text, notes = await _tool_vault_search(inputs)
        return text, [], ([VaultNotesRetrievedEvent(notes=notes)] if notes else [])
    if name == "web_search":
        text, web_results = await _tool_web_search(inputs)
        return text, [], (
            [WebResultsRetrievedEvent(results=web_results)] if web_results else []
        )
    # All other tools return no extra events
    text, docs = await _execute_tool_inner(name, inputs)
    return text, docs, []


async def _execute_tool_inner(
    name: str, inputs: dict
) -> tuple[str, list[DocumentResult]]:
    if name == "search":
        return await _tool_search(inputs)
    if name == "search_exact":
        return await _tool_search_exact(inputs)
    if name == "get_document_details":
        return await _tool_get_document_details(inputs)
    if name == "get_document_table":
        return await _tool_get_document_table(inputs)
    if name == "get_document_page_text":
        return await _tool_get_document_page_text(inputs)
    if name == "view_document_page":
        return await _tool_view_document_page(inputs)
    if name == "web_fetch_page":
        return await _tool_web_fetch_page(inputs)
    if name == "calculate":
        return _tool_calculate(inputs), []
    if name == "get_current_date":
        return _tool_get_current_date(), []
    if name == "get_actions":
        return await _tool_get_actions(inputs), []
    if name == "create_deadline":
        return await _tool_create_deadline(inputs)
    if name == "create_note":
        return await _tool_create_note(inputs)
    if name == "search_emails":
        return await _tool_search_emails(inputs)
    if name == "search_calendar":
        return await _tool_search_calendar(inputs)
    if name == "remember_fact":
        return await _tool_remember_fact(inputs)
    if name == "update_brain_fact":
        return await _tool_update_brain_fact(inputs)
    if name == "delete_brain_fact":
        return await _tool_delete_brain_fact(inputs)
    return f"Unbekanntes Tool: {name}", []


_BOOL_OPS = {"OR", "AND", "NOT", "UND", "ODER"}


def _identifier_atoms(query: str) -> list[str] | None:
    """If the query looks like a single separator-bearing identifier (license
    plate, invoice/reference number, …), return its atoms split on whitespace
    AND hyphens; else None.

    Atoms must each be a short letter group (1-3) or a digit group (1-6), with
    at least one of each, 2-4 atoms total. This catches 'KA-AA 1331',
    'KA AA 1331', 'RE-2024-001' but not normal prose like 'Rechnung Vodafone'.
    Atomizing on both space and hyphen means 'KA AA 1331' and 'KA-AA 1331'
    produce the identical atom list — and therefore identical results.
    """
    atoms = [a for a in re.split(r"[\s\-]+", query.strip()) if a]
    if not (2 <= len(atoms) <= 4):
        return None
    has_alpha = has_digit = False
    for a in atoms:
        if re.fullmatch(r"[A-Za-zÄÖÜäöü]{1,3}", a):
            has_alpha = True
        elif re.fullmatch(r"[0-9]{1,6}", a):
            has_digit = True
        else:
            return None
    return atoms if (has_alpha and has_digit) else None


def _separator_variants(atoms: list[str]) -> list[str]:
    """Join atoms with every separator ('', '-', ' ') between adjacent atoms.

    'KA-AA 1331' (stored form) and 'KAAA1331' etc. are all generated, so the
    filter matches the document regardless of how the user spaced/hyphenated it.
    """
    seps = ("", "-", " ")
    variants = {atoms[0]}
    for atom in atoms[1:]:
        variants = {prefix + sep + atom for prefix in variants for sep in seps}
    return sorted(variants)


def _build_where_document(query: str) -> dict | None:
    atoms = _identifier_atoms(query)
    if atoms:
        # Tight phrase match across separator variants — avoids the loose
        # single-token OR ('KA' OR 'AA') that floods candidates and lets the
        # vector ranking bury the real identifier hits.
        variants = _separator_variants(atoms)
        if len(variants) == 1:
            return {"$contains": variants[0]}
        return {"$or": [{"$contains": v} for v in variants]}

    words = [w for w in query.split() if len(w) > 1 and w.upper() not in _BOOL_OPS]
    if not words:
        return None
    if len(words) == 1:
        return {"$contains": words[0]}
    return {"$or": [{"$contains": w} for w in words]}


async def _tool_search(inputs: dict) -> tuple[str, list[DocumentResult]]:
    created: dict | None = None
    if inputs.get("created_after") or inputs.get("created_before"):
        created = {
            k: v
            for k, v in [
                ("after", inputs.get("created_after")),
                ("before", inputs.get("created_before")),
            ]
            if v
        }
    semantic_query = inputs.get("semantic_query") or None
    raw_text = (inputs.get("query") or "").strip()

    # text_query goes to ChromaDB as an OR where_document filter (not Paperless API).
    # Multiple words are OR-connected; single word acts as a must-contain.
    # When no semantic_query is given, raw_text also drives the embedding ranking.
    where_doc = _build_where_document(raw_text) if raw_text else None
    effective_semantic = semantic_query or (raw_text if raw_text else None)

    filters = {
        "query": None,  # text search via ChromaDB where_document, not Paperless
        "correspondent": None,
        "document_type": None,
        "tags": None,
        "created": created,
        "added": None,
    }

    async def _brain_hints() -> list[str]:
        if not effective_semantic:
            return []
        username = _current_owner.get() or ""
        if not username:
            return []
        try:
            from services.clients import brain
            from werkbank.settings_store import (
                get_brain_hint_threshold,
                get_brain_hint_window,
            )

            hits = await brain.search_hints(effective_semantic, username, max_results=5)
            if not hits:
                return []
            best_dist = hits[0][2]
            threshold = get_brain_hint_threshold()
            window = get_brain_hint_window()
            max_dist = 1.0 - threshold
            return [
                f"[{dist:.2f}] {text}"
                for _, text, dist in hits
                if dist <= best_dist * window and dist <= max_dist
            ]
        except Exception:
            return []

    async def _vault_hints() -> list[str]:
        if not effective_semantic:
            return []
        username = _current_owner.get() or ""
        if not username:
            return []
        try:
            from services.clients import vault_chroma
            from werkbank.settings_store import (
                get_brain_hint_threshold,
                get_brain_hint_window,
            )

            total = await vault_chroma.count()
            if total == 0:
                return []
            hits = await vault_chroma.query(
                query_texts=[effective_semantic],
                n_results=min(5, total),
                where={"user": {"$eq": username}},
            )
            results = hits[0] if hits else []
            if not results:
                return []
            best_dist = results[0].get("distance", 1.0)
            threshold = get_brain_hint_threshold()
            window = get_brain_hint_window()
            max_dist = 1.0 - threshold
            lines = []
            for h in results:
                dist = h.get("distance", 1.0)
                if dist > best_dist * window or dist > max_dist:
                    continue
                m = h.get("metadata") or {}
                path = m.get("path", "")
                note_name = path.rsplit("/", 1)[-1].removesuffix(".md") if path else ""
                heading = m.get("heading_path", "")
                label = f"{note_name}" + (f" › {heading}" if heading else "")
                snippet = (h.get("document") or "")[:200]
                lines.append(f"[{dist:.2f}] {label}: {snippet}")
            return lines
        except Exception:
            return []

    outcomes = await asyncio.gather(
        search(
            filters=filters,
            semantic_query=effective_semantic,
            where_document=where_doc,
            paperless_client=_user_paperless(),
        ),
        _brain_hints(),
        _vault_hints(),
        return_exceptions=True,
    )
    if isinstance(outcomes[0], Exception):
        return f"Error during search: {outcomes[0]}", []
    results = outcomes[0]
    hint_texts: list[str] = (
        outcomes[1] if not isinstance(outcomes[1], Exception) else []
    )
    vault_hint_texts: list[str] = (
        outcomes[2] if not isinstance(outcomes[2], Exception) else []
    )

    if not results:
        prefix = ""
        if hint_texts:
            prefix += (
                "💡 Memory entries on this topic:\n"
                + "\n".join(f"• {t}" for t in hint_texts)
                + "\n\n"
            )
        if vault_hint_texts:
            prefix += (
                "📓 Vault notes on this topic:\n"
                + "\n".join(f"• {t}" for t in vault_hint_texts)
                + "\n\n"
            )
        return (prefix + "No documents found.").strip(), []

    lines = [f"Found: {len(results)} document(s)\n"]
    if hint_texts:
        lines.insert(
            0,
            "💡 Memory entries on this topic:\n"
            + "\n".join(f"• {t}" for t in hint_texts)
            + "\n",
        )
    if vault_hint_texts:
        lines.insert(
            0,
            "📓 Vault notes on this topic:\n"
            + "\n".join(f"• {t}" for t in vault_hint_texts)
            + "\n",
        )
    from werkbank.settings_store import get_search_max_results as _get_max_results

    for i, r in enumerate(results[: _get_max_results()], 1):
        doc = r.document
        lines.append(f"{i}. #{doc.id} — {doc.title}")
        meta = " | ".join(
            filter(
                None,
                [
                    doc.document_type,
                    doc.correspondent,
                    r.display_date,
                    f"Score {r.relevance_score:.3f}"
                    if r.relevance_score is not None
                    else None,
                ],
            )
        )
        if meta:
            lines.append(f"   {meta}")
        sc = sidecar_service.load_sidecar(doc.id)
        if sc:
            summary = sc.get("full_summary_summarized") or sc.get("full_summary")
            if summary:
                lines.append(f"   {summary[:250]}")
        if r.matched_chunks:
            snippet = r.matched_chunks[0][:150].replace("\n", " ")
            lines.append(f"   …{snippet}…")
        lines.append("")

    return "\n".join(lines), results


def _resolve_names(
    names: list[str], id_to_name: dict[int, str]
) -> tuple[list[int], list[str]]:
    """Resolve display names to Paperless IDs (case-insensitive, exact then substring).

    Returns (matched_ids, unresolved_names). One name may resolve to several ids
    if multiple entries match the substring fallback.
    """
    matched: list[int] = []
    unresolved: list[str] = []
    lowered = {i: n.lower() for i, n in id_to_name.items()}
    for raw in names:
        needle = raw.strip().lower()
        if not needle:
            continue
        exact = [i for i, n in lowered.items() if n == needle]
        hits = exact or [i for i, n in lowered.items() if needle in n]
        if hits:
            matched.extend(hits)
        else:
            unresolved.append(raw.strip())
    return matched, unresolved


async def _tool_search_exact(inputs: dict) -> tuple[str, list[DocumentResult]]:
    text = (inputs.get("text") or "").strip()
    raw_tags = inputs.get("tags") or []
    tag_names = [t for t in (str(x).strip() for x in raw_tags) if t]
    tag_match = (inputs.get("tag_match") or "all").lower()
    corr_name = (inputs.get("correspondent") or "").strip()
    dtype_name = (inputs.get("document_type") or "").strip()
    created_after = (inputs.get("created_after") or "").strip()
    created_before = (inputs.get("created_before") or "").strip()

    if not any([text, tag_names, corr_name, dtype_name, created_after, created_before]):
        return (
            "No search criterion given (tags, correspondent, type, date or text).",
            [],
        )

    pl = _user_paperless()

    # ── Resolve metadata names → Paperless IDs in parallel ───────────────────
    unresolved: list[str] = []
    tag_ids: list[int] = []
    corr_ids: list[int] = []
    dtype_ids: list[int] = []
    if tag_names or corr_name or dtype_name:
        tag_map, corr_map, dtype_map = await asyncio.gather(
            pl.get_tag_map() if tag_names else _noop_map(),
            pl.get_correspondent_map() if corr_name else _noop_map(),
            pl.get_document_types_map() if dtype_name else _noop_map(),
        )
        if tag_names:
            tag_ids, u = _resolve_names(tag_names, tag_map)
            unresolved += [f"Tag '{x}'" for x in u]
        if corr_name:
            corr_ids, u = _resolve_names([corr_name], corr_map)
            unresolved += [f"Correspondent \'{x}'" for x in u]
        if dtype_name:
            dtype_ids, u = _resolve_names([dtype_name], dtype_map)
            unresolved += [f"Document type \'{x}'" for x in u]

    if unresolved:
        return (
            "The following criteria do not exist in Paperless: "
            + ", ".join(unresolved)
            + ". Check the spelling or ask the user.",
            [],
        )

    created = None
    if created_after or created_before:
        created = {}
        if created_after:
            created["after"] = created_after
        if created_before:
            created["before"] = created_before

    filters = {
        "query": None,  # never use Paperless full-text (OCR inferior to chunks)
        "correspondent": corr_ids or None,
        "document_type": dtype_ids or None,
        "tags": tag_ids if (tag_ids and tag_match == "all") else None,
        "tag_ids_any": tag_ids if (tag_ids and tag_match == "any") else None,
        "created": created,
        "added": None,
    }

    where_doc = _build_where_document(text) if text else None
    if text and not where_doc:
        return "Suchtext zu kurz.", []

    try:
        results = await search(
            filters=filters,
            semantic_query=text or None,
            where_document=where_doc,
            paperless_client=pl,
        )
    except Exception as e:
        return f"Error during analytical search: {e}", []

    # ── Fold in structured cross-reference matches for the literal value ─────
    if text:
        ref_ids = cross_ref_index.find_by_value(text)
        have = {r.document.id for r in results}
        missing = [pid for pid in ref_ids if pid not in have]
        if missing:
            try:
                extra = await pl.list_documents(ids=missing)
                for d in extra:
                    results.append(
                        DocumentResult(
                            document=d,
                            relevance_score=0.5,  # synthetic — cross-ref hit
                            has_actions=sidecar_service.has_actions(d.id),
                        )
                    )
            except Exception:
                pass

    if not results:
        return "No documents match the criteria.", []

    crit = []
    if text:
        crit.append(f"Text '{text}'")
    if tag_names:
        crit.append(f"Tags {tag_match.upper()}: {', '.join(tag_names)}")
    if corr_name:
        crit.append(f"Correspondent \'{corr_name}'")
    if dtype_name:
        crit.append(f"Typ '{dtype_name}'")
    if created:
        crit.append(
            "Zeitraum " + "–".join(filter(None, [created_after, created_before]))
        )
    header = "; ".join(crit) if crit else "criteria"

    lines = [f"Analytical search ({header}): {len(results)} document(s)\n"]
    from werkbank.settings_store import get_search_max_results as _get_max_results

    for i, r in enumerate(results[: _get_max_results()], 1):
        doc = r.document
        lines.append(f"{i}. #{doc.id} — {doc.title}")
        meta = " | ".join(
            filter(None, [doc.document_type, doc.correspondent, r.display_date])
        )
        if meta:
            lines.append(f"   {meta}")
        if r.matched_chunks:
            snippet = r.matched_chunks[0][:200].replace("\n", " ")
            lines.append(f"   …{snippet}…")
        lines.append("")
    return "\n".join(lines), results


async def _noop_map() -> dict[int, str]:
    return {}


def _table_rows(t: dict) -> list:
    """Extracted table rows — sidecars use 'rows' (list of dicts) or 'data'."""
    return t.get("rows") or t.get("data") or []


def _render_table_md(t: dict, offset: int = 0, limit: int | None = None) -> str:
    """Render a sidecar table as a Markdown table, optionally paged.

    Handles rows as list-of-dicts (column→value) or list-of-lists. Returns just
    the table body (no caption).
    """
    rows = _table_rows(t)
    if not rows:
        return "(no rows)"
    window = rows[offset:] if limit is None else rows[offset: offset + limit]
    if not window:
        return "(no further rows)"

    if isinstance(window[0], dict):
        headers: list[str] = []
        for r in window:
            for k in r.keys():
                if k not in headers:
                    headers.append(str(k))
        body = [[("" if r.get(h) is None else str(r.get(h))) for h in headers] for r in window]
    else:
        headers = [str(h) for h in (t.get("headers") or [])]
        body = [[str(c) for c in (row if isinstance(row, (list, tuple)) else [row])] for row in window]
        if not headers:
            width = max(len(r) for r in body)
            headers = [f"Spalte {i + 1}" for i in range(width)]

    def _row(cells: list[str]) -> str:
        return "| " + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in cells) + " |"

    md = [_row(headers), "| " + " | ".join("---" for _ in headers) + " |"]
    md += [_row(r) for r in body]
    return "\n".join(md)


async def _tool_get_document_details(inputs: dict) -> tuple[str, list[DocumentResult]]:
    doc_id = int(inputs.get("document_id", 0))

    # Fetch via user-scoped client — acts as permission check. Resolving the
    # client stays outside the try: a missing session must surface as "sign in
    # again", not be flattened into a generic load error.
    pl = _user_paperless()
    try:
        docs = await pl.list_documents(ids=[doc_id])
        if not docs:
            return f"Dokument #{doc_id} not found or no access.", []
        doc = docs[0]
    except Exception as e:
        return f"Error loading document {doc_id}: {e}", []

    lines = [f"Details for document #{doc_id}\n"]

    # Paperless metadata
    lines += [
        "Metadata:",
        f"  Title: {doc.title}",
        f"  Correspondent: {doc.correspondent or '—'}",
        f"  Document type: {doc.document_type or '—'}",
        f"  Tags: {', '.join(doc.tags) if doc.tags else '—'}",
        f"  Created: {doc.created.strftime('%d.%m.%Y') if doc.created else '—'}",
        f"  Page count: {doc.page_count or '—'}",
        f"  Owner: {doc.owner_name or '—'}",
        "",
    ]

    # User notes from Paperless
    if doc.notes:
        lines.append(f"User notes ({len(doc.notes)}):")
        for n in doc.notes:
            user_name = (
                n.user.get("username") or n.user.get("display_name") or "Unknown"
            )
            date_str = n.created.strftime("%d.%m.%Y")
            lines.append(f"  [{date_str} – {user_name}] {n.note}")
        lines.append("")

    # AI sidecar content
    sc = sidecar_service.load_sidecar(doc_id)
    if sc:
        summary = sc.get("full_summary_summarized") or sc.get("full_summary") or ""
        if summary:
            lines += ["Summary:", summary[:500], ""]

        actions = sc.get("actions") or []
        if actions:
            lines.append(f"Actions / deadlines ({len(actions)}):")
            for a in actions:
                certain = "✓" if a.get("deadline_certain") else "~"
                lines.append(
                    f"  {certain} {a.get('description', '')} — {a.get('deadline', "no date")}"
                )
            lines.append("")

        cross_refs = sc.get("cross_refs") or []
        if cross_refs:
            lines.append(f"Cross-references ({len(cross_refs)}):")
            for r in cross_refs:
                lines.append(f"  {r.get('type', '')}: {r.get('value', '')}")
            lines.append("")

        tables = sc.get("tables") or []
        if tables:
            _PREVIEW = 6
            lines.append(f"Tables ({len(tables)}):")
            for idx, t in enumerate(tables):
                caption = t.get("caption") or t.get("title", "")
                page_n = t.get("page_number", "?")
                n_rows = len(_table_rows(t))
                lines.append(f'  [Table {idx}] page {page_n}: "{caption}" ({n_rows} rows)')
                preview = _render_table_md(t, offset=0, limit=_PREVIEW)
                lines += ["  " + ln for ln in preview.splitlines()]
                if n_rows > _PREVIEW:
                    lines.append(
                        f"  … {n_rows - _PREVIEW} more rows — full table via "
                        f"get_document_table(document_id={doc_id}, table_index={idx})"
                    )
                lines.append("")

        pages = sc.get("pages") or []
        if pages:
            lines.append("Seitenzusammenfassungen:")
            for p in pages:
                ps = p.get("page_summary", "").strip()
                if ps:
                    lines.append(f"  page {p.get('page', '?')}: {ps}")

    return "\n".join(lines), []


async def _tool_get_document_table(inputs: dict) -> tuple[str, list[DocumentResult]]:
    doc_id = int(inputs.get("document_id", 0))
    table_index = int(inputs.get("table_index", 0))
    offset = max(0, int(inputs.get("offset", 0) or 0))
    limit_raw = inputs.get("limit")
    limit = int(limit_raw) if limit_raw not in (None, "") else 100

    # Permission check via user-scoped client (resolved outside the try, so a
    # missing session surfaces as such instead of a generic access error)
    pl = _user_paperless()
    try:
        if not await pl.list_documents(ids=[doc_id]):
            return f"Dokument #{doc_id} not found or no access.", []
    except Exception as e:
        return f"Error accessing document {doc_id}: {e}", []

    sc = sidecar_service.load_sidecar(doc_id)
    tables = (sc or {}).get("tables") or []
    if not tables:
        return f"Dokument #{doc_id} has no extracted tables.", []
    if table_index < 0 or table_index >= len(tables):
        return (
            f"Invalid table_index {table_index}. Document #{doc_id} hat "
            f"{len(tables)} table(s) (0–{len(tables) - 1}).",
            [],
        )

    t = tables[table_index]
    total = len(_table_rows(t))
    caption = t.get("caption") or t.get("title", "")
    page_n = t.get("page_number", "?")

    header = (
        f'Table {table_index} — document #{doc_id}, page {page_n}: "{caption}"\n'
        f"Rows {offset}–{min(offset + limit, total) - 1 if total else 0} of {total}\n"
    )
    body = _render_table_md(t, offset=offset, limit=limit)
    footer = ""
    if offset + limit < total:
        footer = (
            f"… more rows: get_document_table(document_id={doc_id}, "
            f"table_index={table_index}, offset={offset + limit})"
        )
    return header + body + footer, []


async def _tool_get_document_page_text(
    inputs: dict,
) -> tuple[str, list[DocumentResult]]:
    doc_id = int(inputs.get("document_id", 0))
    page_number = int(inputs.get("page_number", 1))
    # Permission check — sidecar is file-based, has no access control
    if not await _user_paperless().list_documents(ids=[doc_id]):
        return f"Dokument #{doc_id} not found or no access.", []
    sc = sidecar_service.load_sidecar(doc_id)
    if sc:
        for p in sc.get("pages") or []:
            if p.get("page_number") == page_number:
                text = p.get("page_text", "").strip()
                if text:
                    return (
                        f"Page {page_number} of document #{doc_id}:\n\n{text}",
                        [],
                    )
    return (
        f"No extracted text found for page {page_number} of document #{doc_id}.",
        [],
    )


async def _tool_view_document_page(inputs: dict) -> tuple[str, list[DocumentResult]]:
    doc_id = int(inputs.get("document_id", 0))
    page_number = int(inputs.get("page_number", 1))
    question = (
        inputs.get("question")
        or "Describe all visual elements of this page: logos, graphics, stamps, signatures, layout, colors."
    )

    # Permission check before serving cached PDF
    if not await _user_paperless().list_documents(ids=[doc_id]):
        return f"Dokument #{doc_id} not found or no access.", []
    cached = os.path.join(_PDF_CACHE, f"{doc_id}.pdf")
    if not os.path.exists(cached):
        try:
            data = await paperless.download_document(doc_id)
            with open(cached, "wb") as f:
                f.write(data)
        except Exception as e:
            return f"PDF could not be loaded: {e}", []

    with open(cached, "rb") as f:
        pdf_bytes = f.read()

    from services.pdf_extractor import PDFExtractor

    page_image = PDFExtractor().extract_page(pdf_bytes, page_number)
    if page_image is None:
        return f"Page {page_number} does not exist in document {doc_id}.", []

    try:
        # Same model the user picked for ingestion, resolved through their own
        # registry so a cloud entry's API key is available here too.
        from services.vision import build_vision_client

        _vision = build_vision_client(_current_owner.get() or "", _current_token.get() or "")
        result = await _vision.describe_page(page_image, question)
        return (
            f"Vision analysis of page {page_number} (document {doc_id}):\n\n{result}",
            [],
        )
    except Exception as e:
        return f"Vision analysis failed: {e}", []


# e5 cosine distance, measured on live data: on-topic hits ≈ 0.10–0.13,
# off-topic floor ≈ 0.16+ (same calibration as the werkbank splitter hint).
_ARCHIVE_HINT_MAX_DIST = 0.15


async def _web_search_archive_hint(query: str) -> str:
    """Peek into the documents index for a web_search query.

    Prepended to web results so the model notices when the archive already
    covers the topic and can follow up with the `search` tool — same pattern
    as the brain hints on document search.
    """
    try:
        hits = await chroma.query(query_texts=[query], n_results=4)
        lines: list[str] = []
        seen: set = set()
        for h in hits[0] if hits else []:
            if h.get("distance", 1.0) > _ARCHIVE_HINT_MAX_DIST:
                continue
            doc_id = (h.get("metadata") or {}).get("paperless_id")
            if doc_id in seen:
                continue
            seen.add(doc_id)
            snippet = (h.get("document") or "").replace("\n", " ")[:160]
            lines.append(f"- Document #{doc_id}: {snippet}")
        if not lines:
            return ""
        return (
            "📁 Note: the document archive contains documents matching this query — check them with the search tool before relying on web sources:\n"
            + "\n".join(lines)
            + "\n\n"
        )
    except Exception:
        return ""


async def _tool_web_search(inputs: dict) -> tuple[str, list[WebSearchResult]]:
    query = inputs.get("query", "")
    try:
        max_results = min(int(inputs.get("max_results") or 5), 10)
    except (TypeError, ValueError):
        max_results = 5
    from werkbank.settings_store import get_archive_language

    _default_lang = get_archive_language() or "en"
    language = (inputs.get("language") or _default_lang).strip() or _default_lang
    time_range = (inputs.get("time_range") or "").strip()
    category = (inputs.get("category") or "").strip().lower()
    # Auto-use the news category for time-filtered queries — the 'general'
    # category returns overview/homepages, 'news' returns real article links + dates.
    if not category and time_range in ("day", "week", "month", "year"):
        category = "news"
    archive_hint = await _web_search_archive_hint(query)
    try:
        form = {"q": query, "format": "json", "language": language}
        if time_range in ("day", "week", "month", "year"):
            form["time_range"] = time_range
        if category in ("general", "news", "science"):
            form["categories"] = category
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{settings.searxng_host}/search", data=form)
            resp.raise_for_status()
            data = resp.json()

        range_sfx = f" [time range: {time_range}]" if time_range else ""
        lines = [f"{archive_hint}Web search: \'{query}'{range_sfx}\n"]

        # SearXNG instant answers / infoboxes — often the most direct hit
        for a in data.get("answers") or []:
            txt = a if isinstance(a, str) else (a.get("answer") or "")
            if txt:
                lines.append(f"💡 Direct answer: {txt}")
        for ib in (data.get("infoboxes") or [])[:2]:
            content = (ib.get("content") or "").strip()
            if content:
                name = ib.get("infobox", "")
                lines.append(f"ℹ️ {name}: {content[:400]}")
        if len(lines) > 1:
            lines.append("")

        hits = data.get("results", [])[:max_results]
        if not hits and len(lines) == 1:
            # SearXNG answers HTTP 200 with an empty result list when its
            # upstream engines are blocked — CAPTCHAs and rate limits, which a
            # burst of agent searches provokes. Reporting that as "nothing
            # found" turns an outage into a finding, so it is named instead.
            blocked = [
                f"{name}: {reason}"
                for name, reason in (data.get("unresponsive_engines") or [])
            ]
            if blocked:
                return (
                    f"{archive_hint}Search unavailable — every engine refused this "
                    f"request ({'; '.join(blocked[:4])}). This is not evidence "
                    f"that nothing exists; the search itself did not run.",
                    [],
                )
            return f"{archive_hint}No results found.", []
        web_results: list[WebSearchResult] = []
        for i, h in enumerate(hits, 1):
            title = h.get("title", "")
            content = (h.get("content") or "")[:300]
            url = h.get("url", "")
            published = (h.get("publishedDate") or "")[:10]
            date_sfx = f" ({published})" if published else ""
            lines.append(f"{i}. [{title}]({url}){date_sfx}")
            if content:
                lines.append(f"   {content}")
            lines.append("")
            if url:
                web_results.append(
                    WebSearchResult(
                        title=title,
                        url=url,
                        snippet=(h.get("content") or "")[:400],
                        published=published,
                        img_src=(h.get("img_src") or h.get("thumbnail") or ""),
                        engine=(h.get("engine") or ""),
                    )
                )
        await _fill_preview_images(web_results)
        return "\n".join(lines), web_results
    except Exception as e:
        return f"Web search failed: {e}", []


# ── Preview images ────────────────────────────────────────────────────────────
#
# SearXNG only passes `img_src` through when the answering engine supplies one,
# and which engines answer varies per query — hence the "sometimes all,
# sometimes none, usually some" the result list used to show. The policy is:
# every card gets a picture or the same placeholder, never a gap.
#
# Filling the blanks means fetching the target page, which tells that site
# someone searched for it. So it is limited to the first few results (the ones
# actually looked at), reads only the page head, and caches per URL.

_OG_IMAGE_TOP_N = 5
_OG_IMAGE_HEAD_BYTES = 65_536
_OG_IMAGE_CACHE: dict[str, str] = {}
_OG_IMAGE_CACHE_MAX = 512

_OG_IMAGE_RE = re.compile(
    r"""<meta[^>]+(?:property|name)\s*=\s*["'](?:og:image(?::secure_url)?|twitter:image)["']"""
    r"""[^>]+content\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
# Same tag with the attributes the other way round — both orders occur in the
# wild and a single-order regex silently misses half of them.
_OG_IMAGE_RE_REVERSED = re.compile(
    r"""<meta[^>]+content\s*=\s*["']([^"']+)["'][^>]+(?:property|name)\s*=\s*"""
    r"""["'](?:og:image(?::secure_url)?|twitter:image)["']""",
    re.IGNORECASE,
)


async def _og_image(url: str) -> str:
    """Best-effort og:image URL for a page. '' when there is none."""
    if url in _OG_IMAGE_CACHE:
        return _OG_IMAGE_CACHE[url]
    found = ""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=3.0, read=4.0, write=3.0, pool=3.0),
            follow_redirects=True,
        ) as client:
            async with client.stream(
                "GET", url, headers={"Accept": "text/html", "Range": f"bytes=0-{_OG_IMAGE_HEAD_BYTES}"}
            ) as resp:
                if resp.status_code >= 400 or "html" not in resp.headers.get("content-type", ""):
                    raise ValueError("not an HTML page")
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    # The tag lives in <head>; reading the whole page would cost
                    # far more than the picture is worth.
                    if size >= _OG_IMAGE_HEAD_BYTES:
                        break
        html = b"".join(chunks).decode("utf-8", errors="replace")
        m = _OG_IMAGE_RE.search(html) or _OG_IMAGE_RE_REVERSED.search(html)
        if m:
            found = urllib.parse.urljoin(url, _html_lib.unescape(m.group(1).strip()))
    except Exception:
        found = ""
    if len(_OG_IMAGE_CACHE) >= _OG_IMAGE_CACHE_MAX:
        _OG_IMAGE_CACHE.clear()          # crude, but this is a decoration cache
    _OG_IMAGE_CACHE[url] = found
    return found


async def _fill_preview_images(results: list) -> None:
    """Fetch missing preview images for the first few results, in parallel."""
    targets = [r for r in results[:_OG_IMAGE_TOP_N] if not r.img_src and r.url]
    if not targets:
        return
    images = await asyncio.gather(
        *(_og_image(r.url) for r in targets), return_exceptions=True
    )
    for result, image in zip(targets, images):
        if isinstance(image, str) and image:
            result.img_src = image


async def _trafilatura_fetch(url: str) -> str | None:
    import trafilatura

    loop = asyncio.get_event_loop()
    downloaded = await loop.run_in_executor(None, lambda: trafilatura.fetch_url(url))
    if not downloaded:
        return None
    text = await loop.run_in_executor(
        None,
        lambda: trafilatura.extract(
            downloaded, include_links=False, include_images=False
        ),
    )
    return text or None


async def _crawl4ai_fetch(url: str) -> str | None:
    """Fetch through headless Chromium.

    Configured to look like a browser rather than like a crawler: a real user
    agent, navigator overrides, and waiting for the network to settle. Publishers
    increasingly serve a JavaScript shell to anything that does not, which is how
    a JAMA case report came back as 603 characters of "enable JavaScript".
    """
    try:
        import trafilatura
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

        browser = BrowserConfig(
            headless=True,
            java_script_enabled=True,
            user_agent_mode="random",
            viewport_width=1280,
            viewport_height=900,
        )
        run = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            # `domcontentloaded`, not `networkidle`: measured against two
            # publisher pages, waiting for the network to fall idle returned
            # **zero** characters both times — ads and trackers keep a news or
            # journal page busy forever, so the wait times out and the page is
            # never read. With `domcontentloaded` plus a settle delay the same
            # eplasty case report came back as 6455 readable characters.
            wait_until="domcontentloaded",
            page_timeout=45_000,
            delay_before_return_html=2.0,
            magic=True,               # cookie banners, overlays, lazy content
            simulate_user=True,
            override_navigator=True,
            word_count_threshold=10,
        )

        async def _run() -> str | None:
            async with AsyncWebCrawler(config=browser) as crawler:
                result = await crawler.arun(url=url, config=run)

            # Chromium renders, trafilatura picks the article out of it. The
            # browser's own markdown is the *whole page* — on Wikipedia the
            # first 8000 characters were the navigation sidebar, so the article
            # never reached the model at all. Extracting from the rendered HTML
            # gives the same page without the chrome.
            html = getattr(result, "html", "") or getattr(result, "cleaned_html", "")
            article = None
            if html:
                loop = asyncio.get_running_loop()
                article = await loop.run_in_executor(
                    None,
                    lambda: trafilatura.extract(
                        html, include_links=False, include_images=False
                    ),
                )

            markdown = getattr(result, "markdown", None)
            # 0.9 returns a MarkdownGenerationResult; older builds a string.
            page = getattr(markdown, "fit_markdown", None) or str(markdown or "")

            # The extract is better when it worked; the raw markdown is the
            # safety net for pages trafilatura cannot parse but Chromium read.
            if article and not _unreadable_page(article):
                return article
            if page and not _unreadable_page(page):
                return page
            return article or page or None

        # Hard bound — a hanging page/browser must not stall the agentic loop
        # (werkbank workers wait on this too, with the GPU idle meanwhile).
        return await asyncio.wait_for(_run(), timeout=90)
    except Exception as exc:                                # noqa: BLE001
        _log.debug("crawl4ai fetch failed for %s: %s", url, exc)
        return None


async def _tool_web_fetch_page(inputs: dict) -> tuple[str, list[DocumentResult]]:
    url = inputs.get("url", "").strip()
    if not url:
        return "No URL given.", []
    mode = _web_fetch_mode.get()
    try:
        if mode == "crawl4ai":
            # Browser first. Slower — seconds, not milliseconds — which is the
            # right trade for a background research run: it reads pages that
            # need JavaScript, and a run waits on a model for far longer anyway.
            text = await _crawl4ai_fetch(url)
            tool_used = "Crawl4AI"
            if _unreadable_page(text or ""):
                plain = await _trafilatura_fetch(url)
                if plain and not _unreadable_page(plain):
                    text, tool_used = plain, "Trafilatura (Fallback)"
        else:
            text = await _trafilatura_fetch(url)
            tool_used = "Trafilatura"
            # The trigger is *readability*, not length. A 200-character floor let
            # a 427-character paywall stub through as if it were the article,
            # and the browser fallback — the one thing that could have opened
            # the page — never ran.
            if _unreadable_page(text or ""):
                fallback = await _crawl4ai_fetch(url)
                if fallback and not _unreadable_page(fallback):
                    text = fallback
                    tool_used = "Crawl4AI (Fallback)"
                elif fallback and len(fallback.strip()) > len((text or "").strip()):
                    text = fallback
                    tool_used = "Crawl4AI (Fallback)"

        if not text:
            return f"No text content extractable from: {url}", []
        if reason := _unreadable_page(text):
            # A bot wall is not an empty page and must not be treated as one:
            # the extractor returns a few hundred characters of "enable
            # JavaScript" or a cookie banner, and an agent that is not told what
            # happened will quote *that* as if it were the article. Observed on
            # a JAMA case report and an eplasty case study — both exactly the
            # source the question needed.
            return (
                f"Could not read {url} — {reason}. Nothing here can be quoted. "
                f"Try another source for the same fact (for a paywalled paper: "
                f"its PubMed or Europe PMC page), or record it as "
                f"source_unavailable.",
                [],
            )
        # A research run reads a page once and has to quote from it; 8000
        # characters is about 1200 words and cuts most articles mid-argument.
        # Chat keeps the smaller budget — there a human is waiting on tokens.
        limit = 16_000 if mode == "crawl4ai" else 8_000
        truncated = (
            f"\n\n[… truncated — the page is longer, this is the first {limit} characters]"
            if len(text) > limit
            else ""
        )
        return f"[{tool_used}] Content of {url}:\n\n{text[:limit]}{truncated}", []
    except Exception as e:
        return f"Web fetch failed: {e}", []


# Phrases that mean "you are talking to a wall, not to an article".
_BOT_WALL_MARKERS = (
    "enable javascript", "javascript is disabled", "please enable js",
    "checking your browser", "verify you are human", "are you a robot",
    "access denied", "403 forbidden", "cloudflare",
    "subscribe to continue", "sign in to continue", "purchase access",
    "cookies to continue", "your browser is not supported",
)
# Below this an "article" is a banner. Real prose from a case report or a news
# item runs into the thousands.
MIN_READABLE_CHARS = 900


def _unreadable_page(text: str) -> str:
    """Why this page cannot be read, or "" if it can be."""
    body = (text or "").strip()
    lowered = body.lower()
    for marker in _BOT_WALL_MARKERS:
        if marker in lowered:
            return f"the page returned a block page ({marker})"
    if len(body) < MIN_READABLE_CHARS:
        return (
            f"only {len(body)} characters came back, which is a banner or a "
            "paywall stub rather than the text"
        )
    return ""


def _tool_calculate(inputs: dict) -> str:
    import math as _math

    expr = (inputs.get("expression") or "").strip()
    if not expr:
        return "No expression given."
    safe_globals: dict = {"__builtins__": {}}
    safe_globals.update(vars(_math))
    safe_globals.update(
        {"abs": abs, "round": round, "min": min, "max": max, "sum": sum, "pow": pow}
    )
    try:
        result = eval(expr, safe_globals, {})  # noqa: S307
        return f"{expr} = {result}"
    except Exception as e:
        return f"Calculation error ({e})"


def _tool_get_current_date() -> str:
    from i18n import month_name, weekday_name

    now = datetime.now()
    return (
        f"Today is {weekday_name(now)}, {month_name(now)} {now.day}, "
        f"{now.year}, {now:%H:%M}."
    )


async def _tool_get_actions(inputs: dict) -> str:
    import json as _json
    from pathlib import Path

    index_path = (
        Path(settings.app_path) / settings.extraction_sidecar_path / "index.json"
    )
    try:
        data = _json.loads(index_path.read_text(encoding="utf-8"))
    except Exception as e:
        return f"index.json could not be loaded: {e}"

    actions: list[dict] = list(data.get("actions") or [])

    # index.json aggregates the actions of EVERY sidecar, i.e. of every document
    # in the archive regardless of owner — it is built by the superuser sync. The
    # descriptions are extracted document content, so they must pass the same
    # permission check as the documents themselves before being shown.
    from services.sidecar_service import filter_visible_actions

    actions = await filter_visible_actions(actions, _user_paperless())

    # Merge the current user's manual due-dates (kind=deadline brain notes)
    user = _current_owner.get()
    if user:
        try:
            from services.clients import brain

            for dl in await brain.get_deadlines(user):
                actions.append(
                    {
                        "paperless_id": None,
                        "deadline": dl.due,
                        "description": dl.text,
                        "deadline_certain": True,
                        "manual": True,
                    }
                )
        except Exception:
            pass

    today = datetime.now().date()

    upcoming_only = inputs.get("upcoming_only", False)
    certain_only = inputs.get("certain_only", False)
    after_str = inputs.get("after") or None
    before_str = inputs.get("before") or None

    after_date = datetime.strptime(after_str, "%Y-%m-%d").date() if after_str else None
    before_date = (
        datetime.strptime(before_str, "%Y-%m-%d").date() if before_str else None
    )

    filtered = []
    for a in actions:
        dl_str = a.get("deadline") or ""
        try:
            dl = datetime.strptime(dl_str, "%Y-%m-%d").date() if dl_str else None
        except ValueError:
            dl = None

        if upcoming_only and (dl is None or dl < today):
            continue
        if after_date and (dl is None or dl < after_date):
            continue
        if before_date and (dl is None or dl > before_date):
            continue
        if certain_only and not a.get("deadline_certain"):
            continue
        filtered.append((dl, a))

    if not filtered:
        return "No actions/deadlines found matching the filter criteria."

    filtered.sort(key=lambda x: x[0] or datetime.max.date())

    lines = [f"Actions / deadlines ({len(filtered)} entries):\n"]
    for dl, a in filtered:
        certain = "✓" if a.get("deadline_certain") else "~"
        dl_label = dl.strftime("%d.%m.%Y") if dl else "no date"
        past = " [OVERDUE]" if dl and dl < today else ""
        src = "📌 manual" if a.get("manual") else f"#{a.get('paperless_id')}"
        lines.append(
            f"  {certain} {src} | {dl_label}{past} | {a.get('description', '')}"
        )
    return "\n".join(lines)


async def _tool_search_emails(inputs: dict) -> tuple[str, list]:
    username = _current_owner.get()
    token = _current_token.get()
    if not username or not token:
        return "No active user — please sign in again.", []

    from services.credential_store import load_credentials

    creds = load_credentials(username, token)
    imap_cfg: dict = creds.get("imap", {})
    if (
        not imap_cfg.get("host")
        or not imap_cfg.get("username")
        or not imap_cfg.get("password")
    ):
        return (
            "IMAP not configured. Please store IMAP credentials under Settings (/settings).",
            [],
        )

    max_results = int(inputs.get("max_results") or 10)

    from services.imap_service import search_emails

    try:
        data = await search_emails(
            host=imap_cfg["host"],
            port=int(imap_cfg.get("port", 993)),
            username=imap_cfg["username"],
            password=imap_cfg["password"],
            inputs=inputs,
            use_ssl=bool(imap_cfg.get("use_ssl", True)),
            max_results=max_results,
        )
    except Exception as e:
        return f"IMAP error: {e}", []

    # list_folders_only diagnostic
    if inputs.get("list_folders_only"):
        folders = data.get("all_folders", [])
        if not folders:
            return "No folders found.", []
        return "Available IMAP folders:\n" + "\n".join(f"  {f}" for f in folders), []

    results = data.get("results", [])
    folder_used = data.get("folder", "?")
    total = data.get("total")
    offset = int(inputs.get("offset") or 0)

    if not results:
        return (
            f"No emails found (searched folder: {folder_used}). "
            "If the emails may be in another folder, use the 'folder' parameter or 'list_folders_only=true'.",
            [],
        )

    label = (
        inputs.get("query")
        or inputs.get("subject")
        or inputs.get("from_addr")
        or inputs.get("to_addr")
        or "latest emails"
    )
    detail = inputs.get("detail", "snippet")
    end = offset + len(results)
    pagination = f", showing {offset + 1}–{end} of {total}" if total is not None else ""
    lines = [
        f"Email search: \'{label}' — {len(results)} hits{pagination} (folder: {folder_used})",
        "IMPORTANT: emails have no document IDs. Never use #N notation for email references.\n",
    ]
    # Said outright, not left in the folder label: these hits answer a WIDER
    # query than the one that was asked, so the model must not present them as
    # matches for the original term.
    if fallback := data.get("fallback"):
        lines.insert(
            1,
            f"NOTE: no email contains '{label}'. The hits below come from a broader "
            f"search for '{fallback}' and may be unrelated — check each one before "
            f"citing it, and say so if none of them actually match.",
        )
    for i, r in enumerate(results, offset + 1):
        lines.append(f"[E{i}] {r.get('date', '')} | From: {r.get('from', '')}")
        lines.append(f"   Subject: {r.get('subject', '')}")
        body = r.get("snippet", "")
        if body:
            lines.append(f"   Body: {body if detail == 'full' else body[:300]}")
        lines.append("")
    if total is not None and end < total:
        lines.append(
            f"[Another {total - end} emails available — offset={end} for the next page]"
        )
    return "\n".join(lines), []


async def _tool_search_calendar(inputs: dict) -> tuple[str, list]:
    username = _current_owner.get()
    token = _current_token.get()
    if not username or not token:
        return "No active user — please sign in again.", []

    from services.credential_store import load_credentials

    creds = load_credentials(username, token)
    cal_cfg: dict = creds.get("calendar", {})
    if not cal_cfg:
        return (
            "Calendar not configured. Please store an iCal URL or CalDAV credentials under Settings (/settings).",
            [],
        )

    query = (inputs.get("query") or "").strip()
    date_from = (inputs.get("date_from") or "").strip()
    date_to = (inputs.get("date_to") or "").strip()

    if not query and not date_from and not date_to:
        return "No search term and no time range given.", []

    from services.caldav_service import search_events

    try:
        events = await search_events(
            config=cal_cfg, query=query, date_from=date_from, date_to=date_to
        )
    except Exception as e:
        return f"Calendar error: {e}", []

    _label_parts = []
    if query:
        _label_parts.append(f"'{query}'")
    if date_from or date_to:
        _label_parts.append(f"{date_from or '?'} – {date_to or '?'}")
    _label = ", ".join(_label_parts)

    if not events:
        return f"No calendar entries found ({_label})", []

    lines = [f"Calendar search {_label} — {len(events)} hits\n"]
    for i, ev in enumerate(events, 1):
        start = ev.get("dtstart", "")
        end = ev.get("dtend", "")
        date_range = f"{start} – {end}" if end and end != start else start
        lines.append(f"{i}. {ev.get('summary', "(no title)")} | {date_range}")
        if ev.get("location"):
            lines.append(f"   Location: {ev['location']}")
        if ev.get("description"):
            lines.append(f"   Description: {ev['description'][:200]}")
        lines.append("")
    return "\n".join(lines), []


async def _tool_remember_fact(inputs: dict) -> tuple[str, list]:
    user = _current_owner.get()
    if not user:
        return "No active user — please sign in again.", []
    from services.clients import brain

    text = inputs["text"]

    if not inputs.get("force"):
        hits = await brain.search_hints(text, user, max_results=3)
        similar = [(fid, ftxt, dist) for fid, ftxt, dist in hits if dist < 0.15]
        if similar:
            lines = ["⚠️ Very similar facts already exist:"]
            for fid, ftxt, dist in similar:
                lines.append(
                    f"  ID: {fid} | similarity: {1 - dist:.0%} | Text: {ftxt}"
                )
            lines.append("")
            lines.append("→ Call update_brain_fact to update.")
            lines.append(
                "→ Call remember_fact with force=true to create it anyway."
            )
            return "\n".join(lines), []

    from services.clients import vault_brain_writer

    fact_id = await vault_brain_writer.create_memory(
        text=text,
        tags=inputs.get("tags") or [],
        user=user,
        source_doc_id=inputs.get("source_doc_id"),
        source_page=inputs.get("source_page"),
        confidence=float(inputs.get("confidence") or 1.0),
        filename_topic=inputs.get("filename_topic"),
    )
    return f"Fact stored (ID: {fact_id}).", []


async def _tool_create_note(inputs: dict) -> tuple[str, list[DocumentResult]]:
    doc_id = int(inputs.get("document_id", 0) or 0)
    note = (inputs.get("note") or "").strip()
    if not doc_id:
        return "A document_id is required.", []
    if not note:
        return "The note text is empty — nothing was saved.", []

    # User-scoped client on purpose: Paperless attributes the note to the owner
    # of the token, so the superuser client would file every note under the
    # admin account instead of the person who asked for it.
    pl = _user_paperless()
    try:
        err = await pl.create_note(doc_id, note)
    except Exception as e:
        return f"Error saving the note on document {doc_id}: {e}", []
    if err:
        return f"Note on document {doc_id} not saved: {err}", []
    return f"Note saved on document #{doc_id}.", []


async def _tool_create_deadline(inputs: dict) -> tuple[str, list]:
    user = _current_owner.get()
    if not user:
        return "No active user — please sign in again.", []

    text = (inputs.get("text") or "").strip()
    due = (inputs.get("due") or "").strip()
    if not text or not due:
        return "Description and due date (YYYY-MM-DD) are required.", []
    try:
        datetime.strptime(due, "%Y-%m-%d")
    except ValueError:
        return f"Invalid date \'{due}\'. Expected YYYY-MM-DD.", []

    from services.clients import vault_brain_writer

    pbrain_id = await vault_brain_writer.create_deadline(
        text=text, due=due, user=user, tags=inputs.get("tags") or None
    )
    return f"Deadline stored ({due}, ID: {pbrain_id}).", []


async def _tool_search_memory(inputs: dict) -> tuple[str, list, list]:
    user = _current_owner.get()
    if not user:
        return "No active user — please sign in again.", []
    from services.brain_service import _parse_fact
    from services.clients import brain, vault_chroma
    from werkbank.settings_store import get_brain_hint_threshold, get_brain_hint_window

    query = inputs["query"]
    k = int(inputs.get("max_results") or 5)

    # Fetch brain facts with distances directly
    raw_hits: list[dict] = []
    try:
        total = await brain._chroma.count()
        if total > 0:
            where = {"$or": [{"user": {"$eq": user}}, {"common": {"$eq": True}}]}
            result = await brain._chroma.query(
                query_texts=[query],
                n_results=min(k, total),
                where=where,
            )
            raw_hits = result[0] if result else []
    except Exception:
        pass

    if not raw_hits:
        return "No relevant facts found in memory.", [], []

    brain_facts: list[BrainFactResult] = []
    lines = [f"Memory: {len(raw_hits)} relevant fact(s)\n"]
    for h in raw_hits:
        f = _parse_fact(h)
        if not f:
            continue
        dist = h.get("distance", 1.0)
        tags_str = ", ".join(f.tags) if f.tags else "–"
        src = f" [doc. #{f.source_doc_id}]" if f.source_doc_id else ""
        conf = f" ({f.confidence:.0%})" if f.confidence < 1.0 else ""
        # Deadlines carry the due date in metadata, not the body — surface it
        # so the LLM can answer "bis wann …" without it being in the text.
        due = f" | 📅 due: {f.due}" if f.kind == "deadline" and f.due else ""
        path_str = (h.get("metadata") or {}).get("path", "")
        brain_facts.append(
            BrainFactResult(
                pbrain_id=f.id,
                text=f.text,
                distance=dist,
                tags=list(f.tags),
                confidence=f.confidence,
                path=path_str,
                user=user,
            )
        )
        lines.append(
            f"• [{dist:.2f}] ID: {f.id} | {f.text}{due}{conf}{src} [{tags_str}]"
        )

    # Vault cross-preview
    vault_notes: list[VaultNoteResult] = []
    try:
        threshold = get_brain_hint_threshold()
        window = get_brain_hint_window()
        max_dist = 1.0 - threshold
        vtotal = await vault_chroma.count()
        if vtotal > 0:
            vhits = await vault_chroma.query(
                query_texts=[query],
                n_results=min(3, vtotal),
                where={"user": {"$eq": user}},
            )
            vresults = vhits[0] if vhits else []
            if vresults:
                best = vresults[0].get("distance", 1.0)
                vault_lines = []
                for h in vresults:
                    d = h.get("distance", 1.0)
                    if d > best * window or d > max_dist:
                        continue
                    m = h.get("metadata") or {}
                    path = m.get("path", "")
                    note_name = (
                        path.rsplit("/", 1)[-1].removesuffix(".md") if path else ""
                    )
                    heading = m.get("heading_path", "")
                    snippet = (h.get("document") or "")[:150]
                    heading_sfx = f" › {heading}" if heading else ""
                    vault_lines.append(
                        f"• [{d:.2f}] [[{note_name}]]{heading_sfx}: {snippet}"
                    )
                    psid = m.get("pbrain_id", "")
                    if psid:
                        vault_notes.append(
                            VaultNoteResult(
                                pbrain_id=psid,
                                path=path,
                                title=note_name,
                                snippet=snippet,
                                distance=d,
                                heading_path=heading,
                                user=user,
                            )
                        )
                if vault_lines:
                    lines.insert(
                        1,
                        "📓 Related vault notes:\n" + "\n".join(vault_lines) + "\n",
                    )
    except Exception:
        pass

    return "\n".join(lines), brain_facts, vault_notes


async def _tool_update_brain_fact(inputs: dict) -> tuple[str, list]:
    user = _current_owner.get()
    if not user:
        return "No active user — please sign in again.", []
    fact_id = (inputs.get("fact_id") or "").strip()
    new_text = (inputs.get("new_text") or "").strip()
    if not fact_id or not new_text:
        return "fact_id and new_text are required.", []
    try:
        from services.clients import vault_brain_writer

        await vault_brain_writer.update_memory(fact_id, new_text)
        return f"Fact {fact_id[:8]}… updated.", []
    except Exception as e:
        return f"Error while updating: {e}", []


async def _tool_delete_brain_fact(inputs: dict) -> tuple[str, list]:
    user = _current_owner.get()
    if not user:
        return "No active user — please sign in again.", []
    fact_id = (inputs.get("fact_id") or "").strip()
    if not fact_id:
        return "fact_id is required.", []

    try:
        from services.clients import vault_brain_writer

        await vault_brain_writer.delete_memory(fact_id)
        return f"Fact {fact_id[:8]}… deleted.", []
    except Exception as e:
        return f"Error while deleting: {e}", []


def _fmt_note_line(name: str, heading: str, psid: str, dist: float) -> str:
    """One result line. distance < 0 marks a lexical title match (not a score)."""
    score = "(title match)" if dist < 0 else f"(distance {dist:.2f})"
    return (
        f"• [[{name}]]"
        + (f" › {heading}" if heading else "")
        + (f" — pbrain_id: {psid}" if psid else "")
        + f" {score}"
    )


async def _vault_title_matches(user: str, query: str, limit: int = 5) -> list[VaultNoteResult]:
    """Lexical title lookup: notes whose filename contains every query token.

    Semantic search alone cannot reliably surface a note by its *title* — a short
    filename is diluted by the note body in the embedding, so "find the note
    called X" ranks poorly against thematically-close notes. This complements it
    with a plain substring match on note_name. Returns matches most-specific
    first, with distance = -1.0 as a title-match sentinel.
    """
    from services.clients import vault_chroma

    tokens = [t for t in re.findall(r"\w+", query.lower()) if len(t) >= 3]
    if not tokens:
        return []
    qfull = query.strip().lower()
    try:
        items = await vault_chroma.get(where={"user": {"$eq": user}})
    except Exception:
        return []
    # Keep one representative chunk per note (the lowest chunk_index) for the snippet.
    by_note: dict[str, dict] = {}
    for it in items:
        m = it.get("metadata") or {}
        pid = m.get("pbrain_id")
        if not pid:
            continue
        idx = m.get("chunk_index", 0)
        cur = by_note.get(pid)
        if cur is None or idx < (cur.get("metadata") or {}).get("chunk_index", 0):
            by_note[pid] = it
    scored: list[tuple[int, VaultNoteResult]] = []
    for pid, it in by_note.items():
        m = it.get("metadata") or {}
        name = (m.get("note_name") or "").strip()
        nl = name.lower()
        if not nl or not all(t in nl for t in tokens):
            continue
        # Prefer a full-phrase hit, then shorter (more precise) titles.
        score = 100 + (50 if qfull in nl else 0) - len(name)
        scored.append((
            score,
            VaultNoteResult(
                pbrain_id=pid,
                path=m.get("path", ""),
                title=name,
                snippet=(it.get("document") or "")[:300],
                distance=-1.0,
                heading_path=m.get("heading_path", ""),
                user=user,
            ),
        ))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:limit]]


async def _tool_vault_search(inputs: dict) -> tuple[str, list]:
    user = _current_owner.get()
    if not user:
        return "No active user — please sign in again.", []
    from services.clients import vault_chroma

    pbrain_id = (inputs.get("pbrain_id") or "").strip()

    # Modus 2: vollständiger Abruf einer Notiz per pbrain_id
    if pbrain_id:
        try:
            items = await vault_chroma.get(where={"pbrain_id": {"$eq": pbrain_id}})
            if not items:
                return f"No note with pbrain_id \'{pbrain_id}\' found.", []
            items_sorted = sorted(
                items, key=lambda x: (x.get("metadata") or {}).get("chunk_index", 0)
            )
            m0 = items_sorted[0].get("metadata") or {}
            path = m0.get("path", "")
            lines = [f"Note: {path} (pbrain_id: {pbrain_id}, {len(items_sorted)} Chunks)\n"]
            for item in items_sorted:
                m = item.get("metadata") or {}
                heading = m.get("heading_path", "")
                if heading:
                    lines.append(f"### {heading}")
                lines.append(item.get("document", ""))
            return "\n".join(lines), []
        except Exception as e:
            return f"Vault retrieval failed: {e}", []

    # Modus 1: semantische Suche + lexikalische Titel-Treffer
    query = (inputs.get("query") or "").strip()
    if not query:
        return "query or pbrain_id required.", []
    k = int(inputs.get("max_results") or 5)
    try:
        total = await vault_chroma.count()
        if total == 0:
            return "No notes indexed in the vault.", []
        hits = await vault_chroma.query(
            query_texts=[query],
            n_results=min(k, total),
            where={"user": {"$eq": user}},
        )
    except Exception as e:
        return f"Vault search failed: {e}", []

    # Title matches first: a note whose filename matches the query is almost
    # always what the user means ("find my note called X"), and vector distance
    # ranks it poorly against thematically-close notes. Then semantic hits, minus
    # any note already surfaced by title.
    title_hits = await _vault_title_matches(user, query)
    title_ids = {r.pbrain_id for r in title_hits}

    notes: list[VaultNoteResult] = list(title_hits)
    for h in hits[0]:
        m = h.get("metadata") or {}
        psid = m.get("pbrain_id", "")
        if psid and psid in title_ids:
            continue
        path = m.get("path", "")
        name = path.rsplit("/", 1)[-1].removesuffix(".md") if path else psid[:8]
        notes.append(
            VaultNoteResult(
                pbrain_id=psid,
                path=path,
                title=name,
                snippet=(h.get("document") or "")[:300],
                distance=h.get("distance", 1.0),
                heading_path=m.get("heading_path", ""),
                user=user,
            )
        )

    if not notes:
        return "No relevant notes found.", []

    lines = [f"Vault: {len(notes)} hits\n"]
    for r in notes:
        # Lead with the note; put the id in an unmistakable labelled form and the
        # score last as a parenthesised word — NOT a leading "[0.18]" bracket,
        # which weak models copied into the pbrain_id argument.
        lines.append(_fmt_note_line(r.title, r.heading_path, r.pbrain_id, r.distance))
        lines.append(f"  {r.snippet}")

    # Brain cross-preview
    try:
        from services.clients import brain
        from werkbank.settings_store import (
            get_brain_hint_threshold,
            get_brain_hint_window,
        )

        threshold = get_brain_hint_threshold()
        window = get_brain_hint_window()
        max_dist = 1.0 - threshold
        bhits = await brain.search_hints(query, user, max_results=3)
        if bhits:
            best = bhits[0][2]
            brain_lines = []
            for _, text, d in bhits:
                if d > best * window or d > max_dist:
                    continue
                brain_lines.append(f"• [{d:.2f}] {text}")
            if brain_lines:
                lines.insert(
                    1,
                    "💡 Related memory facts:\n" + "\n".join(brain_lines) + "\n",
                )
    except Exception:
        pass

    return "\n".join(lines), notes


# ── Helpers ───────────────────────────────────────────────────────────────────


def _with_cache_marker(messages: list[dict]) -> list[dict]:
    """Copy of messages with an ephemeral cache breakpoint on the last content
    block. Together with the breakpoint on the system prompt this lets every
    agentic iteration reuse the previous one's prefix (tools + system + history)
    instead of re-paying the full input each round."""
    if not messages:
        return messages
    out = list(messages)
    last = dict(out[-1])
    content = last.get("content")
    if isinstance(content, str):
        blocks: list[dict] = [{"type": "text", "text": content}]
    elif isinstance(content, list):
        blocks = [dict(b) for b in content]
    else:
        return out
    if not blocks:
        return out
    blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
    last["content"] = blocks
    out[-1] = last
    return out


def _block_to_dict(block) -> dict:
    if block.type == "text":
        return {"type": "text", "text": block.text}
    if block.type == "tool_use":
        return {
            "type": "tool_use",
            "id": block.id,
            "name": block.name,
            "input": block.input,
        }
    if block.type == "thinking":
        # The signature must survive the round trip. When a thinking block is
        # replayed next to a tool_use block the API verifies it, and rejects the
        # request outright if it is missing.
        out: dict = {"type": "thinking", "thinking": block.thinking}
        if sig := getattr(block, "signature", None):
            out["signature"] = sig
        return out
    if block.type == "redacted_thinking":
        return {"type": "redacted_thinking", "data": block.data}
    return {"type": block.type}


def _to_ollama_tools(definitions: list[dict]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": d["name"],
                "description": d["description"],
                "parameters": dict(d["input_schema"]),
            },
        }
        for d in definitions
    ]


# ── Claude backend ────────────────────────────────────────────────────────────


_CLAUDE_CTX: list[tuple[str, int]] = [
    ("claude-4", 200_000),
    ("claude-3", 200_000),
    ("claude-2.1", 200_000),
    ("claude-2", 100_000),
    ("claude-instant", 100_000),
]


def _claude_ctx_window(model: str, override: int = 0) -> int:
    if override:
        return override
    for prefix, ctx in _CLAUDE_CTX:
        if model.startswith(prefix):
            return ctx
    return 200_000


# Anthropic's floor for `budget_tokens`; anything smaller is rejected.
MIN_THINKING_BUDGET = 1024
DEFAULT_THINKING_BUDGET = 4096
# Headroom for the answer on top of the thinking budget — max_tokens covers both.
_ANSWER_HEADROOM = 8_000


class ClaudeChatBackend:
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "",
        context_window: int = 0,
        think: bool | None = None,
        thinking_budget: int = 0,
    ):
        import anthropic

        kwargs: dict = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = anthropic.AsyncAnthropic(**kwargs)
        self.model = model
        self.context_window = _claude_ctx_window(model, context_window)
        # None = say nothing and let the model/endpoint decide. That is what the
        # backend did before this option existed: Claude then never thinks, and
        # a MiniMax-style endpoint thinks whenever it feels like it — which is
        # exactly the "thinks only sometimes" behaviour the flag exists to fix.
        self.think = think
        self.thinking_budget = max(thinking_budget or DEFAULT_THINKING_BUDGET, MIN_THINKING_BUDGET)

    def _thinking_params(self, temperature: float) -> tuple[dict, float, int]:
        """Return (extra kwargs, effective temperature, max_tokens)."""
        max_tokens = 12_000
        if self.think is None:
            return {}, temperature, max_tokens
        if not self.think:
            return {"thinking": {"type": "disabled"}}, temperature, max_tokens
        # max_tokens must exceed budget_tokens, and extended thinking only runs
        # at temperature 1 — the API rejects any other value instead of clamping.
        return (
            {"thinking": {"type": "enabled", "budget_tokens": self.thinking_budget}},
            1.0,
            max(max_tokens, self.thinking_budget + _ANSWER_HEADROOM),
        )

    async def run_turn(
        self,
        messages: list[dict],
        system: str,
        temperature: float = 0.7,
        tools: list[dict] | None = None,
        max_iterations: int = MAX_ITERATIONS,
        answer_language: str = "",
    ) -> AsyncGenerator[ChatEvent, None]:
        active_tools = tools if tools is not None else TOOL_DEFINITIONS
        # Cache breakpoint at the end of the system prompt: tools + system form a
        # static prefix reused across all iterations of the agentic loop (and
        # across turns while the 5-min cache TTL holds).
        system_blocks = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
        working = list(messages)
        usage = 0
        extra_kwargs, temperature, max_tokens = self._thinking_params(temperature)

        for _i in range(max_iterations):
            yield IterationEvent(_i + 1)

            # Buffer text — stop_reason unknown until stream ends.
            # Intermediate (tool_use) → ThinkingEvent; final (end_turn) → TextTokenEvent.
            # Thinking deltas (MiniMax M-series and other reasoning models on
            # Anthropic-compatible endpoints; plain Claude sends none) are
            # streamed live and guarded against runaway loops.
            text_chunks: list[str] = []
            think_parts: list[str] = []
            think_yield_buf: list[str] = []
            loop_detected = False
            async with self.client.messages.stream(
                model=self.model,
                system=system_blocks,
                messages=_with_cache_marker(working),
                tools=active_tools,
                max_tokens=max_tokens,
                temperature=temperature,
                **extra_kwargs,
            ) as stream:
                async for event in stream:
                    if event.type != "content_block_delta":
                        continue
                    d = event.delta
                    if d.type == "text_delta":
                        text_chunks.append(d.text)
                    elif d.type == "thinking_delta":
                        think_parts.append(d.thinking)
                        think_yield_buf.append(d.thinking)
                        if sum(len(x) for x in think_yield_buf) >= 400:
                            yield ThinkingEvent(text="".join(think_yield_buf))
                            think_yield_buf.clear()
                            if _thinking_runaway("".join(think_parts)):
                                loop_detected = True
                                break
                if think_yield_buf:
                    yield ThinkingEvent(text="".join(think_yield_buf))
                if loop_detected:
                    await stream.close()
                else:
                    final = await stream.get_final_message()

            if loop_detected:
                _log.warning(
                    "Thought loop detected (%s), injecting correction", self.model
                )
                yield ThinkingEvent(
                    text="\n[Gedankenschleife erkannt — Modell wird unterbrochen]"
                )
                working.append({"role": "user", "content": _LOOP_CORRECTION_MSG})
                continue

            accumulated = "".join(text_chunks)
            # Cached tokens are reported separately — fold them back in so the
            # context-usage display keeps reflecting the true prompt size.
            _u = final.usage
            usage = (
                _u.input_tokens
                + _u.output_tokens
                + (getattr(_u, "cache_read_input_tokens", 0) or 0)
                + (getattr(_u, "cache_creation_input_tokens", 0) or 0)
            )

            if final.stop_reason == "end_turn":
                if accumulated:
                    yield TextTokenEvent(accumulated)
                yield DoneEvent(input_tokens=usage, context_window=self.context_window)
                return

            if final.stop_reason == "tool_use":
                if accumulated:
                    # intermediate narration — route to Gedanken
                    yield ThinkingEvent(text=accumulated)

                tool_blocks = [b for b in final.content if b.type == "tool_use"]
                working.append(
                    {
                        "role": "assistant",
                        "content": [_block_to_dict(b) for b in final.content],
                    }
                )

                tool_results = []
                for block in tool_blocks:
                    yield ToolCallEvent(
                        label=_tool_label(block.name),
                        tool_name=block.name,
                        tool_input=block.input,
                    )
                    result_text, docs, extras = await execute_tool(
                        block.name, block.input
                    )
                    yield ToolResultEvent(tool_output=result_text[:2000])
                    if docs:
                        yield DocsRetrievedEvent(docs)
                    for extra in extras:
                        yield extra
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_text,
                        }
                    )

                # Re-assert the answer language in the same user message as the
                # tool results (a separate one would break Anthropic's role
                # alternation). Text blocks must follow the tool_result blocks.
                if answer_language:
                    from i18n import answer_language_reminder

                    tool_results.append({
                        "type": "text",
                        "text": answer_language_reminder(answer_language),
                    })
                working.append({"role": "user", "content": tool_results})
                continue

            # Other stop reasons (max_tokens, …): don't swallow what was
            # generated — an empty answer looks like a hang to the user.
            if accumulated:
                yield TextTokenEvent(accumulated)
            if final.stop_reason == "max_tokens":
                yield TextTokenEvent(
                    "\n\n*[Response was cut off at the token limit.]*"
                )
            yield DoneEvent(input_tokens=usage, context_window=self.context_window)
            return

        yield DoneEvent(input_tokens=usage, context_window=self.context_window)


# ── OpenAI-compatible backend (Ollama /v1, MiniMax, Moonshot, …) ──────────────


async def _fetch_ollama_ctx(base_url: str, model: str) -> tuple[int, bool]:
    """Probe Ollama for the model's actual context length.

    Returns ``(context_window, authoritative)``. ``authoritative`` is True when
    the value reflects the context the model is *actually* running with
    (/api/ps for a loaded model, or an explicit Modelfile num_ctx). It is False
    for the GGUF native ceiling, which is only an upper bound and is wrong
    whenever the server runs the model at a smaller OLLAMA_CONTEXT_LENGTH set
    via env/flag (not in the Modelfile). Callers should keep re-probing until
    they get an authoritative value. Returns ``(0, False)`` on failure.

    Priority:
    1. /api/ps — actual runtime context for currently loaded model (authoritative)
    2. /api/show parameters.num_ctx — explicitly configured in Modelfile (authoritative)
    3. /api/show model_info.*.context_length — GGUF ceiling (provisional)
    """
    import re as _re

    server = _re.sub(r"/v1/?$", "", base_url.rstrip("/"))

    # 1. /api/ps: actual loaded context (what `ollama ps` shows) — authoritative
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{server}/api/ps")
        if resp.status_code == 200:
            for entry in resp.json().get("models") or []:
                if entry.get("name") == model or entry.get("model") == model:
                    # newer Ollama exposes num_ctx directly on the entry
                    for field in ("num_ctx", "context_length"):
                        v = entry.get(field)
                        if v:
                            return int(v), True
                    # also check nested details / model_info
                    for sub in (
                        entry.get("details") or {},
                        entry.get("model_info") or {},
                    ):
                        for k, v in sub.items():
                            if "context_length" in k or k == "num_ctx":
                                try:
                                    return int(v), True
                                except (TypeError, ValueError):
                                    pass
    except Exception:
        pass

    # 2+3. /api/show
    ceiling = 0
    for body in ({"model": model}, {"name": model}):
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(f"{server}/api/show", json=body)
                if resp.status_code != 200:
                    continue
                data = resp.json()
            # parameters string: "num_ctx 131072" — explicitly configured, beats GGUF ceiling
            params = data.get("parameters") or ""
            m = _re.search(r"num_ctx\s+(\d+)", params)
            if m:
                return int(m.group(1)), True
            # model_info / modelinfo: GGUF native ceiling — provisional upper bound only
            for key in ("model_info", "modelinfo"):
                info = data.get(key) or {}
                for k, v in info.items():
                    if "context_length" in k:
                        try:
                            ceiling = int(v)
                        except (TypeError, ValueError):
                            pass
        except Exception:
            continue
    return ceiling, False


def _is_thinking_model(model: str) -> bool:
    """Heuristic: detect models that have extended-thinking enabled by default."""
    _lower = model.lower()
    return any(k in _lower for k in ("qwen3", "qwen-3", "deepseek-r", "qwq"))


# ── Tool-use guard ────────────────────────────────────────────────────────────
#
# Some models answer a question about the user's own documents, mail or
# purchases straight out of their weights — inventing an answer that reads
# exactly like a real one ("You bought the Pediro Pro 2.0"). The cheap defence
# is a heuristic on the *request*: a first-person question about a personal
# fact cannot be answered without a tool, so if the model produced no tool call
# at all, ask once more with tool use required.
#
# Deliberately not a hallucination detector and not an LLM judge — this looks at
# the question, never at the answer, and costs one regex.

_PERSONAL_MARKERS = re.compile(
    # German possessives spelled out rather than `mein\w*`: that would also
    # match the verb ("was meinst du?"), which is not a personal fact at all.
    r"\b(mein|meine|meinen|meinem|meiner|meines|bei\s+mir)\b"
    # German puts the participle at the end ("den ich gekauft habe"), so the
    # pronoun and the verb are not adjacent — hence the bounded window.
    r"|\bich\b.{0,40}?\b(hab|habe|hatte|bin|war|bekam|kaufte|gekauft|bestellt"
    r"|bezahlt|erhalten|geschrieben|geschickt|abgeschlossen)\b"
    r"|\b(my|mine)\b"
    r"|\b(did|do|does)\s+i\b"
    r"|\bi\s+(have|had|bought|got|ordered|paid|received|sent|wrote)\b",
    re.IGNORECASE,
)

# General-knowledge questions must not trip the guard: "What is a torque
# wrench?" needs no tool, and forcing one would waste a turn and a search.
_GENERAL_QUESTION = re.compile(
    r"^\s*(was ist|was sind|wie funktioniert|wie viel wiegt|erkläre|erklär|"
    r"what is|what are|how does|how do|explain|define)\b",
    re.IGNORECASE,
)


def _needs_tool_use(text: str) -> bool:
    """True when the question is about the user's own data.

    Such a question is unanswerable from model weights by construction: the
    model has never seen this user's mail, invoices or calendar.
    """
    if not text or _GENERAL_QUESTION.match(text):
        return False
    return bool(_PERSONAL_MARKERS.search(text))


def _last_user_text(messages: list[dict]) -> str:
    """Plain text of the most recent user message ('' if there is none)."""
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
    return ""


def _api_error_text(body: str) -> str:
    """Pull the human-readable message out of an OpenAI-style error body.

    Both shapes are in the wild: {"error": {"message": ...}} (OpenAI, Ollama)
    and {"error": "..."} (Ollama's native endpoint, several gateways).
    """
    try:
        err = (json.loads(body) or {}).get("error")
    except (json.JSONDecodeError, ValueError, AttributeError):
        return body.strip()[:500]
    if isinstance(err, dict):
        return str(err.get("message") or err)[:500]
    if err:
        return str(err)[:500]
    return body.strip()[:500]


_TOOL_REMINDER = (
    "Reminder: statements about this user's documents, emails, purchases, "
    "appointments or other personal facts may only be based on tool results. "
    "Use a tool now. If it returns nothing, say that nothing was found — never "
    "fill the gap from your own knowledge."
)


class OpenAICompatibleChatBackend:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        max_output_tokens: int | None = None,
        think: bool | None = None,
        supports_tools: bool = True,
        tool_choice_mode: str = "auto",
        force_tool_first_turn: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        # max_output_tokens caps generation (thinking + response).
        # Default 16 384 prevents runaway thinking models from blocking forever.
        self.max_output_tokens = max_output_tokens or 16_384
        self.think = think  # None=model-default, True/False=explicit override
        # Per-model tool behaviour. "OpenAI-compatible" is a claim, not a
        # guarantee: some gateways drop `tools` unless `tool_choice` is set,
        # some models never call one, and a model that cannot use tools is
        # better off not being offered them than answering from memory.
        self.supports_tools = supports_tools
        self.tool_choice_mode = tool_choice_mode or "auto"
        self.force_tool_first_turn = force_tool_first_turn
        self.context_window = 0
        self._ctx_authoritative = False

    async def _ensure_ctx(self) -> None:
        """Probe Ollama for the context window, keep retrying until authoritative.

        The first probe usually runs before the model is loaded, so /api/ps is
        empty and we only get the GGUF ceiling (provisional). Once the model is
        loaded (after the first inference) /api/ps reports the real runtime
        context, so we re-probe each turn until we have an authoritative value.
        """
        if self._ctx_authoritative:
            return
        cw, authoritative = await _fetch_ollama_ctx(self.base_url, self.model)
        if cw:
            self.context_window = cw
        self._ctx_authoritative = authoritative

    async def run_turn(
        self,
        messages: list[dict],
        system: str,
        temperature: float = 0.7,
        tools: list[dict] | None = None,
        max_iterations: int = MAX_ITERATIONS,
        answer_language: str = "",
    ) -> AsyncGenerator[ChatEvent, None]:
        await self._ensure_ctx()
        _tools_list = tools if tools is not None else TOOL_DEFINITIONS
        if not self.supports_tools:
            _tools_list = []
        active_tools = _to_ollama_tools(_tools_list)
        _allowed_tool_names: set[str] = {t["name"] for t in _tools_list}
        working = [{"role": "system", "content": system}] + list(messages)
        headers: dict = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        # Guard state, for the whole turn: at most one forced retry, so a model
        # that refuses to call tools costs one extra request, not a loop.
        guard_applies = bool(active_tools) and _needs_tool_use(_last_user_text(messages))
        force_tools = guard_applies and self.force_tool_first_turn
        guard_retried = False

        prompt_tokens = 0
        for _i in range(max_iterations):
            yield IterationEvent(_i + 1)
            _wol_touch()

            payload: dict = {
                "model": self.model,
                "messages": working,
                "stream": True,
                "stream_options": {"include_usage": True},
                "temperature": temperature,
                "max_tokens": self.max_output_tokens,
            }
            if active_tools:
                payload["tools"] = active_tools
                # Sent explicitly: several gateways ignore `tools` entirely
                # unless `tool_choice` is present, which looks exactly like a
                # model that chose not to call anything.
                payload["tool_choice"] = "required" if force_tools else self.tool_choice_mode
            if self.think is not None:
                payload["think"] = self.think

            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            inline_think_parts: list[str] = []
            think_yield_buf: list[str] = []
            tool_calls_map: dict[int, dict] = {}
            in_think_block = False
            think_yielded = False
            think_chars = 0
            loop_detected = False

            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=5.0)
            ) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                ) as resp:
                    if resp.status_code >= 400:
                        await resp.aread()
                        # httpx's own message is only status + URL, which hides
                        # the one useful part: the server says *why* it refused
                        # (bad payload shape, model not found, context overflow).
                        raise RuntimeError(
                            f"{resp.status_code} from {self.base_url}: "
                            f"{_api_error_text(resp.text)}"
                        )

                    async for raw_line in resp.aiter_lines():
                        line = raw_line.strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                        except (json.JSONDecodeError, ValueError):
                            continue

                        choice = (chunk.get("choices") or [{}])[0]
                        delta = choice.get("delta") or {}

                        # Dedicated reasoning field (DeepSeek-R1, some Qwen configs)
                        for key in ("reasoning_content", "thinking", "reasoning"):
                            r = delta.get(key) or ""
                            if r:
                                reasoning_parts.append(r)
                                think_yield_buf.append(r)
                                think_yielded = True
                                think_chars += len(r)
                                break
                        if (
                            think_chars > _THINK_SOFT_CAP
                            and not loop_detected
                            and _thinking_runaway("".join(reasoning_parts))
                        ):
                            loop_detected = True
                            await resp.aclose()
                            break

                        # Content delta
                        c = delta.get("content") or ""
                        if c:
                            content_parts.append(c)
                            # Detect inline <think> tags (Qwen3, etc.) —
                            # only when no dedicated reasoning field seen yet
                            if not think_yielded:
                                full = "".join(content_parts).lstrip()
                                if not in_think_block and full.startswith(
                                    ("<think>", "<thinking>")
                                ):
                                    in_think_block = True
                                if in_think_block:
                                    if re.search(r"</think(?:ing)?>", full):
                                        in_think_block = False
                                    else:
                                        think_yield_buf.append(c)
                                        inline_think_parts.append(c)
                                        think_chars += len(c)
                                        if think_chars > _THINK_SOFT_CAP and _thinking_runaway(
                                            "".join(inline_think_parts)
                                        ):
                                            loop_detected = True
                                            await resp.aclose()
                                            break

                        # Yield thinking in ~400-char batches so stop check can fire
                        if sum(len(x) for x in think_yield_buf) >= 400:
                            yield ThinkingEvent(text="".join(think_yield_buf))
                            think_yield_buf.clear()

                        # Tool call deltas
                        for tc_delta in delta.get("tool_calls") or []:
                            idx = tc_delta.get("index", 0)
                            if idx not in tool_calls_map:
                                tool_calls_map[idx] = {
                                    "id": "",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            if tc_delta.get("id"):
                                tool_calls_map[idx]["id"] = tc_delta["id"]
                            fn_d = tc_delta.get("function") or {}
                            if fn_d.get("name"):
                                tool_calls_map[idx]["function"]["name"] += fn_d["name"]
                            if fn_d.get("arguments"):
                                tool_calls_map[idx]["function"]["arguments"] += fn_d[
                                    "arguments"
                                ]

                        usage_d = chunk.get("usage") or {}
                        if usage_d.get("prompt_tokens"):
                            prompt_tokens = usage_d["prompt_tokens"]

                    # Flush remaining thinking buffer
                    if think_yield_buf:
                        yield ThinkingEvent(text="".join(think_yield_buf))

            # Model is now loaded — re-probe /api/ps for the real runtime context
            # (the start-of-turn probe ran before load and may only have the
            # GGUF ceiling). Fires before any DoneEvent below, so even the first
            # turn reports the correct context window.
            await self._ensure_ctx()

            if loop_detected:
                _log.warning(
                    "Thought loop detected (%s), injecting correction", self.model
                )
                yield ThinkingEvent(
                    text="\n[Gedankenschleife erkannt — Modell wird unterbrochen]"
                )
                working.append({"role": "user", "content": _LOOP_CORRECTION_MSG})
                continue

            raw_content = "".join(content_parts)
            raw_reasoning = "".join(reasoning_parts)
            raw_tool_calls = [tool_calls_map[i] for i in sorted(tool_calls_map)]

            # If thinking came from a dedicated reasoning field, strip tags from content.
            # If thinking came from <think> tags in content, extract them now.
            if raw_reasoning:
                content = _THINK_RE.sub("", raw_content).strip()
            else:
                extra_thinking, content = _extract_thinking(raw_content, "")
                if extra_thinking and not think_yielded:
                    yield ThinkingEvent(text=extra_thinking)

            if not raw_tool_calls:
                # The model answered a question about the user's own data
                # without looking anything up — so whatever it said, it made up.
                # Ask once more, with tool use required.
                if guard_applies and not guard_retried:
                    guard_retried = True
                    force_tools = True
                    _log.info(
                        "Tool-use guard: %s answered a personal question with no "
                        "tool call — retrying with tool_choice=required",
                        self.model,
                    )
                    # Role "user", not "system": Ollama's OpenAI-compatible
                    # endpoint rejects a system message anywhere but position 0
                    # with a 500 ("system message must be at the beginning").
                    working.append({"role": "user", "content": _TOOL_REMINDER})
                    continue
                if content:
                    yield TextTokenEvent(content)
                yield DoneEvent(
                    input_tokens=prompt_tokens, context_window=self.context_window
                )
                return

            # A tool ran, so the answer will be grounded: release the guard, and
            # above all stop forcing tool use — otherwise the model could never
            # produce the final answer it now has the material for.
            guard_applies = False
            force_tools = False

            if content:
                # intermediate narration between tool calls — treat as thinking
                yield ThinkingEvent(text=content)

            working.append(
                {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": raw_tool_calls,
                }
            )

            for tc in raw_tool_calls:
                tc_id = tc.get("id", "")
                fn = tc.get("function", {})
                tool_name = fn.get("name", "")
                tool_args = fn.get("arguments", {})
                if isinstance(tool_args, str):
                    tool_args = _parse_tool_arguments(tool_args)

                if tool_name not in _allowed_tool_names:
                    working.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": f"Tool '{tool_name}' ist deaktiviert.",
                        }
                    )
                    continue

                yield ToolCallEvent(
                    label=_tool_label(tool_name),
                    tool_name=tool_name,
                    tool_input=tool_args,
                )
                result_text, docs, extras = await execute_tool(tool_name, tool_args)
                yield ToolResultEvent(tool_output=result_text[:2000])
                if docs:
                    yield DocsRetrievedEvent(docs)
                for extra in extras:
                    yield extra

                working.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": result_text,
                    }
                )

            # Last thing before the model writes its answer — see
            # answer_language_reminder(): the rule is in the system prompt, but
            # a German document summary sitting right here outweighs it.
            # Role "user": Ollama's OpenAI-compatible endpoint 500s on a system
            # message that is not the first one ("system message must be at the
            # beginning"), so a mid-conversation reminder can never be one.
            if answer_language:
                from i18n import answer_language_reminder

                working.append({
                    "role": "user",
                    "content": answer_language_reminder(answer_language),
                })

        yield DoneEvent(input_tokens=prompt_tokens, context_window=self.context_window)
