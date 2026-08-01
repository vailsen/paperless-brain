# app_ui/pages/chat.py

import asyncio
import html as _html
import json
import re
import urllib.parse

from nicegui import app as ng_app
from nicegui import ui

from app_ui.cluster_dialog import create_cluster_dialog
from app_ui.document_dialog import create_document_dialog
from app_ui.layout import page_layout, require_auth
from app_ui.pages.browser import _render_card
from config.chat_prompts import build_system_prompt
from config.settings import settings
from i18n import DEFAULT_LANG, get_translator
from models.brain_fact_result import BrainFactResult
from models.result_document import DocumentResult
from models.vault_note_result import VaultNoteResult
from services import sync_state
from services.session_auth import get_session_token

# ui.markdown default extras + LaTeX ($$...$$ → MathML via latex2mathml)
_MD_EXTRAS = ["fenced-code-blocks", "tables", "latex"]
from services.chat_service import (
    TOOL_DEFINITIONS,
    ClaudeChatBackend,
    DocsRetrievedEvent,
    DocxRequestEvent,
    DoneEvent,
    DownloadRequestEvent,
    EmailRequestEvent,
    IterationEvent,
    OpenAICompatibleChatBackend,
    PdfSaveRequestEvent,
    KanbanTaskRequestEvent,
    ThinkingEvent,
    TextTokenEvent,
    ToolCallEvent,
    ToolResultEvent,
    BrainFactsRetrievedEvent,
    VaultNotesRetrievedEvent,
    WebResultsRetrievedEvent,
    _current_owner,
    _current_token,
    _web_fetch_mode,
)
from services.clients import get_session_paperless
from services.credential_store import load_credentials, save_credentials
import services.chat_history_service as _chat_hist

_DOC_ID_RE = re.compile(r"(?<!\]\()(?<![`\w])(#)(\d+)\b")
_VAULT_LINK_RE = re.compile(r"\[([^\]]+)\]\(vault:([a-f0-9-]{32,})\)")
_VAULT_WIKI_RE = re.compile(r"`?\[\[([^\]|]+?)(?:\|[^\]]+)?\]\]`?")
_MD_TABLE_RE = re.compile(r"^\|.+\|[ \t]*\n\|[-: |]+\|", re.MULTILINE)


def _extract_markdown_csvs(text: str) -> list[bytes]:
    """Return UTF-8-BOM CSV bytes for every markdown table found in text."""
    import csv
    import io

    def _cells(line: str) -> list[str]:
        return [c.strip() for c in line.strip().strip("|").split("|")]

    def _table_to_csv(lines: list[str]) -> bytes | None:
        if len(lines) < 2:
            return None
        header = _cells(lines[0])
        rows = [
            _cells(row)
            for row in lines[2:]
            if row.strip() and not re.fullmatch(r"[\| \-:]+", row.strip())
        ]
        buf = io.StringIO()
        csv.writer(buf).writerows([header] + rows)
        return buf.getvalue().encode("utf-8-sig")

    results = []
    pos = 0
    while True:
        m = _MD_TABLE_RE.search(text, pos)
        if not m:
            break
        start = text.rfind("\n", 0, m.start()) + 1
        lines: list[str] = []
        for line in text[start:].splitlines():
            if line.strip().startswith("|"):
                lines.append(line)
            elif lines:
                break
        csv_bytes = _table_to_csv(lines)
        if csv_bytes:
            results.append(csv_bytes)
        # advance past this table
        pos = start + sum(len(ln) + 1 for ln in lines)
    return results


def _inject_doc_links(text: str, vault_index: dict | None = None) -> str:
    def _vault_a(psid: str, label: str) -> str:
        return (
            f'<a href="#" data-vault-id="{psid}" '
            f'style="color:#60a5fa;text-decoration:underline;cursor:pointer;'
            f'touch-action:manipulation;">{label}'
            f'<span style="font-size:0.7em;vertical-align:super;margin-left:2px;opacity:0.7;">↗</span>'
            f'</a>'
        )

    # Replace explicit [name](vault:uuid) links the LLM may have written
    text = _VAULT_LINK_RE.sub(
        lambda m: _vault_a(m.group(2), m.group(1).replace("**", "")),
        text,
    )

    # Convert [[note-name]] wiki-links the LLM may write → clickable vault links
    if vault_index:
        def _wiki_replace(m: re.Match) -> str:
            name = m.group(1).strip()
            psid = vault_index.get(name.lower())
            if psid:
                return _vault_a(psid, name)
            return m.group(0)  # unknown note → leave as-is
        text = _VAULT_WIKI_RE.sub(_wiki_replace, text)

    return _DOC_ID_RE.sub(
        lambda m: (
            f'<a href="#" data-doc-id="{m.group(2)}" '
            f'style="color:#a78bfa;text-decoration:underline;cursor:pointer;'
            f'touch-action:manipulation;">#{m.group(2)}'
            f'<span style="font-size:0.7em;vertical-align:super;margin-left:2px;opacity:0.7;">↗</span>'
            f'</a>'
        ),
        text,
    )


async def _resolve_vault_wiki_links(text: str, index: dict, username: str) -> None:
    """Query vault_chroma for any [[name]] in text not yet in index, add pbrain_id."""
    from services.clients import vault_chroma
    unresolved = [
        m.group(1).strip()
        for m in _VAULT_WIKI_RE.finditer(text)
        if m.group(1).strip().lower() not in index
    ]
    if not unresolved or not username:
        return
    try:
        total = await vault_chroma.count()
        if not total:
            return
        for name in set(unresolved):
            # EXACT filename match only. A [[Foo]] must open the note literally
            # named Foo. No semantic fallback — guessing the embedding-nearest
            # note is exactly what made wrong notes open (e.g. a non-existent
            # [[Urlaubslog]] resolving to an unrelated note). If there's no exact
            # match, leave the [[name]] as plain text (no link).
            try:
                exact = await vault_chroma.get(
                    where={"$and": [
                        {"user": {"$eq": username}},
                        {"note_name": {"$eq": name}},
                    ]}
                )
                if exact:
                    psid = (exact[0].get("metadata") or {}).get("pbrain_id", "")
                    if psid:
                        index[name.lower()] = psid
            except Exception:
                pass
    except Exception:
        pass



_TABS_H = "48px"

# ── Tool groups (display name, icon, [tool names in TOOL_DEFINITIONS]) ────────
_TOOL_GROUPS: list[tuple[str, str, list[str]]] = [
    (
        "documents",
        "description",
        [
            "search",
            "search_exact",
            "get_document_details",
            "get_document_table",
            "get_document_page_text",
            "get_actions",
            "download_document",
        ],
    ),
    ("email", "email", ["search_emails"]),
    ("calendar", "calendar_month", ["search_calendar"]),
    ("web", "public", ["web_search", "web_fetch_page"]),
    ("calculate", "calculate", ["calculate"]),
    ("visual", "visibility", ["view_document_page"]),
    ("memory", "psychology", ["remember_fact", "create_deadline", "search_memory", "update_brain_fact", "delete_brain_fact"]),
    ("vault", "book", ["vault_search"]),
    ("deep_research", "build", ["create_kanban_task"]),
    ("create", "create", ["trigger_docx_generation", "create_email", "generate_chat_pdf"]),
    # Separate from "create": those three only open a dialog with a draft, this
    # one writes into Paperless straight away — worth its own switch.
    ("document_notes", "sticky_note_2", ["create_note"]),
]
_TOOL_ALWAYS_ON: set[str] = {
    "get_current_date",
}
_TOOL_GROUP_DEFAULTS: dict[str, bool] = {
    name: (name != "visual") for name, _, _ in _TOOL_GROUPS
}

# Stored tool_prefs used the old German group names — map them once on load.
_LEGACY_GROUP_KEYS: dict[str, str] = {
    "Dokumente": "documents", "E-Mail": "email", "Kalender": "calendar",
    "Web": "web", "Rechnen": "calculate", "Visuell": "visual",
    "Gedächtnis": "memory", "Vault": "vault",
    "Tiefenrecherche": "deep_research", "Erstellen": "create",
}

# Guard: every tool MUST belong to a group (or be always-on), otherwise it is
# silently dropped by _active_tools() and the model can never call it — exactly
# the bug create_deadline hit. Fail loudly at import instead.
_GROUPED_TOOLS: set[str] = _TOOL_ALWAYS_ON | {
    tool for _, _, tool_names in _TOOL_GROUPS for tool in tool_names
}
_UNGROUPED_TOOLS: set[str] = {t["name"] for t in TOOL_DEFINITIONS} - _GROUPED_TOOLS
assert not _UNGROUPED_TOOLS, (
    f"Tools nicht in _TOOL_GROUPS/_TOOL_ALWAYS_ON eingetragen (würden stumm "
    f"herausgefiltert): {sorted(_UNGROUPED_TOOLS)}"
)

# Quick-chip labels + prompts are defined (translated) at render time inside
# chat() as _QUICK_CHIPS_LOCAL.


def _activity_hint(_tool_name: str, tool_input: dict) -> str:
    """Extract a short readable hint from tool input for the activity bar."""
    _ = get_translator()
    q = tool_input.get("query") or tool_input.get("text") or ""
    doc_id = tool_input.get("document_id") or tool_input.get("source_doc_id")
    if q:
        short = q[:60].rstrip()
        return f": {short}{'…' if len(q) > 60 else ''}"
    if doc_id:
        return _(": Doc #{id}").format(id=doc_id)
    return ""


_CHAT_CSS = """<style>
html, body { overflow: hidden !important; }
.q-page { min-height: 0 !important; overflow: hidden !important; }
.chat-main-col {
    position: fixed !important;
    top: var(--q-header-height, 52px) !important;
    left: 0 !important; right: 0 !important; bottom: 0 !important;
}
.chat-splitter > .q-splitter__panel {
    position: relative !important; overflow: hidden !important;
}
.chat-splitter > .q-splitter__panel:first-child {
    border-right: 1px solid var(--c-border) !important;
}
.dl-table th {
    background: var(--c-surface) !important; color: var(--c-text-muted) !important;
    font-size: 0.75rem !important; padding: 6px 10px !important;
}
.dl-table td {
    color: var(--c-text-2) !important; font-size: 0.75rem !important;
    padding: 4px 10px !important; white-space: normal !important;
    word-break: break-word; vertical-align: top;
}
.dl-table .q-table { background: transparent !important; }
.dl-table .q-table__container { border: 1px solid var(--c-border); border-radius: 4px; }
.dl-splitter > .q-splitter__panel { position: relative !important; overflow: hidden !important; }
.dl-splitter > .q-splitter__panel:first-child { border-right: 1px solid var(--c-border) !important; }
.chat-md { color: var(--c-text-2); font-size: 0.875rem; line-height: 1.6; }
.chat-md p { margin: 0 0 0.4rem 0; }
.chat-md p:last-child { margin-bottom: 0; }
.chat-md ul, .chat-md ol { padding-left: 1.4rem; margin: 0 0 0.4rem 0; }
.chat-md li { margin-bottom: 0.15rem; }
.chat-md strong { color: var(--c-text); }
.chat-md em { color: var(--c-text-2); }
.chat-md code { background: var(--c-border); border-radius: 3px; padding: 0.1rem 0.3rem; font-family: monospace; font-size: 0.8em; color: #a5b4fc; }
.chat-md pre { background: var(--c-bg); border: 1px solid var(--c-border); border-radius: 6px; padding: 0.75rem; overflow-x: auto; margin: 0.4rem 0; }
.chat-md pre code { background: transparent; padding: 0; color: var(--c-text-2); }
.chat-md h1, .chat-md h2, .chat-md h3 { color: var(--c-text); margin: 0.5rem 0 0.2rem 0; font-weight: 600; }
.chat-md h1 { font-size: 1rem; }
.chat-md h2 { font-size: 0.95rem; }
.chat-md h3 { font-size: 0.875rem; }
.chat-md blockquote { border-left: 3px solid #6d28d9; padding-left: 0.75rem; color: var(--c-text-muted); margin: 0.4rem 0; }
.chat-md table { border-collapse: collapse; width: 100%; margin: 0.4rem 0; font-size: 0.8em; }
.chat-md th { background: var(--c-border); color: var(--c-text-muted); padding: 4px 8px; text-align: left; }
.chat-md td { border-top: 1px solid var(--c-border); padding: 4px 8px; color: var(--c-text-2); }
/* Chat history left drawer — always available (desktop + mobile) */
.chat-history-drawer {
    --hist-w: 252px;
    position: fixed !important;
    top: var(--q-header-height, 52px);
    left: calc(-1 * var(--hist-w) - 4px); bottom: 0;
    width: var(--hist-w) !important;
    background: var(--c-bg-deep); border-right: 2px solid #6d28d9;
    box-shadow: 4px 0 24px rgba(0,0,0,.75);
    z-index: 501; transition: left .3s cubic-bezier(.4,0,.2,1);
    display: flex; flex-direction: column; overflow: hidden;
}
.chat-history-drawer.open { left: 0 !important; }
.chat-hist-resize {
    position: absolute; right: 0; top: 0; bottom: 0;
    width: 5px; cursor: col-resize; z-index: 2;
    background: transparent; transition: background .15s;
}
.chat-hist-resize:hover, .chat-hist-resize.dragging { background: rgba(109,40,217,0.45); }
.chat-history-scrim {
    display: none; position: fixed;
    top: var(--q-header-height, 52px); left: 0; right: 0; bottom: 0;
    z-index: 500; background: rgba(0,0,0,.5);
}
.chat-history-scrim.open { display: block; }
.chat-history-item {
    display: flex; align-items: center; gap: 2px;
    padding: 5px 6px 5px 10px; border-radius: 6px;
    border-left: 2px solid transparent;
    transition: background .15s; min-width: 0;
}
.chat-history-item:hover { background: #1a2235; }
.chat-history-item:hover .chat-hist-actions { opacity: 1; }
.chat-history-item.active { background: var(--c-surface-3); border-left-color: #6d28d9; }
.chat-hist-actions { opacity: 0; transition: opacity .15s; display: flex; gap: 0; flex-shrink: 0; }
@media (max-width: 767px) {
    .chat-hist-actions { opacity: 1 !important; }
}
/* Mobile: docs panel slides in from right as fixed overlay */
@media (max-width: 767px) {
    .chat-splitter > .q-splitter__panel.q-splitter__before { width: 100% !important; }
    .chat-splitter .q-splitter__separator-area { display: none !important; }
    .chat-docs-mobile {
        position: fixed !important;
        top: var(--q-header-height, 52px); right: -100%; bottom: 0;
        width: 94% !important; max-width: 480px !important;
        background: var(--c-bg); border-left: 2px solid #6d28d9;
        box-shadow: -4px 0 24px rgba(0,0,0,.7);
        z-index: 500; transition: right .3s cubic-bezier(.4,0,.2,1); overflow: hidden;
    }
    .chat-docs-mobile.open { right: 0 !important; }
    .chat-docs-scrim {
        display: none; position: fixed;
        top: var(--q-header-height, 52px); left: 0; right: 0; bottom: 0;
        z-index: 499; background: rgba(0,0,0,.5);
    }
    .chat-docs-scrim.open { display: block; }
    /* Tighter spacing in the narrow drawer so cards don't force overflow */
    .chat-docs-mobile .doc-cards-row { gap: 8px !important; padding: 8px !important; }
    .chat-docs-mobile .chat-results-exp .q-expansion-item__container > .q-item { padding: 0 6px; }
}
@media (min-width: 768px) {
    .chat-docs-mobile { display: none !important; }
    .chat-docs-scrim { display: none !important; }
}
@media (max-width: 767px) {
    .chat-temp-input { display: none !important; }
}
@media (max-width: 767px) and (orientation: portrait) {
    .chat-temp-input { display: flex !important; }
    .chat-backend-sel { min-width: 5.5rem !important; max-width: 7rem !important; }
    .chat-backend-sel .q-field__native { font-size: 0.7rem !important; }
    .chat-backend-sel .q-field__control { padding-left: 6px !important; padding-right: 2px !important; }
    .chat-model-sel { min-width: 5rem !important; max-width: 7rem !important; }
    .chat-model-sel .q-field__native { font-size: 0.7rem !important; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .chat-model-sel .q-field__control { padding-left: 6px !important; padding-right: 2px !important; }
}
/* Quick chips horizontal scroll, no scrollbar */
.chat-quick-strip { overflow-x: auto; overflow-y: hidden; scrollbar-width: none; }
.chat-quick-strip::-webkit-scrollbar { display: none; }
/* Prominent chat input */
.chat-main-input .q-field__control {
    border-color: var(--c-border-strong) !important;
    border-radius: 12px !important;
    background: var(--c-surface) !important;
}
.chat-main-input .q-field__control:hover {
    border-color: #7c3aed !important;
}
.chat-main-input.q-field--focused .q-field__control {
    border-color: #7c3aed !important;
    box-shadow: 0 0 0 3px rgba(124,58,237,0.18) !important;
}
.chat-main-input .q-field__native { color: var(--c-text-2) !important; }
/* Mic button active state */
.chat-mic-btn.mic-active { background: #450a0a !important; }
.chat-mic-btn.mic-active .q-icon, .chat-mic-btn.mic-active i { color: #f87171 !important; }
/* Tool panel checkbox label size */
.chat-tool-cb .q-checkbox__label { font-size: 0.72rem !important; color: var(--c-text-muted) !important; }
/* Right-edge drawer handle (slider flag, mid-right screen edge) */
.chat-edge-handle {
    position: fixed; right: 0; top: 50%; transform: translateY(-50%);
    width: 22px; height: 96px; z-index: 498;
    background: var(--c-surface);
    border: 1px solid var(--c-border); border-right: none;
    border-radius: 10px 0 0 10px;
    display: flex; align-items: center; justify-content: center;
    cursor: pointer; user-select: none;
    transition: right .3s cubic-bezier(.4,0,.2,1), background .15s;
    box-shadow: -2px 0 10px rgba(0,0,0,0.35);
}
/* Keyboard shrinks the mobile viewport: a handle at 50% lands exactly on the
   send button. Park it at 25% on mobile instead. Must come AFTER the base
   rule above — equal specificity, source order decides. */
@media (max-width: 767px) {
    .chat-edge-handle { top: 25%; }
}
.chat-edge-handle:hover { background: var(--c-surface-2); }
.chat-edge-handle .q-icon { color: var(--c-text-muted); font-size: 18px; }
.chat-edge-handle.has-results {
    background: rgba(124,58,237,0.16);
    border-color: rgba(124,58,237,0.55);
}
.chat-edge-handle.has-results .q-icon { color: #c4b5fd; }
.chat-edge-badge {
    position: absolute; top: -7px; left: -9px; z-index: 2;
    background: #7c3aed; color: #fff;
    font-size: 0.6rem; font-weight: 700; line-height: 1;
    border-radius: 999px; padding: 2px 5px; min-width: 16px;
    text-align: center; pointer-events: none;
    box-shadow: 0 1px 4px rgba(0,0,0,0.5);
}
@keyframes results-pulse {
    0% { box-shadow: 0 0 0 0 rgba(124,58,237,0.7); }
    100% { box-shadow: 0 0 0 12px rgba(124,58,237,0); }
}
.chat-edge-handle.pulse { animation: results-pulse 0.9s ease-out 2; }
/* Quasar gives q-scrollarea__content min-width:100% + absolute positioning,
   so it grows to max-content (two 208px cards side by side) and overflows the
   narrow mobile drawer, clipping expansions/cards on the right. Cap it. */
.chat-results-scroll .q-scrollarea__content { max-width: 100%; }
/* Results panel accordion sections */
.chat-results-exp {
    border: 1px solid var(--c-border); border-radius: 8px;
    background: var(--c-surface); overflow: hidden;
}
.chat-results-exp .q-expansion-item__container > .q-item {
    min-height: 36px; padding: 0 10px;
}
.chat-results-exp .q-expansion-item__content { background: var(--c-bg); }
.tab-count {
    font-size: 0.6rem; background: var(--c-border); color: var(--c-text-muted);
    border-radius: 999px; padding: 1px 6px; line-height: 1.2; min-width: 15px;
    text-align: center;
}
.chat-results-exp.has-hits .tab-count { background: #4c1d95; color: #ddd6fe; }
/* Web result cards (RSS-feed style) */
.web-card { transition: transform .12s, box-shadow .12s; }
.web-card:hover { transform: translateY(-1px); box-shadow: 0 4px 18px rgba(0,0,0,0.45) !important; }
.web-card-link { text-decoration: none !important; display: block; width: 100%; }
/* Typing indicator */
.typing-dots { display:inline-flex; align-items:center; gap:4px; padding:2px 0; }
.typing-dots span {
    display:inline-block; width:7px; height:7px; border-radius:50%;
    background:#a78bfa; opacity:0.3;
    animation: typing-bounce 1.2s ease-in-out infinite;
}
.typing-dots span:nth-child(2) { animation-delay:0.2s; }
.typing-dots span:nth-child(3) { animation-delay:0.4s; }
@keyframes typing-bounce {
    0%, 60%, 100% { transform: translateY(0); opacity:0.3; }
    30% { transform: translateY(-5px); opacity:1; }
}
</style>"""


@ui.page("/chat")
async def chat():
    if not require_auth():
        return
    page_layout()
    _ = get_translator()
    ui.add_head_html(_CHAT_CSS)

    # Render-time translations for import-time constants. Keys stay German
    # (stable state keys in tool_prefs); only display labels are translated.
    _TOOL_GROUP_LABELS = {
        "documents": _("Documents"),
        "email": _("Email"),
        "calendar": _("Calendar"),
        "web": _("Web"),
        "calculate": _("Calc"),
        "visual": _("Visual"),
        "memory": _("Memory"),
        "vault": _("Vault"),
        "deep_research": _("Deep research"),
        "create": _("Create"),
        # "Document notes", not "Notes": the Vault group already covers the
        # user's own notes, these are notes on Paperless documents.
        "document_notes": _("Document notes"),
    }
    _QUICK_CHIPS_LOCAL = [
        (_("✨ What can you do?"), _("Show me what you can do! List all your capabilities and tools.")),
        (_("📌 Open deadlines"), _("What open deadlines do I have?")),
        (_("📅 Next month"), _("What's in my calendar next month?")),
        (_("📄 New documents"), _("Show me my latest documents from the last 30 days.")),
        (_("📧 New emails"), _("What are my latest emails?")),
        (_("💰 Invoices"), _("Are there any open invoices or outstanding payments?")),
        (_("✉️ Write a letter"), _("Write a letter.")),
        (_("📧 Write an email"), _("Write an email.")),
        (_("💾 Save chat to Paperless"), _("Create a document from the current chat and save it to Paperless.")),
    ]

    open_document, _doc_dlg = create_document_dialog(
        open_cluster_fn=lambda doc_id: open_cluster(doc_id),
        pin_fn=lambda r: _on_pin(r),
        get_pin_state_fn=lambda doc_id: doc_id in _pinned_ids(),
    )

    # ── Vault note dialog ─────────────────────────────────────────────────────
    with ui.dialog() as _vault_dlg:
        with ui.card().style(
            "background:var(--c-bg); width:min(720px,98vw); max-height:90vh;"
            "overflow-y:auto; border:1px solid var(--c-border);"
        ):
            with ui.row().classes("items-center justify-between mb-2 w-full"):
                _vault_dlg_title = ui.label(_("Note")).classes(
                    "text-base font-semibold text-blue-300"
                )
                ui.button(icon="close", on_click=_vault_dlg.close).props(
                    "flat dark dense"
                ).classes("text-gray-400")
            _vault_dlg_path = ui.label("").classes("text-xs text-gray-400 font-mono mb-1")
            ui.separator()
            _vault_dlg_body = ui.markdown("").classes("chat-md mt-3")

    async def open_vault_note(pbrain_id: str) -> None:
        from services.clients import brain, vault_chroma
        from vault.frontmatter import read as fm_read
        from vault.paths import vault_path
        username = ng_app.storage.user.get("paperless_user", "")
        try:
            # Try vault collection first, then brain (fact) collection
            items = await vault_chroma.get(where={"pbrain_id": {"$eq": pbrain_id}})
            is_brain = False
            if not items:
                items = await brain._chroma.get(ids=[pbrain_id])
                is_brain = bool(items)
            if not items:
                ui.notify(_("Note not found."), type="warning")
                return
            entry = items[0]
            meta_stored = entry.get("metadata") or {}
            path_str = meta_stored.get("path", "")

            if path_str and username:
                # File-backed: read from disk
                abs_path = vault_path(username) / path_str
                fm_meta, body = fm_read(abs_path)
                name = path_str.rsplit("/", 1)[-1].removesuffix(".md")
                _vault_dlg_title.set_text(fm_meta.get("title") or name)
                _vault_dlg_path.set_text(path_str)
                _vault_dlg_body.set_content(body or _("_Empty_"))
            else:
                # Chroma-only fact (no file yet): show document text directly
                doc_text = entry.get("document") or _("_No content_")
                import json as _json
                tags = _json.loads(meta_stored.get("tags", "[]") or "[]")
                conf = meta_stored.get("confidence", 1.0)
                header = _("**Memory fact** (ID: `{id}…`)").format(id=pbrain_id[:8])
                if tags:
                    header += "\n\n" + _("Tags: {tags}").format(tags=', '.join(tags))
                if float(conf) < 1.0:
                    header += " · " + _("Confidence: {conf}").format(conf=f"{float(conf):.0%}")
                _vault_dlg_title.set_text(_("🧠 Memory fact"))
                _vault_dlg_path.set_text(f"brain:{pbrain_id[:8]}")
                _vault_dlg_body.set_content(f"{header}\n\n---\n\n{doc_text}")
            _vault_dlg.open()
        except Exception as e:
            ui.notify(_("Error: {err}").format(err=e), type="negative")

    def _render_vault_card(note: VaultNoteResult) -> None:
        _card = ui.card().classes("w-52 hover:shadow-xl bg-gray-800 gap-0 doc-card").style(
            "position:relative; overflow:hidden; cursor:pointer;"
        )
        _card.on("click", lambda n=note: asyncio.ensure_future(open_vault_note(n.pbrain_id)))
        with _card:
            with ui.element("div").classes("w-full rounded-t").style(
                "height:9rem; background:#0d2137; position:relative;"
                "display:flex; align-items:center; justify-content:center;"
            ):
                ui.icon("article", size="xl").classes("text-blue-400 opacity-60")
                ui.label("MD").style(
                    "position:absolute; top:8px; left:8px; font-size:0.6rem;"
                    "background:#1e3a5f; color:#93c5fd; border-radius:3px; padding:1px 5px;"
                    "font-family:monospace; letter-spacing:1px;"
                )
            with ui.column().classes("p-2 gap-1 w-full"):
                ui.label(note.title or note.pbrain_id[:8]).classes(
                    "font-semibold text-xs text-blue-200 leading-tight"
                ).style(
                    "display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;"
                    "overflow:hidden;word-break:break-all;"
                )
                ui.separator().classes("my-1")
                if note.heading_path:
                    with ui.row().classes("items-center gap-1"):
                        ui.icon("tag", size="xs").classes("text-gray-400")
                        ui.label(note.heading_path).classes("text-xs text-gray-400 truncate max-w-36")
                if note.distance < 1.0:
                    ui.label(_("Dist: {dist}").format(dist=f"{note.distance:.2f}")).classes("text-xs text-blue-600")
                if note.snippet:
                    ui.label(note.snippet[:120]).classes(
                        "text-xs text-gray-400 leading-tight"
                    ).style(
                        "display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;"
                    )
                with ui.row().classes("items-center justify-between mt-1 w-full"):
                    ui.badge("vault", color="blue").classes("text-xs")
                    ui.icon("open_in_new", size="xs").classes("text-gray-600")

    def _render_brain_card(fact: BrainFactResult) -> None:
        _card = ui.card().classes("w-52 hover:shadow-xl bg-gray-800 gap-0 doc-card").style(
            "position:relative; overflow:hidden; cursor:pointer;"
        )
        _card.on("click", lambda f=fact: asyncio.ensure_future(open_vault_note(f.pbrain_id)))
        with _card:
            with ui.element("div").classes("w-full rounded-t").style(
                "height:9rem; background:#1a0a2e; position:relative;"
                "display:flex; align-items:center; justify-content:center;"
            ):
                ui.icon("psychology", size="xl").classes("text-purple-400 opacity-70")
                ui.label("🧠").style(
                    "position:absolute; top:8px; left:8px; font-size:0.85rem;"
                )
                if fact.confidence < 1.0:
                    ui.label(f"{fact.confidence:.0%}").style(
                        "position:absolute; top:8px; right:8px; font-size:0.6rem;"
                        "background:#2d1b5e; color:#c4b5fd; border-radius:3px; padding:1px 4px;"
                        "font-family:monospace;"
                    )
            with ui.column().classes("p-2 gap-1 w-full"):
                ui.label(fact.text).classes(
                    "text-xs text-purple-100 leading-tight"
                ).style(
                    "display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;"
                    "overflow:hidden;"
                )
                ui.separator().classes("my-1")
                if fact.tags:
                    with ui.row().classes("flex-wrap gap-1"):
                        for t in fact.tags[:3]:
                            ui.badge(t, color="purple").classes("text-xs")
                if fact.distance < 1.0:
                    ui.label(_("Dist: {dist}").format(dist=f"{fact.distance:.2f}")).classes("text-xs text-purple-700")
                with ui.row().classes("items-center justify-between mt-1 w-full"):
                    ui.badge("memory", color="purple").classes("text-xs")
                    ui.icon("open_in_new", size="xs").classes("text-gray-600")

    def _render_web_card(r) -> None:
        """RSS-feed style card for a web search hit. Click opens the article."""
        img = r.img_src or ""
        if img.startswith("//"):
            img = "https:" + img
        try:
            domain = urllib.parse.urlparse(r.url).netloc.removeprefix("www.")
        except Exception:
            domain = r.url
        with ui.link(target=r.url, new_tab=True).classes("web-card-link"):
            with ui.card().classes(
                "w-full bg-gray-800 gap-0 web-card"
            ).style(
                "cursor:pointer; overflow:hidden; padding:0;"
                "border:1px solid var(--c-border);"
            ):
                with ui.row().classes("w-full gap-0 no-wrap items-stretch"):
                    if img:
                        ui.html(
                            f'<img src="{_html.escape(img, quote=True)}" '
                            f'style="width:96px; min-height:96px; height:100%;'
                            f'object-fit:cover; flex-shrink:0; display:block;" '
                            f'loading="lazy" '
                            f"onerror=\"this.style.display='none'\">",
                            sanitize=False,
                        ).style("flex-shrink:0; align-self:stretch;")
                    else:
                        with ui.element("div").style(
                            "width:96px; min-height:96px; flex-shrink:0;"
                            "background:#062a22; display:flex;"
                            "align-items:center; justify-content:center;"
                        ):
                            ui.icon("public", size="lg").classes(
                                "text-teal-400 opacity-60"
                            )
                    with ui.column().classes("gap-1").style(
                        "flex:1; min-width:0; padding:10px 12px;"
                    ):
                        ui.label(r.title or r.url).classes(
                            "text-xs font-semibold text-teal-100 leading-tight"
                        ).style(
                            "display:-webkit-box; -webkit-line-clamp:2;"
                            "-webkit-box-orient:vertical; overflow:hidden;"
                            "word-break:break-word;"
                        )
                        with ui.row().classes("items-center gap-2 no-wrap").style(
                            "min-width:0;"
                        ):
                            ui.icon("language", size="xs").classes("text-teal-600")
                            ui.label(domain).classes(
                                "text-xs text-teal-500 truncate"
                            ).style("font-family:monospace; font-size:0.65rem;")
                            if r.published:
                                ui.label(r.published).classes(
                                    "text-xs text-gray-500"
                                ).style("font-size:0.65rem; flex-shrink:0;")
                        if r.snippet:
                            ui.label(r.snippet).classes(
                                "text-xs text-gray-400 leading-tight"
                            ).style(
                                "display:-webkit-box; -webkit-line-clamp:3;"
                                "-webkit-box-orient:vertical; overflow:hidden;"
                            )

    ui.add_head_html("""<script>
(function fixViewport() {
    var m = document.querySelector('meta[name="viewport"]');
    var c = 'width=device-width, initial-scale=1.0, user-scalable=yes';
    if (m) { m.content = c; } else {
        m = document.createElement('meta'); m.name = 'viewport'; m.content = c;
        document.head.appendChild(m);
    }
})();
</script>""")

    # ── User credentials (LLM settings override .env defaults) ───────────────
    _username = ng_app.storage.user.get("paperless_user", "")
    _token = get_session_token()
    _creds = load_credentials(_username, _token) if _username and _token else {}
    _llm = _creds.get("llm", {})
    _api_key = _llm.get("anthropic_api_key") or settings.anthropic_api_key

    # ── Persistent chat settings (credential store, cross-device) ────────────
    _chat_cfg: dict = _creds.get("chat_settings", {})

    def _save_chat_settings() -> None:
        if not _username or not _token:
            return
        fresh = load_credentials(_username, _token)
        fresh["chat_settings"] = _chat_cfg
        save_credentials(_username, _token, fresh)

    # ── Session state ─────────────────────────────────────────────────────────
    _saved_tool_prefs = {
        _LEGACY_GROUP_KEYS.get(k, k): v
        for k, v in (_chat_cfg.get("tool_prefs") or {}).items()
    }
    _initial_tool_prefs = {
        name: _saved_tool_prefs.get(name, default)
        for name, default in _TOOL_GROUP_DEFAULTS.items()
    }

    # Pinned docs: list of {"id": int, "title": str} — persisted in storage
    _pinned_cache: list[dict] = list(ng_app.storage.user.get("pinned_docs_cache") or [])

    # ── Model registry ────────────────────────────────────────────────────────
    from services.model_registry import get_models as _get_reg_models
    _reg_models = [m for m in _get_reg_models(_username, _token) if m.get("enabled", True)]
    _saved_name = ng_app.storage.user.get("chat_model_name", "")
    _initial_model_name = (
        _saved_name if any(m["name"] == _saved_name for m in _reg_models)
        else (_reg_models[0]["name"] if _reg_models else "")
    )
    _sel_model = next((m for m in _reg_models if m["name"] == _initial_model_name), None)

    _reg_backends: dict = {}
    for _rm in _reg_models:
        if _rm.get("backend") == "anthropic":
            _reg_backends[_rm["name"]] = ClaudeChatBackend(
                api_key=_rm.get("api_key") or _api_key,
                model=_rm["model"],
                base_url=_rm.get("base_url", ""),
            )
        else:
            _think_cfg = _rm.get("think")  # None=auto, True/False=explicit
            _reg_backends[_rm["name"]] = OpenAICompatibleChatBackend(
                base_url=_rm.get("base_url", ""),
                api_key=_rm.get("api_key", ""),
                model=_rm["model"],
                max_output_tokens=int(_rm.get("max_output_tokens") or 0) or None,
                think=bool(_think_cfg) if _think_cfg is not None else None,
            )

    _initial_temperature = float(
        (_sel_model.get("temperature") if _sel_model else None)
        or _llm.get("temperature")
        or 0.3
    )

    _s: dict = {
        "messages": [],
        "running": False,
        "stop_requested": False,
        "chat_model_name": _initial_model_name,
        "temperature": _initial_temperature,
        "total_tokens": 0,
        "context_window": 0,
        "current_ctx_tokens": 0,
        "conv_id": None,
        "history_visible": False,
        "docs_visible": False,
        "pinned_docs": _pinned_cache,
        "tool_prefs": _initial_tool_prefs,
        "tools_panel_open": False,
        "max_iterations": int(_chat_cfg.get("max_iterations", 16)),
        "web_fetch_mode": _chat_cfg.get("web_fetch_mode", "hybrid"),
        "show_thinking": _chat_cfg.get("show_thinking", True),
    }
    _rs: dict = {
        "last_results": [], "vault_results": [], "brain_results": [],
        "web_results": [], "vault_index": {}, "right_panel": None,
        "section_order": ["docs", "brain", "vault", "web"],
        "expanded": {"docs"},
    }
    # vault_index: {lowercase_title: pbrain_id} for auto-linking in LLM responses
    _tool_cbs: list[tuple] = []  # (ui.checkbox, group_name)

    # ── Helpers that don't depend on layout elements ───────────────────────────

    def _placeholder() -> None:
        with ui.column().style(
            "position:absolute; top:0; left:0; right:0; bottom:0;"
            "display:flex; align-items:center; justify-content:center;"
        ):
            ui.icon("manage_search", size="xl").classes("text-gray-600")
            ui.label(_("Search results appear here")).classes(
                "text-gray-500 text-sm mt-2"
            )

    def _welcome_msg() -> None:
        with ui.row().classes("justify-start w-full"):
            ui.html(
                '<div style="background:var(--c-surface);border-radius:0 12px 12px 12px;'
                'padding:10px 14px;max-width:85%;font-size:0.875rem;color:var(--c-text-2);">'
                + _(
                    "Hello! I am your document assistant. I can search and read documents and answer general questions. What would you like to know?"
                )
                + "</div>",
                sanitize=False,
            )

    def _render_stored_msg(msg: dict) -> None:
        """Render a single stored message dict into messages_container."""
        if msg["role"] == "user":
            with ui.row().classes("justify-end w-full"):
                ui.html(
                    f'<div style="background:#6d28d9;border-radius:12px 0 12px 12px;'
                    f"padding:10px 14px;font-size:0.875rem;color:#f8fafc;"
                    f'word-break:break-word;line-height:1.5;">'
                    f"{_html.escape(msg['content'])}"
                    f"</div>",
                    sanitize=False,
                ).style("max-width:85%;")
        else:
            with ui.row().classes("justify-start w-full"):
                with ui.element("div").style(
                    "background:var(--c-surface);border-radius:0 12px 12px 12px;"
                    "padding:10px 14px;max-width:85%;font-size:0.875rem;color:var(--c-text-2);"
                ):
                    ui.markdown(
                        _inject_doc_links(msg["content"]), sanitize=False, extras=_MD_EXTRAS
                    ).classes("chat-md")

    def _active_groups() -> set[str]:
        return {
            name
            for name, _, _ in _TOOL_GROUPS
            if _s["tool_prefs"].get(name, _TOOL_GROUP_DEFAULTS.get(name, True))
        }

    def _active_tools() -> list[dict]:
        enabled: set[str] = _TOOL_ALWAYS_ON.copy()
        for name, _, tool_names in _TOOL_GROUPS:
            if _s["tool_prefs"].get(name, _TOOL_GROUP_DEFAULTS.get(name, True)):
                enabled.update(tool_names)
        return [t for t in TOOL_DEFINITIONS if t["name"] in enabled]

    def _on_tool_pref_change(group: str, enabled: bool) -> None:
        _s["tool_prefs"][group] = enabled
        _chat_cfg["tool_prefs"] = dict(_s["tool_prefs"])
        _save_chat_settings()

    # ── History drawer helpers ────────────────────────────────────────────────

    def _est_tokens() -> int:
        """Rough token estimate from current messages (1 token ≈ 4 chars)."""
        return max(0, sum(len(str(m.get("content", ""))) for m in _s["messages"]) // 4)

    def _get_model_ctx_win() -> int:
        """Context window from model config — used before first API response."""
        name = _s.get("chat_model_name", "")
        rm = next((m for m in _reg_models if m["name"] == name), None)
        if rm and rm.get("backend") == "anthropic":
            from services.chat_service import _claude_ctx_window
            return _claude_ctx_window(rm.get("model", ""))
        return 0

    def _chat_uses_local_lane() -> bool:
        """True if the selected chat model runs on the local Ollama lane.

        Sync/ingest occupies the local GPU, so only local-lane chat models are
        blocked while a sync runs; API-lane models keep working. Unknown models
        count as local (conservative: keep the old blocking behavior).
        """
        name = _s.get("chat_model_name", "")
        rm = next((m for m in _reg_models if m["name"] == name), None)
        return rm.get("lane", "api") == "local" if rm else True

    def _fmt_conv_time(updated_at: str) -> str:
        """Format ISO timestamp as short relative string."""
        from datetime import datetime, timedelta
        try:
            dt = datetime.fromisoformat(updated_at).astimezone()
            now = datetime.now(dt.tzinfo)
            delta = now - dt
            if delta < timedelta(minutes=2):
                return _("just now")
            if delta < timedelta(hours=1):
                return _("{n}m ago").format(n=int(delta.total_seconds() / 60))
            if delta < timedelta(days=1):
                return dt.strftime("%H:%M")
            if delta < timedelta(days=7):
                return ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][dt.weekday()]
            return dt.strftime("%d.%m.")
        except Exception:
            return ""

    def _set_history_visible(visible: bool) -> None:
        _s["history_visible"] = visible
        if visible:
            history_drawer.classes(add="open")
            history_scrim.classes(add="open")
            _refresh_history_list()
        else:
            history_drawer.classes(remove="open")
            history_scrim.classes(remove="open")
        history_toggle_btn.classes(
            remove="text-gray-400 text-purple-400",
            add="text-purple-400" if visible else "text-gray-400",
        )

    def _refresh_history_list() -> None:
        history_list_container.clear()
        convs = _chat_hist.list_conversations(_username) if _username else []
        with history_list_container:
            # Always show a placeholder for the current (possibly unsaved) session
            if not _s.get("conv_id"):
                with ui.element("div").classes("chat-history-item active"):
                    with ui.column().style(
                        "min-width:0; flex:1; overflow:hidden; gap:1px;"
                    ):
                        ui.label(_("Current chat")).classes("text-xs text-gray-300").style(
                            "overflow:hidden; text-overflow:ellipsis;"
                            "white-space:nowrap; line-height:1.4;"
                        )
                        ui.label("–").style(
                            "font-size:0.6rem; color:var(--c-border-strong); line-height:1;"
                        )
            for c in convs:
                is_active = c["id"] == _s.get("conv_id")
                item_classes = "chat-history-item" + (" active" if is_active else "")
                with ui.element("div").classes(item_classes):
                    with ui.column().style(
                        "min-width:0; flex:1; overflow:hidden; gap:1px; cursor:pointer;"
                    ).on("click", lambda cid=c["id"]: _load_conv(cid)):
                        ui.label(c["title"]).classes("text-xs text-gray-300").style(
                            "overflow:hidden; text-overflow:ellipsis;"
                            "white-space:nowrap; line-height:1.4;"
                        )
                        ui.label(_fmt_conv_time(c["updated_at"])).style(
                            "font-size:0.6rem; color:var(--c-border-strong); line-height:1;"
                        )
                    with ui.row().classes("chat-hist-actions").style("gap:0; flex-shrink:0;"):
                        ui.button(
                            icon="drive_file_rename_outline",
                            on_click=lambda cid=c["id"], t=c["title"]: _rename_conv(cid, t),
                        ).props("flat dark dense").classes("text-gray-700").style(
                            "min-width:20px; width:20px; height:20px; font-size:13px;"
                        )
                        ui.button(
                            icon="delete_outline",
                            on_click=lambda cid=c["id"]: _delete_conv(cid),
                        ).props("flat dark dense").classes("text-red-900").style(
                            "min-width:20px; width:20px; height:20px; font-size:13px;"
                        )

    def _update_context_progress() -> None:
        # Use API-reported value; fall back to model config before first response
        cw = _s["context_window"] or _get_model_ctx_win()
        ct = _est_tokens()  # always estimated from actual message content
        if cw > 0:
            ratio = min(1.0, ct / cw)
            pct = int(ratio * 100)
            color = "red" if ratio >= 0.9 else "orange" if ratio >= 0.75 else "purple"
            ctx_progress.set_value(ratio)
            ctx_progress.props(f"color={color}")
            ctx_progress.style("opacity:1;")
            ctx_pct_label.set_text(f"{pct}%")
            ct_k = f"{ct/1000:.0f}k" if ct >= 1000 else str(ct)
            cw_k = f"{cw/1000:.0f}k" if cw >= 1000 else str(cw)
            ctx_tt.set_text(_("Context: {ct} / {cw}").format(ct=ct_k, cw=cw_k))
            # Highlight compact button when context is getting full
            compact_btn.classes(
                remove="text-gray-600 text-amber-500",
                add="text-amber-500" if ratio >= 0.75 else "text-gray-600",
            )
        else:
            ctx_progress.set_value(0.0)
            ctx_progress.style("opacity:0.35;")
            ctx_pct_label.set_text("")
            ctx_tt.set_text(_("Context: –"))
            compact_btn.classes(remove="text-amber-500", add="text-gray-600")

    def _load_conv(conv_id: str) -> None:
        messages = _chat_hist.load_messages(conv_id, _username)
        _s["messages"] = messages
        _s["conv_id"] = conv_id
        _s["total_tokens"] = 0
        _s["context_window"] = 0
        _s["current_ctx_tokens"] = 0
        ng_app.storage.user["chat_conv_id"] = conv_id
        ng_app.storage.user.pop("chat_history", None)
        ng_app.storage.user.pop("chat_token_count", None)
        ng_app.storage.user.pop("chat_context_window", None)
        messages_container.clear()
        with messages_container:
            if not messages:
                _welcome_msg()
            else:
                for _msg in messages:
                    _render_stored_msg(_msg)
        _update_context_progress()
        _set_history_visible(False)

    def _delete_conv(conv_id: str) -> None:
        _chat_hist.delete_conversation(conv_id, _username)
        if _s.get("conv_id") == conv_id:
            _s["conv_id"] = None
            _s["messages"].clear()
            _s["total_tokens"] = 0
            _s["context_window"] = 0
            _s["current_ctx_tokens"] = 0
            ng_app.storage.user.pop("chat_conv_id", None)
            messages_container.clear()
            with messages_container:
                _welcome_msg()
            _update_context_progress()
        _refresh_history_list()

    def _rename_conv(conv_id: str, current_title: str) -> None:
        with ui.dialog() as dlg, ui.card().style(
            "background:var(--c-surface); min-width:300px; padding:16px;"
        ):
            ui.label(_("Rename conversation")).classes("text-sm font-semibold text-gray-200 mb-2")
            name_input = ui.input(value=current_title).props("outlined dark dense").classes("w-full")
            with ui.row().classes("justify-end gap-2 mt-3"):
                ui.button(_("Cancel"), on_click=dlg.close).props("flat dark dense").classes("text-gray-400")
                def _do_rename() -> None:
                    new_title = name_input.value.strip()
                    if new_title:
                        _chat_hist.rename_conversation(conv_id, _username, new_title)
                        dlg.close()
                        _refresh_history_list()
                ui.button(_("Save"), on_click=_do_rename).props("unelevated dark dense").classes("bg-purple-700 text-white")
        dlg.open()

    def _new_conv() -> None:
        _s["messages"].clear()
        _s["total_tokens"] = 0
        _s["context_window"] = 0
        _s["current_ctx_tokens"] = 0
        _s["conv_id"] = None
        _s["tool_prefs"] = dict(_TOOL_GROUP_DEFAULTS)
        _s["max_iterations"] = 16
        ng_app.storage.user.pop("chat_history", None)
        ng_app.storage.user.pop("chat_token_count", None)
        ng_app.storage.user.pop("chat_context_window", None)
        ng_app.storage.user.pop("chat_conv_id", None)
        messages_container.clear()
        with messages_container:
            _welcome_msg()
        _rs["last_results"] = []
        _rs["vault_results"] = []
        _rs["brain_results"] = []
        _rs["web_results"] = []
        _rs["section_order"] = ["docs", "brain", "vault", "web"]
        _rs["expanded"] = {"docs"}
        _render_results()
        # Drawer open/closed state deliberately untouched — clearing the chat
        # must not collapse a panel the user opened (or reopen a closed one).
        _update_context_progress()
        for _cb, _grp in _tool_cbs:
            _cb.set_value(_TOOL_GROUP_DEFAULTS.get(_grp, True))
        _set_history_visible(False)

    async def _compact_chat() -> None:
        if not _s["messages"] or _s["running"]:
            ui.notify(_("No conversation to compress") if not _s["messages"] else _("Chat running"), type="warning")
            return

        backend = _reg_backends.get(_s["chat_model_name"])
        if not backend:
            ui.notify(_("No model selected"), type="warning")
            return

        _s["running"] = True
        _s["stop_requested"] = False
        send_btn.props(add="icon=stop color=red")

        history_text = "\n".join(
            f"{'Nutzer' if m['role'] == 'user' else 'Assistent'}: {m['content']}"
            for m in _s["messages"]
        )
        compact_request = (
            "Fasse das folgende Gespräch sehr kompakt zusammen. "
            "Behalte alle wichtigen Fakten, Dokument-IDs (#NNN), Daten und Ergebnisse. "
            "Antworte NUR mit der Zusammenfassung, ohne Präambel:\n\n" + history_text
        )

        # Clear old messages and show a compacting-in-progress bubble
        messages_container.clear()
        _s["messages"] = []
        _s["total_tokens"] = 0
        _s["context_window"] = 0
        _s["current_ctx_tokens"] = 0
        _update_context_progress()

        with messages_container:
            with ui.row().classes("justify-start w-full"):
                with ui.element("div").style(
                    "background:var(--c-surface-3); border-radius:0 12px 12px 12px;"
                    "padding:10px 14px; max-width:85%; font-size:0.875rem; color:var(--c-text-2);"
                    "border-left:3px solid #6d28d9; display:flex; flex-direction:column; gap:6px;"
                ):
                    with ui.row().classes("items-center gap-2"):
                        ui.spinner(size="xs").classes("text-purple-400")
                        ui.label(_("Compressing…")).classes("text-xs text-purple-300")
                    compact_md = ui.markdown(
                        "", sanitize=False, extras=_MD_EXTRAS
                    ).classes("chat-md")
        messages_scroll.scroll_to(percent=1.0)

        try:
            parts: list[str] = []
            async for ev in backend.run_turn(
                system="Du bist ein präziser Zusammenfasser. Antworte nur mit der kompakten Zusammenfassung.",
                messages=[{"role": "user", "content": compact_request}],
                tools=[],
                temperature=0.2,
                max_iterations=1,
            ):
                if _s["stop_requested"]:
                    break
                if isinstance(ev, TextTokenEvent):
                    parts.append(ev.text)
                    compact_md.set_content("".join(parts))

            summary = "".join(parts).strip()
            if summary:
                summary_content = _("**[Summary]**") + f"\n\n{summary}"
                _s["messages"] = [{"role": "assistant", "content": summary_content}]
                # Replace the spinner bubble with the final rendered result
                messages_container.clear()
                with messages_container:
                    with ui.row().classes("justify-start w-full"):
                        with ui.element("div").style(
                            "background:var(--c-surface-3); border-radius:0 12px 12px 12px;"
                            "padding:10px 14px; max-width:85%; font-size:0.875rem; color:var(--c-text-2);"
                            "border-left:3px solid #6d28d9;"
                        ):
                            ui.label(_("🗜 Compressed conversation")).classes(
                                "text-xs text-purple-400 font-semibold mb-2"
                            )
                            ui.markdown(
                                summary, sanitize=False, extras=_MD_EXTRAS
                            ).classes("chat-md")
                _update_context_progress()
                ng_app.storage.user["chat_history"] = list(_s["messages"])
                if _s.get("conv_id") and _username:
                    _chat_hist.replace_messages(_s["conv_id"], _username, _s["messages"])
            else:
                # Nothing generated — restore welcome
                messages_container.clear()
                with messages_container:
                    _welcome_msg()
                ui.notify(_("Compression produced no result"), type="warning")
        except Exception as e:
            messages_container.clear()
            with messages_container:
                _welcome_msg()
            ui.notify(_("Error compressing: {err}").format(err=e), type="negative")
        finally:
            _s["running"] = False
            _s["stop_requested"] = False
            send_btn.props(add="icon=send color=purple")
            messages_scroll.scroll_to(percent=1.0)

    # ── Chat history left drawer ──────────────────────────────────────────────
    history_scrim = ui.element("div").classes("chat-history-scrim")
    history_drawer = ui.element("div").classes("chat-history-drawer")
    with history_drawer:
        # Resize handle (right edge)
        ui.element("div").classes("chat-hist-resize")
        with ui.row().style(
            "flex-shrink:0; height:44px; background:var(--c-bg-deep);"
            "border-bottom:1px solid #1e2a3a;"
            "align-items:center; padding:0 8px 0 12px; gap:6px;"
        ):
            ui.icon("forum", size="xs").classes("text-purple-400")
            ui.label(_("Conversations")).classes("text-sm text-gray-300 font-semibold flex-1")
            ui.button(
                icon="close",
                on_click=lambda: _set_history_visible(False),
            ).props("flat dark dense").classes("text-gray-600")
        # "Neuer Chat" button
        with ui.element("div").style(
            "flex-shrink:0; padding:8px 10px 6px 10px;"
            "display:flex; justify-content:stretch;"
        ):
            ui.button(
                _("New chat"), icon="add",
                on_click=lambda: _new_conv(),
            ).props("unelevated dark no-caps").style(
                "flex:1; max-width:220px; height:30px; border-radius:7px;"
                "background:linear-gradient(135deg,#4c1d95 0%,#7c3aed 100%);"
                "color:#ede9fe; font-weight:600; font-size:0.78rem; letter-spacing:0.01em;"
                "box-shadow:0 2px 10px rgba(109,40,217,0.3);"
            )
        # Conversation list (scrollable)
        history_list_container = ui.element("div").style(
            "flex:1; overflow-y:auto; padding:4px 6px 8px 6px;"
            "display:flex; flex-direction:column; gap:1px;"
        )

    # ── Mobile docs drawer ────────────────────────────────────────────────────
    scrim = ui.element("div").classes("chat-docs-scrim")
    mobile_docs = ui.element("div").classes("chat-docs-mobile")
    with mobile_docs:
        with ui.row().style(
            "position:absolute; top:0; left:0; right:0; height:44px; z-index:1;"
            "background:var(--c-surface); border-bottom:1px solid var(--c-border);"
            "align-items:center; padding:0 12px; gap:8px; flex-shrink:0;"
        ):
            ui.icon("search", size="xs").classes("text-purple-400")
            ui.label(_("Search results")).classes(
                "text-sm text-gray-300 font-semibold flex-1"
            )
            ui.button(
                icon="close",
                on_click=lambda: _set_docs_visible(False),
            ).props("flat dark dense").classes("text-gray-400")
        mobile_docs_inner = ui.element("div").style(
            "position:absolute; top:44px; left:0; right:0; bottom:0; overflow:hidden;"
        )

    # ── Right-edge drawer handle (slider flag) ────────────────────────────────
    docs_edge_handle = ui.element("div").classes("chat-edge-handle")
    with docs_edge_handle:
        docs_handle_icon = ui.icon("chevron_left")
        docs_badge = ui.label("").classes("chat-edge-badge").style("display:none;")
        ui.tooltip(_("Show/hide search results"))
    docs_edge_handle.on("click", lambda: _set_docs_visible(not _s["docs_visible"]))

    # Keep the handle glued to the results panel's left edge — follows both
    # programmatic open/close AND manual splitter dragging (desktop only;
    # on mobile the panel is a fixed overlay and the handle stays at right:0).
    ui.add_head_html("""<script>
(function trackEdgeHandle() {
    function upd() {
        var handle = document.querySelector('.chat-edge-handle');
        var splitter = document.querySelector('.chat-splitter');
        var after = document.querySelector('.chat-splitter > .q-splitter__after');
        if (!handle || !after) return;
        // No transition while the user drags the separator — 1:1 tracking
        var dragging = splitter && splitter.classList.contains('q-splitter--active');
        handle.style.transition = dragging ? 'none' : '';
        if (window.matchMedia('(min-width:768px)').matches) {
            handle.style.right = after.getBoundingClientRect().width + 'px';
        } else {
            handle.style.right = '0px';
        }
    }
    function setup() {
        var after = document.querySelector('.chat-splitter > .q-splitter__after');
        if (!after) { setTimeout(setup, 300); return; }
        new ResizeObserver(upd).observe(after);
        window.addEventListener('resize', upd);
        upd();
    }
    setTimeout(setup, 300);
})();
</script>""")

    def _set_docs_visible(visible: bool) -> None:
        _s["docs_visible"] = visible
        _splitter.set_value(66.7 if visible else 100)  # chat : results = 2 : 1
        if visible:
            mobile_docs.classes(add="open")
            scrim.classes(add="open")
            docs_edge_handle.classes(add="open")
            docs_edge_handle.classes(remove="pulse")
            docs_handle_icon.set_name("chevron_right")
        else:
            mobile_docs.classes(remove="open")
            scrim.classes(remove="open")
            docs_edge_handle.classes(remove="open")
            docs_handle_icon.set_name("chevron_left")

    scrim.on("click", lambda: _set_docs_visible(False))
    history_scrim.on("click", lambda: _set_history_visible(False))

    # ── Querverweis-Cluster dialog ────────────────────────────────────────────
    open_cluster = create_cluster_dialog(
        open_document_fn=open_document,
        pin_fn=lambda r: _on_pin(r),
        get_pinned_ids_fn=lambda: _pinned_ids(),
        render_card_fn=_render_card,
    )

    # ── Page layout ───────────────────────────────────────────────────────────
    with (
        ui.column()
        .classes("w-full bg-gray-900 chat-main-col")
        .style("overflow:hidden;")
    ):
        with (
            ui.splitter(value=100)
            .classes("chat-splitter")
            .style("height:100%; overflow:hidden; width:100%;") as _splitter
        ):
            # ── Left: chat panel ──────────────────────────────────────────────
            with _splitter.before:
                with ui.element("div").style(
                    "position:absolute; top:0; left:0; right:0; bottom:0;"
                    "display:flex; flex-direction:column; overflow:hidden;"
                ):
                    # Header row
                    with ui.row().style(
                        f"flex-shrink:0; height:{_TABS_H};"
                        "background:var(--c-surface); border-bottom:1px solid var(--c-border);"
                        "align-items:center; padding:0 12px; gap:8px;"
                        "flex-wrap:nowrap !important; overflow:hidden;"
                    ):
                        history_toggle_btn = (
                            ui.button(icon="menu")
                            .props("flat dark dense")
                            .classes("text-gray-400")
                            .tooltip(_("Conversation history"))
                        )
                        history_toggle_btn.on_click(
                            lambda: _set_history_visible(not _s["history_visible"])
                        )
                        ui.icon("chat", size="xs").classes("text-purple-400")
                        ui.label(_("Chat")).classes(
                            "text-gray-200 font-semibold text-sm"
                        ).style("flex-shrink:0;")
                        # Spacer — pushes spinner + model + buttons to right
                        ui.element("div").style("flex:1;")
                        # Context usage progress
                        with ui.element("div").style(
                            "display:inline-flex; align-items:center; gap:4px; cursor:default;"
                        ):
                            with ui.element("div").style(
                                "position:relative; display:inline-flex;"
                                "align-items:center; justify-content:center;"
                                "width:30px; height:30px;"
                            ):
                                ctx_progress = ui.circular_progress(
                                    value=0.0, min=0.0, max=1.0, size="30px",
                                    color="purple", show_value=False,
                                ).props("thickness=0.2").style("opacity:0.35;")
                                ctx_pct_label = ui.label("").style(
                                    "position:absolute;"
                                    "top:50%; left:50%; transform:translate(-50%,-50%);"
                                    "font-size:8px; font-weight:700;"
                                    "color:#c4b5fd; pointer-events:none; line-height:1;"
                                    "white-space:nowrap;"
                                )
                                ctx_tt = ui.tooltip(_("Context: –"))

                        if _reg_models:
                            _chat_model_sel = (
                                ui.select(
                                    options={m["name"]: m["name"] for m in _reg_models},
                                    value=_initial_model_name,
                                )
                                .props("dense outlined dark")
                                .classes("chat-model-sel")
                                .style("flex-shrink:0; width:13rem;")
                            )

                            def _on_chat_model_change(e) -> None:
                                name = e.value
                                _s["chat_model_name"] = name
                                ng_app.storage.user["chat_model_name"] = name
                                rm = next((m for m in _reg_models if m["name"] == name), None)
                                if rm and rm.get("temperature"):
                                    _s["temperature"] = float(rm["temperature"])
                                # Reset API-reported context_window so _get_model_ctx_win() takes over
                                _s["context_window"] = 0
                                _update_context_progress()
                                _update_sync_block()

                            _chat_model_sel.on_value_change(_on_chat_model_change)
                        else:
                            ui.label(_("No models configured")).classes("text-xs text-red-400 self-center")

                        ui.button(
                            icon="delete_sweep",
                            on_click=lambda: _clear_chat(),
                        ).props("flat dark dense").classes("text-gray-400").tooltip(
                            _("Delete conversation")
                        )

                    # Messages scroll area
                    messages_scroll = ui.scroll_area().style("flex:1; min-height:0;")
                    with messages_scroll:
                        messages_container = (
                            ui.column()
                            .classes("w-full gap-3 p-4 pb-2")
                            .style("max-width:860px; margin:0 auto;")
                        )

                    # Activity bar — shows current tool while running
                    activity_row = ui.row().style(
                        "flex-shrink:0; min-height:22px;"
                        "background:var(--c-surface-2); border-top:1px solid var(--c-surface);"
                    )
                    activity_row.set_visibility(False)
                    with activity_row:
                        with ui.element("div").style(
                            "max-width:860px; margin:0 auto; width:100%;"
                            "display:flex; align-items:flex-start; gap:6px;"
                            "padding:3px 12px; flex-wrap:nowrap;"
                        ):
                            ui.spinner(size="xs").classes("text-purple-400").style("flex-shrink:0; margin-top:2px;")
                            activity_label = ui.label("").classes(
                                "text-xs text-gray-500"
                            ).style("white-space:normal; word-break:break-word; line-height:1.4;")

                    # Sync-blocking banner
                    sync_banner = ui.row().style(
                        "flex-shrink:0; background:#78350f22; border-top:1px solid #92400e;"
                    )
                    sync_banner.set_visibility(
                        sync_state.is_running[0] and _chat_uses_local_lane()
                    )
                    with sync_banner:
                        with ui.element("div").style(
                            "max-width:860px; margin:0 auto; width:100%;"
                            "display:flex; align-items:center; justify-content:center;"
                            "gap:6px; padding:4px 12px;"
                        ):
                            ui.icon("sync", size="xs").classes("text-yellow-500")
                            ui.label(_("Sync running — chat is locked")).classes(
                                "text-yellow-400 text-xs"
                            )

                    # No chat model configured — a fresh install has an empty
                    # registry, and without this the input accepts a message that
                    # can never be answered.
                    no_model_banner = ui.row().style(
                        "flex-shrink:0; background:#7f1d1d22; border-top:1px solid #b91c1c;"
                    )
                    no_model_banner.set_visibility(not _reg_backends)
                    with no_model_banner:
                        with ui.element("div").style(
                            "max-width:860px; margin:0 auto; width:100%;"
                            "display:flex; align-items:center; justify-content:center;"
                            "gap:6px; padding:4px 12px; flex-wrap:wrap;"
                        ):
                            ui.icon("smart_toy", size="xs").classes("text-red-400")
                            ui.label(_("No chat model configured")).classes(
                                "text-red-300 text-xs"
                            )
                            ui.button(
                                _("Set one up"),
                                on_click=lambda: ui.navigate.to("/settings"),
                            ).props("flat dense no-caps color=red").classes("text-xs")

                    # Tool toggle panel (collapsible, hidden by default)
                    tools_panel = (
                        ui.row()
                        .classes("chat-tool-panel")
                        .style(
                            "flex-shrink:0; background:var(--c-surface-2); border-top:1px solid var(--c-surface);"
                        )
                    )
                    tools_panel.set_visibility(False)
                    with tools_panel:
                        with ui.element("div").style(
                            "max-width:860px; margin:0 auto; width:100%;"
                        ):
                            # ── Section 1: Tools ──────────────────────────────
                            with ui.element("div").style("padding:6px 14px 4px;"):
                                ui.label(_("Tools")).classes(
                                    "text-xs text-gray-500 uppercase tracking-wide mb-2 block"
                                )
                                with ui.row().classes("flex-wrap gap-x-4 gap-y-1 items-center"):
                                    for _grp_name, _grp_icon, _grp_tools in _TOOL_GROUPS:
                                        _initial_val = _s["tool_prefs"].get(_grp_name, True)
                                        _cb = ui.checkbox(
                                            _TOOL_GROUP_LABELS.get(_grp_name, _grp_name), value=_initial_val
                                        ).classes("text-gray-300 chat-tool-cb")
                                        _cb.on_value_change(
                                            lambda e, g=_grp_name: _on_tool_pref_change(g, e.value)
                                        )
                                        _tool_cbs.append((_cb, _grp_name))
                            ui.separator().classes("my-0").style("border-color:var(--c-surface);")
                            # ── Section 2: Web-Modus ──────────────────────────
                            with ui.element("div").style("padding:6px 14px 4px;"):
                                ui.label(_("Web")).classes(
                                    "text-xs text-gray-500 uppercase tracking-wide mb-2 block"
                                )
                                _web_mode_select = (
                                    ui.select(
                                        {"hybrid": _("Hybrid"), "crawl4ai": _("Crawl4AI (thorough)")},
                                        value=_s["web_fetch_mode"],
                                    )
                                    .props("dense outlined dark")
                                    .classes("chat-tool-cb")
                                    .style("min-width:10rem; max-width:18rem; width:fit-content;")
                                )
                                def _on_web_mode_change(e) -> None:
                                    _s["web_fetch_mode"] = e.value
                                    _chat_cfg["web_fetch_mode"] = e.value
                                    _save_chat_settings()
                                _web_mode_select.on_value_change(_on_web_mode_change)
                            ui.separator().classes("my-0").style("border-color:var(--c-surface);")
                            # ── Section 3: Sonstige ───────────────────────────
                            with ui.element("div").style("padding:6px 14px 6px;"):
                                ui.label(_("Other")).classes(
                                    "text-xs text-gray-500 uppercase tracking-wide mb-2 block"
                                )
                                with ui.row().classes("items-center gap-6 flex-wrap"):
                                    with ui.row().classes("items-center gap-2"):
                                        ui.label(_("Max. tool calls")).classes("text-xs text-gray-500")
                                        _max_iter_input = (
                                            ui.number(
                                                value=_s["max_iterations"], min=1, max=64, step=1
                                            )
                                            .props("outlined dark dense")
                                            .classes("chat-tool-cb")
                                            .style("width:64px;")
                                        )
                                        def _on_max_iter_change(e) -> None:
                                            val = max(1, int(e.value or 16))
                                            _s["max_iterations"] = val
                                            _chat_cfg["max_iterations"] = val
                                            _save_chat_settings()
                                        _max_iter_input.on_value_change(_on_max_iter_change)
                                    _think_cb = ui.checkbox(
                                        _("Show thinking log"), value=_s["show_thinking"]
                                    ).classes("text-gray-300 chat-tool-cb")
                                    def _on_think_toggle(e) -> None:
                                        _s["show_thinking"] = e.value
                                        _chat_cfg["show_thinking"] = e.value
                                        _save_chat_settings()
                                    _think_cb.on_value_change(_on_think_toggle)

                    # Pinned docs chips row (auto-hide)
                    pinned_row = ui.row().style(
                        "flex-shrink:0; min-height:30px; background:var(--c-surface-2);"
                        "border-top:1px solid var(--c-surface);"
                    )
                    pinned_row.set_visibility(False)
                    with pinned_row:
                        with ui.element("div").style(
                            "max-width:860px; margin:0 auto; width:100%;"
                            "display:flex; align-items:center; gap:4px;"
                            "padding:4px 12px;"
                        ):
                            ui.icon("push_pin", size="xs").classes(
                                "text-purple-400 flex-shrink-0"
                            )
                            pinned_chips_container = ui.row().classes(
                                "flex-wrap gap-1 flex-1 items-center"
                            )

                    # Quick action chips
                    with (
                        ui.element("div")
                        .classes("chat-quick-strip")
                        .style(
                            "flex-shrink:0; background:var(--c-surface-2); border-top:1px solid var(--c-surface);"
                            "height:36px;"
                        )
                    ):
                        with ui.element("div").style(
                            "width:100%; overflow-x:auto; scrollbar-width:none;"
                            "display:flex; align-items:center; justify-content:safe center;"
                            "gap:6px; padding:4px 12px; height:100%;"
                        ):
                            for _chip_label, _chip_text in _QUICK_CHIPS_LOCAL:
                                ui.button(
                                    _chip_label,
                                    on_click=lambda t=_chip_text: _send_quick(t),
                                ).props("flat dense dark no-wrap").classes(
                                    "text-xs text-gray-400"
                                ).style(
                                    "white-space:nowrap; border:1px solid var(--c-border);"
                                    "border-radius:999px; padding:0 8px;"
                                    "height:24px; min-height:24px;"
                                )

                    # Input row
                    with (
                        ui.row()
                        .classes("chat-input-row")
                        .style(
                            "flex-shrink:0;"
                            "background:var(--c-bg-deep); border-top:1px solid var(--c-surface-2);"
                            "padding:10px 16px;"
                        )
                    ):
                        with ui.element("div").style(
                            "max-width:860px; margin:0 auto; width:100%;"
                            "display:flex; align-items:center; gap:8px;"
                        ):
                            tools_toggle_btn = (
                                ui.button(icon="tune")
                                .props("flat dark")
                                .classes("text-gray-500")
                                .style("flex-shrink:0; min-width:36px; width:36px; height:36px;")
                                .tooltip(_("Configure tools"))
                            )
                            tools_toggle_btn.on_click(lambda: _toggle_tools_panel())

                            compact_btn = (
                                ui.button(icon="compress")
                                .props("flat dark")
                                .classes("text-gray-600")
                                .style("flex-shrink:0; min-width:36px; width:36px; height:36px;")
                                .tooltip(_("Compress chat"))
                            )
                            compact_btn.on_click(lambda: asyncio.ensure_future(_compact_chat()))

                            input_field = (
                                ui.textarea(placeholder=_("Message …"))
                                .classes("flex-1 chat-main-input")
                                .props("outlined dark autogrow rows=1")
                            )
                            _ph_wide = _("Message … (Shift+Enter for a new line)").replace("'", "\\'")
                            _ph_narrow = _("Message …").replace("'", "\\'")
                            ui.add_body_html(f"""<script>
(function(){{
  function _upd(){{
    document.querySelectorAll('.chat-main-input textarea').forEach(function(el){{
      // Kill the browser spellcheck (uses the English dict → marks German red)
      el.spellcheck=false;
      el.setAttribute('autocapitalize','off');
      if(window.matchMedia('(min-width:640px)').matches)
        el.placeholder='{_ph_wide}';
      else
        el.placeholder='{_ph_narrow}';
    }});
  }}
  window.addEventListener('load',_upd);
  window.addEventListener('resize',_upd);
  setTimeout(_upd,500);

}})();
</script>""")

                            def _handle_send_or_stop() -> None:
                                if _s["running"]:
                                    _s["stop_requested"] = True
                                else:
                                    asyncio.ensure_future(do_send())

                            with ui.element("div").classes("chat-mic-btn").style(
                                "display:inline-flex; align-items:center; justify-content:center;"
                                "width:38px; height:38px; border-radius:8px; cursor:pointer;"
                                "background:var(--c-surface-2); flex-shrink:0;"
                                "transition:background .2s, color .2s;"
                            ):
                                ui.icon("mic").style("font-size:20px; color:var(--c-text-muted);")

                            send_btn = (
                                ui.button(icon="send", on_click=_handle_send_or_stop)
                                .props("color=purple unelevated round dense")
                                .classes("chat-send-btn")
                            )

            # ── Right: doc panel ──────────────────────────────────────────────
            with _splitter.after:
                _rs["right_panel"] = ui.element("div").style(
                    "position:absolute; top:0; left:0; right:0; bottom:0;"
                    "background:var(--c-bg);"
                )
                with _rs["right_panel"]:
                    _placeholder()

    # ── Enter key handling ────────────────────────────────────────────────────
    ui.add_head_html("""<script>
(function waitForChatInput() {
    var row = document.querySelector('.chat-input-row');
    if (!row) { setTimeout(waitForChatInput, 100); return; }
    var ta = row.querySelector('textarea');
    if (!ta) { setTimeout(waitForChatInput, 100); return; }
    ta.addEventListener('keydown', function(e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            e.stopPropagation();
            var btn = document.querySelector('.chat-send-btn');
            if (btn) btn.click();
        }
    }, true);
})();
</script>""")

    ui.add_head_html("""<script>
(function initMicInput() {
    var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    function getMicBtn() { return document.querySelector('.chat-mic-btn'); }
    function getTextarea() {
        var row = document.querySelector('.chat-input-row');
        return row ? row.querySelector('textarea') : null;
    }
    function setVueValue(ta, value) {
        var setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
        setter.call(ta, value);
        ta.dispatchEvent(new Event('input', { bubbles: true }));
    }
    function waitForBtn() {
        var btn = getMicBtn();
        if (!btn) { setTimeout(waitForBtn, 200); return; }
        if (!SR) { btn.style.display = 'none'; return; }
        var recognition = new SR();
        recognition.lang = 'de-DE';
        recognition.continuous = false;
        recognition.interimResults = true;
        var listening = false;
        var baseText = '';
        recognition.onresult = function(e) {
            var t = '';
            for (var i = e.resultIndex; i < e.results.length; i++) t += e.results[i][0].transcript;
            var ta = getTextarea();
            if (ta) setVueValue(ta, baseText + t);
        };
        function getIcon(b) { return b.querySelector('.q-icon, i'); }
        function stopListening() {
            listening = false;
            var b = getMicBtn();
            if (b) {
                b.classList.remove('mic-active');
                var ic = getIcon(b);
                if (ic) ic.textContent = 'mic';
            }
        }
        recognition.onend = stopListening;
        recognition.onerror = stopListening;
        btn.addEventListener('click', function(e) {
            e.stopPropagation();
            if (listening) { recognition.stop(); return; }
            var ta = getTextarea();
            baseText = ta ? ta.value : '';
            if (baseText && !baseText.endsWith(' ')) baseText += ' ';
            try {
                recognition.start();
                listening = true;
                btn.classList.add('mic-active');
                var ic = getIcon(btn);
                if (ic) ic.textContent = 'mic_none';
            } catch(ex) { stopListening(); }
        });
    }
    waitForBtn();
})();
</script>""")

    # ── Doc-link bridge ───────────────────────────────────────────────────────
    _doc_handler = ui.element("div").style("display:none;")

    async def open_doc_by_id(doc_id: int) -> None:
        try:
            doc = await get_session_paperless().get_document(doc_id)
        except Exception:
            ui.notify(_("Document #{id} not found or no access.").format(id=doc_id), type="warning")
            return
        try:
            await open_document(DocumentResult(document=doc))
        except Exception as exc:
            # Don't fail silently — a malformed sidecar must not swallow the click
            ui.notify(_("Document #{id} cannot be displayed: {err}").format(id=doc_id, err=exc), type="negative")

    async def _handle_doc_open(e) -> None:
        try:
            args = e.args
            if isinstance(args, (list, tuple)):
                args = args[0] if args else None
            await open_doc_by_id(int(args))
        except (TypeError, ValueError):
            pass

    _doc_handler.on("docOpen", _handle_doc_open)
    _doc_listener_id = list(_doc_handler._event_listeners.keys())[0]

    _vault_handler = ui.element("div").style("display:none;")

    async def _handle_vault_open(e) -> None:
        try:
            args = e.args
            if isinstance(args, (list, tuple)):
                args = args[0] if args else None
            await open_vault_note(str(args))
        except Exception:
            ui.notify(_("Note could not be opened."), type="warning")

    _vault_handler.on("vaultOpen", _handle_vault_open)
    _vault_listener_id = list(_vault_handler._event_listeners.keys())[0]

    ui.add_head_html(f"""<script>
// Delegated handler for data-vault-id / data-doc-id links (survives innerHTML injection)
document.addEventListener('click', function(e) {{
    var v = e.target.closest('[data-vault-id]');
    if (v) {{
        e.preventDefault();
        console.log('[PaperSage] vault link click, id=', v.getAttribute('data-vault-id'));
        window.__openVaultNote(v.getAttribute('data-vault-id'));
        return;
    }}
    var d = e.target.closest('[data-doc-id]');
    if (d) {{ e.preventDefault(); window.__openDocById(d.getAttribute('data-doc-id')); return; }}
}});
window.__openDocById = function(docId) {{
    if (!window.socket || !window.did_handshake) {{
        setTimeout(function() {{ window.__openDocById(docId); }}, 100);
        return;
    }}
    window.socket.emit('event', {{
        id: {_doc_handler.id},
        client_id: window.clientId,
        listener_id: '{_doc_listener_id}',
        args: [String(docId)]
    }});
}};
window.__openVaultNote = function(noteId) {{
    console.log('[PaperlessBrain] __openVaultNote called, noteId=', noteId, 'socket=', !!window.socket, 'handshake=', window.did_handshake);
    if (!window.socket || !window.did_handshake) {{
        console.log('[PaperlessBrain] socket not ready, retrying...');
        setTimeout(function() {{ window.__openVaultNote(noteId); }}, 100);
        return;
    }}
    console.log('[PaperlessBrain] emitting socket event, element_id={_vault_handler.id}, listener_id={_vault_listener_id}');
    window.socket.emit('event', {{
        id: {_vault_handler.id},
        client_id: window.clientId,
        listener_id: '{_vault_listener_id}',
        args: [JSON.stringify(noteId)]
    }});
    console.log('[PaperlessBrain] socket event emitted');
}};
</script>""")

    # ── Mobile layout fix ─────────────────────────────────────────────────────
    ui.add_head_html("""<script>
(function fixMobileChat() {
    function applyLayout() {
        var header = document.querySelector('.q-header');
        var headerBottom = header ? header.getBoundingClientRect().bottom : 52;
        var col = document.querySelector('.chat-main-col');
        if (col) col.style.setProperty('top', headerBottom + 'px', 'important');
        ['.chat-docs-mobile', '.chat-docs-scrim', '.chat-history-drawer', '.chat-history-scrim'].forEach(function(sel) {
            var el = document.querySelector(sel);
            if (el) el.style.setProperty('top', headerBottom + 'px', 'important');
        });
        var vv = window.visualViewport;
        if (vv && col) {
            var kbHeight = Math.max(0, window.innerHeight - (vv.offsetTop + vv.height));
            col.style.setProperty('bottom', kbHeight + 'px', 'important');
        }
    }
    setTimeout(applyLayout, 300);
    window.addEventListener('resize', applyLayout);
    if (window.visualViewport) {
        window.visualViewport.addEventListener('resize', applyLayout);
        window.visualViewport.addEventListener('scroll', applyLayout);
    }
})();
</script>""")

    # ── Sync-block watcher ────────────────────────────────────────────────────
    def _update_sync_block() -> None:
        syncing = sync_state.is_running[0] and _chat_uses_local_lane()
        sync_banner.set_visibility(syncing)
        # An empty model registry blocks permanently, not until sync finishes —
        # but it disables the same two controls, so it rides the same watcher.
        blocked = syncing or not _reg_backends
        if not _s["running"]:
            input_field.set_enabled(not blocked)
            send_btn.set_enabled(not blocked)

    _update_sync_block()  # apply before the first tick, not 2 s later
    ui.timer(2.0, _update_sync_block)

    # ── Callbacks (defined after layout so layout elements are in scope) ───────

    def _pinned_ids() -> set[int]:
        return {d["id"] for d in _s["pinned_docs"]}

    def _refresh_pinned_chips() -> None:
        pinned_chips_container.clear()
        has_pins = bool(_s["pinned_docs"])
        pinned_row.set_visibility(has_pins)
        if not has_pins:
            return
        with pinned_chips_container:
            for _pd in list(_s["pinned_docs"]):
                _title = _pd["title"][:22] + ("…" if len(_pd["title"]) > 22 else "")

                def _make_unpin(entry=_pd):
                    def _unpin() -> None:
                        if entry in _s["pinned_docs"]:
                            _s["pinned_docs"].remove(entry)
                        _sync_pin_storage()
                        _refresh_pinned_chips()
                        _render_results()

                    return _unpin

                with (
                    ui.row()
                    .classes("items-center gap-0 rounded-full px-2")
                    .style("background:#4c1d95; height:22px;")
                ):
                    ui.label(f"#{_pd['id']} {_title}").classes(
                        "text-xs text-purple-200"
                    )
                    ui.button(icon="close", on_click=_make_unpin()).props(
                        "flat dense dark"
                    ).classes("text-purple-300").style(
                        "width:16px; height:16px; font-size:10px; padding:0; min-height:0;"
                    )

    def _sync_pin_storage() -> None:
        ng_app.storage.user["pinned_doc_ids"] = [d["id"] for d in _s["pinned_docs"]]
        ng_app.storage.user["pinned_docs_cache"] = list(_s["pinned_docs"])

    def _on_pin(result: DocumentResult) -> None:
        doc_id = result.document.id
        if doc_id in _pinned_ids():
            _s["pinned_docs"] = [d for d in _s["pinned_docs"] if d["id"] != doc_id]
            ui.notify(f"#{doc_id} gelöst", timeout=1500)
        else:
            _s["pinned_docs"].append({"id": doc_id, "title": result.document.title})
            ui.notify(f"#{doc_id} angeheftet", type="positive", timeout=1500)
        _sync_pin_storage()
        _refresh_pinned_chips()
        _render_results()

    def _toggle_tools_panel() -> None:
        _s["tools_panel_open"] = not _s["tools_panel_open"]
        tools_panel.set_visibility(_s["tools_panel_open"])
        tools_toggle_btn.classes(
            remove="text-gray-400 text-purple-400",
            add="text-purple-400" if _s["tools_panel_open"] else "text-gray-400",
        )

    def _send_quick(text: str) -> None:
        if not _s["running"]:
            input_field.set_value(text)
            asyncio.ensure_future(do_send())

    _RESULT_SECTIONS = [
        ("docs", _("Documents"), "description"),
        ("brain", _("Memory"), "psychology"),
        ("vault", _("Notes"), "book"),
        ("web", _("Web search"), "public"),
    ]

    def _tab_counts() -> dict[str, int]:
        return {
            "docs": len(_rs["last_results"]),
            "brain": len(_rs["brain_results"]),
            "vault": len(_rs["vault_results"]),
            "web": len(_rs["web_results"]),
        }

    def _update_results_btn(pulse: bool = False) -> None:
        total = sum(_tab_counts().values())
        if total:
            docs_badge.set_text("99+" if total > 99 else str(total))
            docs_badge.style(remove="display:none;")
            docs_edge_handle.classes(add="has-results")
        else:
            docs_badge.style(add="display:none;")
            docs_edge_handle.classes(remove="has-results pulse")
        if pulse and total and not _s["docs_visible"]:
            docs_edge_handle.classes(add="pulse")

    def _render_results(activate: str | None = None, pulse: bool = False) -> None:
        if activate:
            # Relevant category jumps to the top and expands; all others collapse
            _order = _rs["section_order"]
            if activate in _order:
                _order.remove(activate)
            _order.insert(0, activate)
            _rs["expanded"] = {activate}
        for _n in _rs["vault_results"]:
            if _n.title and _n.pbrain_id:
                _rs["vault_index"][_n.title.lower()] = _n.pbrain_id
        counts = _tab_counts()
        _pids = _pinned_ids()
        _meta = {key: (label, icon) for key, label, icon in _RESULT_SECTIONS}

        def _on_exp_change(e, k) -> None:
            if e.value:
                _rs["expanded"].add(k)
            else:
                _rs["expanded"].discard(k)

        def _fill(container) -> None:
            container.clear()
            with container:
                if not any(counts.values()):
                    _placeholder()
                    return
                # Accordion sections instead of tabs — QTabPanels + clear()
                # silently fails in NiceGUI, and stacked expansions let the
                # relevant category float to the top on new results.
                # height:auto beats nicegui-scroll-area's default 16rem height,
                # so the absolute inset actually stretches to the full panel.
                with ui.scroll_area().classes("chat-results-scroll").style(
                    "position:absolute; top:0; left:0; right:0; bottom:0;"
                    "height:auto; width:auto;"
                ):
                    with ui.column().classes("w-full gap-2 p-2").style(
                        "flex-wrap:nowrap;"
                    ):
                        for key in _rs["section_order"]:
                            label, icon = _meta[key]
                            has = counts[key] > 0
                            exp = (
                                ui.expansion(
                                    value=(has and key in _rs["expanded"])
                                )
                                .classes(
                                    "w-full chat-results-exp"
                                    + (" has-hits" if has else "")
                                )
                                .props("dense" + ("" if has else " disable"))
                            )
                            with exp.add_slot("header"):
                                with ui.row().classes(
                                    "items-center gap-2 no-wrap w-full"
                                ):
                                    ui.icon(icon, size="xs").classes(
                                        "text-purple-400" if has else "text-gray-600"
                                    )
                                    ui.label(label).classes(
                                        "text-xs "
                                        + (
                                            "text-gray-200 font-semibold"
                                            if has
                                            else "text-gray-600"
                                        )
                                    )
                                    ui.label(str(counts[key])).classes("tab-count")
                            exp.on_value_change(
                                lambda e, k=key: _on_exp_change(e, k)
                            )
                            with exp:
                                if key == "web":
                                    with ui.column().classes("gap-3 p-3 w-full"):
                                        for r in _rs["web_results"]:
                                            _render_web_card(r)
                                else:
                                    with ui.row().classes(
                                        "flex-wrap gap-4 p-3 doc-cards-row"
                                    ):
                                        if key == "docs":
                                            for r in _rs["last_results"]:
                                                _render_card(
                                                    r,
                                                    open_document,
                                                    open_cluster,
                                                    on_pin=_on_pin,
                                                    is_pinned=r.document.id in _pids,
                                                )
                                        elif key == "vault":
                                            for n in _rs["vault_results"]:
                                                _render_vault_card(n)
                                        elif key == "brain":
                                            for f in _rs["brain_results"]:
                                                _render_brain_card(f)

        _fill(_rs["right_panel"])
        _fill(mobile_docs_inner)
        _update_results_btn(pulse=pulse)

    def _clear_chat() -> None:
        # Delete current conversation from DB if it exists
        if _s.get("conv_id") and _username:
            _chat_hist.delete_conversation(_s["conv_id"], _username)
        _new_conv()

    async def _scroll_bottom() -> None:
        messages_scroll.scroll_to(percent=1.0)
        await asyncio.sleep(0.05)
        messages_scroll.scroll_to(percent=1.0)

    async def do_send() -> None:
        text = input_field.value.strip()
        if not text or _s["running"]:
            return
        # Checked before any state is mutated. The backend lookup further down
        # happens only after the user bubble is rendered and the button has been
        # switched to "stop", so failing there leaves the UI stuck mid-turn.
        if not _reg_backends:
            ui.notify(
                _("No chat model configured — add one in Settings > AI Models."),
                type="negative",
            )
            return
        if sync_state.is_running[0] and _chat_uses_local_lane():
            ui.notify(_("Sync is currently running — please wait"), type="warning")
            return
        _s["running"] = True
        _s["stop_requested"] = False
        _rs["vault_index"] = {}    # reset per-turn vault link index
        _rs["brain_results"] = []  # reset brain fact cards
        _rs["web_results"] = []    # reset web result cards (accumulate per turn)
        docs_edge_handle.classes(remove="pulse")  # allow pulse re-trigger this turn
        _current_owner.set(ng_app.storage.user.get("paperless_user"))
        _current_token.set(get_session_token())
        _web_fetch_mode.set(_s["web_fetch_mode"])

        input_field.set_value("")
        send_btn.props(add="icon=stop color=red")
        _msg_save_start = len(_s["messages"])  # track index for auto-save

        # Render user bubble (always shows clean text, no pin prefix)
        with messages_container:
            with ui.row().classes("justify-end w-full"):
                ui.html(
                    f'<div style="background:#6d28d9;border-radius:12px 0 12px 12px;'
                    f"padding:10px 14px;font-size:0.875rem;color:#f8fafc;"
                    f'word-break:break-word;line-height:1.5;">'
                    f"{_html.escape(text)}"
                    f"</div>",
                    sanitize=False,
                ).style("max-width:85%;")

        _s["messages"].append({"role": "user", "content": text})
        await _scroll_bottom()
        await asyncio.sleep(0)

        # Sync vault before any brain/vault retrieval this turn — but only AFTER
        # the user bubble is rendered and the input is cleared, so a slow sync
        # doesn't look like a swallowed message (double-Enter then aborted the
        # turn into an empty bubble). force=True: exactly one sync per turn; the
        # cooldown gate would only cause flaky misses of fresh Obsidian edits.
        # The git scan is near-free when nothing changed, and the per-user lock
        # still serializes against other syncs.
        _turn_username = ng_app.storage.user.get("paperless_user", "")
        if _turn_username:
            from vault.sync import sync_user as _vault_sync

            _sync_task = asyncio.ensure_future(_vault_sync(_turn_username, force=True))
            try:
                await asyncio.wait_for(asyncio.shield(_sync_task), timeout=0.4)
            except asyncio.TimeoutError:
                # Sync has real work to do — tell the user instead of stalling silently
                with messages_container:
                    with ui.row().classes("justify-start w-full items-center gap-2") as _sync_row:
                        ui.spinner(size="xs").classes("text-purple-400")
                        ui.label(_("📓 Synchronizing vault …")).classes(
                            "text-xs text-purple-300"
                        )
                await _scroll_bottom()
                try:
                    await _sync_task
                except Exception:
                    pass  # vault sync failure must never block the chat turn
                _sync_row.delete()
            except Exception:
                pass  # vault sync failure must never block the chat turn

        # Inject pinned-doc context into LLM messages without polluting stored history
        llm_messages = list(_s["messages"])
        if _s["pinned_docs"]:
            _pin_prefix = (
                "[Angeheftete Dokumente: "
                + ", ".join(f'#{d["id"]} "{d["title"]}"' for d in _s["pinned_docs"])
                + "]\n\n"
            )
            llm_messages[-1] = {
                "role": "user",
                "content": _pin_prefix + llm_messages[-1]["content"],
            }

        # Create assistant bubble with streaming label
        streaming_label: list = [None]
        thinking_content_el: list = [None]  # ui.html inside persistent <details>
        status_badge: list = [None]
        bubble: list = [None]

        with messages_container:
            with ui.row().classes("justify-start w-full"):
                with ui.element("div").style(
                    "background:var(--c-surface);border-radius:0 12px 12px 12px;"
                    "padding:10px 14px;max-width:85%;font-size:0.875rem;color:var(--c-text-2);"
                    "display:flex;flex-direction:column;gap:6px;"
                ) as b:
                    bubble[0] = b
                    status_badge[0] = ui.html(
                        '<div class="typing-dots">'
                        "<span></span><span></span><span></span>"
                        "</div>",
                        sanitize=False,
                    )
                    # Persistent <details> — browser owns open/closed state; we only
                    # update the inner content div, so state is never lost on refresh.
                    with ui.element("details").style(
                        "margin-bottom:8px;border-left:2px solid var(--c-border);"
                        "padding-left:8px;display:none"
                    ) as _thinking_details_el:
                        with ui.element("summary").style(
                            "color:var(--c-text-muted);font-size:0.72rem;cursor:pointer;"
                            "list-style:none;display:flex;align-items:center;gap:4px"
                        ):
                            ui.html(
                                '&#x1F4AD; ' + _("Thoughts")
                                + '<span style="font-size:0.6rem;margin-left:2px;'
                                'opacity:0.7">▼</span>',
                                sanitize=False,
                            )
                        thinking_content_el[0] = ui.html("", sanitize=False)
                    streaming_label[0] = ui.markdown(
                        "", sanitize=False, extras=_MD_EXTRAS
                    ).classes("chat-md")

        await _scroll_bottom()
        await asyncio.sleep(0)

        accumulated = [""]
        thinking_chunks: list = [[]]  # list of {"text": str, "iter_label": str}
        _STRIP_THINK = re.compile(r"</?think(?:ing)?>", re.IGNORECASE)

        def _build_display() -> str:
            return accumulated[0]

        def _fmt_tool_call_label(label: str, tool_input: dict, iteration: int) -> str:
            clean = label.rstrip(".")
            if tool_input:
                arg_parts = []
                for k, v in list(tool_input.items())[:3]:
                    v_str = f'"{v}"' if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                    if len(v_str) > 40:
                        v_str = v_str[:37] + "…"
                    arg_parts.append(f'"{k}": {v_str}')
                return f"Iter {iteration} → {clean} ({', '.join(arg_parts)})"
            return f"Iter {iteration} → {clean}"

        def _update_thinking() -> None:
            if not _s["show_thinking"] or not thinking_chunks[0]:
                return
            parts = []
            for chunk in thinking_chunks[0]:
                text = _STRIP_THINK.sub("", chunk["text"]).strip()
                if text:
                    esc = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    parts.append(
                        f'<pre style="color:var(--c-text-muted);font-size:0.7rem;white-space:pre-wrap;'
                        f'margin:0 0 2px 0">{esc}</pre>'
                    )
                if chunk["iter_label"]:
                    lbl = chunk["iter_label"].replace("<", "&lt;").replace(">", "&gt;")
                    parts.append(
                        f'<div style="color:#60a5fa;font-size:0.68rem;border-top:1px solid '
                        f'var(--c-border);padding:3px 0 6px 0">{lbl}</div>'
                    )
            thinking_content_el[0].set_content(
                f'<div style="margin-top:4px">{"".join(parts)}</div>'
            )
            _thinking_details_el.style(remove="display:none")

        _iter = [1]
        _tool_trace: list[dict] = []
        _pending_docx: list = [None]  # DocxRequestEvent params if triggered
        _pending_email: list = [None]  # EmailRequestEvent params if triggered
        _pending_pdf: list = [None]     # PdfSaveRequestEvent params if triggered
        _pending_kanban: list = [None]  # KanbanTaskRequestEvent params if triggered
        _pending_downloads: list[dict] = []  # DownloadRequestEvent params (one per doc)

        backend = _reg_backends.get(
            _s["chat_model_name"],
            next(iter(_reg_backends.values())) if _reg_backends else None,
        )
        if backend is None:
            # Reachable only if the registry emptied mid-turn (model deleted in
            # another tab). do_send has already switched the button to "stop" and
            # rendered the user bubble, so both have to be undone here — leaving
            # them is what made this look like a request hanging forever.
            ui.notify(_("No model configured — please set one up in Settings > AI Models."), type="negative")
            _s["running"] = False
            send_btn.props(remove="icon=stop color=red")
            send_btn.props(add="icon=send color=purple")
            _update_sync_block()
            return

        _username = ng_app.storage.user.get("paperless_user") or ""
        _lang = ng_app.storage.user.get("language", DEFAULT_LANG)
        # Build the prompt from only the ACTIVE tool groups — the model is never
        # told to use a tool that was filtered out of its tool list.
        _system = build_system_prompt(_active_groups(), _username, _lang)

        try:
            async for event in backend.run_turn(
                llm_messages,
                _system,
                _s["temperature"],
                tools=_active_tools(),
                max_iterations=_s["max_iterations"],
            ):
                if _s["stop_requested"]:
                    break
                if isinstance(event, ThinkingEvent):
                    thinking_chunks[0].append({"text": event.text, "iter_label": ""})
                    if _s["show_thinking"]:
                        _update_thinking()
                        messages_scroll.scroll_to(percent=1.0)
                        await asyncio.sleep(0)

                elif isinstance(event, TextTokenEvent):
                    accumulated[0] += event.text
                    streaming_label[0].set_content(_build_display())
                    messages_scroll.scroll_to(percent=1.0)
                    await asyncio.sleep(0)

                elif isinstance(event, IterationEvent):
                    _iter[0] = event.iteration

                elif isinstance(event, ToolCallEvent):
                    if thinking_chunks[0]:
                        thinking_chunks[0][-1]["iter_label"] = _fmt_tool_call_label(
                            event.label, event.tool_input, _iter[0]
                        )
                        if _s["show_thinking"]:
                            _update_thinking()
                    _tool_trace.append(
                        {
                            "iter": _iter[0],
                            "name": event.tool_name,
                            "label": event.label,
                            "input": event.tool_input,
                            "output": "",
                        }
                    )
                    status_badge[0].set_content(
                        f'<span style="font-size:0.72rem;color:#a78bfa;font-family:inherit;">'
                        f"{event.label} · {_iter[0]}/{_s['max_iterations']}"
                        f"</span>"
                    )
                    status_badge[0].style("display:block;")
                    hint = _activity_hint(event.tool_name, event.tool_input)
                    activity_label.set_text(f"{event.label}{hint}")
                    activity_row.set_visibility(True)
                    await asyncio.sleep(0)

                elif isinstance(event, ToolResultEvent):
                    if _tool_trace:
                        _tool_trace[-1]["output"] = event.tool_output

                elif isinstance(event, DocsRetrievedEvent):
                    _rs["last_results"] = event.results
                    _render_results(activate="docs", pulse=True)
                    await asyncio.sleep(0)

                elif isinstance(event, VaultNotesRetrievedEvent):
                    _rs["vault_results"] = event.notes
                    _render_results(activate="vault", pulse=True)
                    await asyncio.sleep(0)

                elif isinstance(event, BrainFactsRetrievedEvent):
                    _rs["brain_results"] = event.facts
                    _render_results(activate="brain", pulse=True)
                    await asyncio.sleep(0)

                elif isinstance(event, WebResultsRetrievedEvent):
                    _seen_urls = {w.url for w in _rs["web_results"]}
                    _rs["web_results"].extend(
                        w for w in event.results if w.url not in _seen_urls
                    )
                    _render_results(activate="web", pulse=True)
                    await asyncio.sleep(0)

                elif isinstance(event, DocxRequestEvent):
                    _pending_docx[0] = event.params

                elif isinstance(event, EmailRequestEvent):
                    _pending_email[0] = event.params

                elif isinstance(event, PdfSaveRequestEvent):
                    _pending_pdf[0] = event.params
                elif isinstance(event, KanbanTaskRequestEvent):
                    _pending_kanban[0] = event.params

                elif isinstance(event, DownloadRequestEvent):
                    _pending_downloads.append(event.params)

                elif isinstance(event, DoneEvent):
                    status_badge[0].style("display:none;")
                    activity_row.set_visibility(False)
                    if event.input_tokens:
                        _s["total_tokens"] += event.input_tokens
                        _s["current_ctx_tokens"] = event.input_tokens
                    if event.context_window:
                        _s["context_window"] = event.context_window
                    _update_context_progress()

        except Exception as e:
            error_text = _("Error: {err}").format(err=e)
            accumulated[0] = accumulated[0] or error_text
            streaming_label[0].set_content(accumulated[0])

        finally:
            status_badge[0].style("display:none;")
            activity_row.set_visibility(False)
            if thinking_chunks[0] and _s["show_thinking"]:
                _update_thinking()
            if accumulated[0]:
                await _resolve_vault_wiki_links(
                    accumulated[0], _rs.setdefault("vault_index", {}), _username
                )
                streaming_label[0].set_content(
                    _inject_doc_links(accumulated[0], _rs.get("vault_index"))
                )
                _s["messages"].append({"role": "assistant", "content": accumulated[0]})
                _update_context_progress()  # re-estimate now that assistant msg is in history
            _md_raw = accumulated[0]

            def _dl_md(_c=_md_raw):
                if _c:
                    ui.download(_c.encode("utf-8"), _("answer.md"), media_type="text/markdown")

            # ── Build tool-trace dialog (button added to shared footer row below) ──
            trace_dlg = None
            if _tool_trace:
                trace = list(_tool_trace)
                with bubble[0]:
                    with ui.dialog() as trace_dlg:
                        with ui.card().style(
                            "background:var(--c-surface); width:min(700px, 95vw);"
                        ):
                            with ui.row().classes("items-center justify-between mb-2"):
                                ui.label(_("Tool history")).classes(
                                    "text-sm font-semibold text-gray-100"
                                )
                                ui.button(icon="close", on_click=trace_dlg.close).props(
                                    "flat dark dense"
                                ).classes("text-gray-400")
                            ui.separator()
                            with ui.column().style(
                                "max-height:65vh; overflow-y:auto;"
                                " gap:8px; margin-top:8px; width:100%;"
                            ):
                                for step in trace:
                                    with ui.card().style(
                                        "background:var(--c-bg); width:100%; padding:10px;"
                                    ):
                                        with ui.row().classes(
                                            "items-center gap-2 mb-1"
                                        ):
                                            ui.badge(
                                                f"Iter {step['iter']}", color="purple"
                                            ).classes("text-xs")
                                            ui.label(step["label"].rstrip(".")).classes(
                                                "text-xs text-gray-300 font-medium"
                                            )
                                        inp = step["input"]
                                        if inp:
                                            ui.label(
                                                json.dumps(
                                                    inp, ensure_ascii=False, indent=2
                                                )
                                            ).classes(
                                                "text-xs text-gray-500 font-mono"
                                                " whitespace-pre-wrap"
                                            )
                                        else:
                                            ui.label(_("(no parameters)")).classes(
                                                "text-xs text-gray-600 italic"
                                            )
                                        out = step.get("output", "")
                                        if out:
                                            ui.separator().style("margin:4px 0;")
                                            ui.label(f"→ {out}").classes(
                                                "text-xs text-green-500 font-mono"
                                                " whitespace-pre-wrap"
                                            )

            # ── Single shared footer row: .MD | CSV… | ⓘ ─────────────────────
            _csvs = _extract_markdown_csvs(accumulated[0]) if accumulated[0] else []
            _multi = len(_csvs) > 1
            if _md_raw or _csvs or trace_dlg:
                with bubble[0]:
                    with ui.row().classes("justify-end w-full mt-1 gap-1"):
                        if _md_raw:
                            ui.button(".MD", icon="download", on_click=_dl_md).props(
                                "flat dark dense"
                            ).classes("text-gray-600 text-xs").tooltip(
                                _("Download answer as Markdown")
                            )
                        for _ci, _csv_bytes in enumerate(_csvs):
                            _label = f"CSV{_ci + 1}" if _multi else "CSV"

                            def _dl_csv(_b=_csv_bytes, _n=_ci + 1, _m=_multi):
                                ui.download(
                                    _b,
                                    f"tabelle_{_n}.csv" if _m else "tabelle.csv",
                                    media_type="text/csv",
                                )

                            ui.button(_label, icon="download", on_click=_dl_csv).props(
                                "flat dark dense"
                            ).classes("text-gray-500 text-xs").tooltip(
                                _("Table {n} as CSV").format(n=_ci + 1)
                                if _multi
                                else _("Download table as CSV")
                            )
                        if trace_dlg is not None:
                            ui.button(
                                icon="info_outline", on_click=trace_dlg.open
                            ).props("flat dark dense").classes("text-gray-600").tooltip(
                                _("Show tool history")
                            )

            # ── Letter dialog (triggered by trigger_docx_generation tool) ──────
            if _pending_docx[0] is not None:
                _dp = _pending_docx[0]
                _username2 = ng_app.storage.user.get("paperless_user") or ""
                _token2 = get_session_token() or ""
                _sp = (
                    load_credentials(_username2, _token2)
                    if _username2 and _token2
                    else {}
                ).get("sender_profile", {})

                with bubble[0]:
                    with ui.dialog().props("persistent") as _letter_dlg:
                        with ui.card().style(
                            "background:var(--c-surface); width:min(520px, 95vw); max-width:min(760px, 95vw); max-height:90vh; overflow-y:auto;"
                        ):
                            with ui.row().classes("items-center justify-between mb-2"):
                                ui.label(_("Create letter")).classes(
                                    "text-base font-semibold text-gray-100"
                                )
                                ui.button(
                                    icon="close", on_click=_letter_dlg.close
                                ).props("flat dark dense").classes("text-gray-400")
                            ui.separator()

                            # Sender fields
                            ui.label(_("Sender")).classes(
                                "text-xs text-gray-400 mt-3 mb-1 font-semibold uppercase tracking-wide"
                            )
                            _lf_sname = (
                                ui.input(_("Name"), value=_sp.get("name", ""))
                                .props("outlined dark dense")
                                .classes("w-full")
                            )
                            _lf_scompany = (
                                ui.input(_("Company"), value=_sp.get("company", ""))
                                .props("outlined dark dense")
                                .classes("w-full")
                            )
                            _lf_sstreet = (
                                ui.input(_("Street"), value=_sp.get("street", ""))
                                .props("outlined dark dense")
                                .classes("w-full")
                            )
                            with ui.row().classes("w-full gap-2"):
                                _lf_splz = (
                                    ui.input(_("Postcode"), value=_sp.get("plz", ""))
                                    .props("outlined dark dense")
                                    .style("width:30%")
                                )
                                _lf_scity = (
                                    ui.input(_("City"), value=_sp.get("city", ""))
                                    .props("outlined dark dense")
                                    .style("flex:1")
                                )
                            _lf_sphone = (
                                ui.input(_("Phone"), value=_sp.get("phone", ""))
                                .props("outlined dark dense")
                                .classes("w-full")
                            )
                            _lf_semail = (
                                ui.input(_("Email"), value=_sp.get("email", ""))
                                .props("outlined dark dense")
                                .classes("w-full")
                            )

                            ui.separator().classes("my-3")

                            # Recipient fields (pre-filled from LLM params)
                            ui.label(_("Recipient")).classes(
                                "text-xs text-gray-400 mb-1 font-semibold uppercase tracking-wide"
                            )
                            _lf_rname = (
                                ui.input(_("Name"), value=_dp.get("recipient_name", ""))
                                .props("outlined dark dense")
                                .classes("w-full")
                            )
                            _lf_rstreet = (
                                ui.input(
                                    _("Street"), value=_dp.get("recipient_street", "")
                                )
                                .props("outlined dark dense")
                                .classes("w-full")
                            )
                            with ui.row().classes("w-full gap-2"):
                                _lf_rplz = (
                                    ui.input(
                                        _("Postcode"), value=_dp.get("recipient_postcode", "")
                                    )
                                    .props("outlined dark dense")
                                    .style("width:30%")
                                )
                                _lf_rcity = (
                                    ui.input(_("City"), value=_dp.get("recipient_city", ""))
                                    .props("outlined dark dense")
                                    .style("flex:1")
                                )

                            ui.separator().classes("my-3")

                            # Letter content
                            ui.label(_("Content")).classes(
                                "text-xs text-gray-400 mb-1 font-semibold uppercase tracking-wide"
                            )
                            _lf_crossref = (
                                ui.input(
                                    _("Your reference (invoice no. / file no.)"),
                                    value=_dp.get("source_cross_ref", ""),
                                )
                                .props("outlined dark dense")
                                .classes("w-full")
                            )
                            _lf_bezug = (
                                ui.input(
                                    _("Reference (short title of ref. doc., empty = none)"),
                                    value=_dp.get("src_doc_info", ""),
                                )
                                .props("outlined dark dense")
                                .classes("w-full")
                            )
                            _lf_subject = (
                                ui.input(_("Subject"), value=_dp.get("subject", ""))
                                .props("outlined dark dense")
                                .classes("w-full")
                            )
                            _lf_salutation = (
                                ui.input(
                                    _("Salutation"),
                                    value=_dp.get(
                                        "salutation", "Sehr geehrte Damen und Herren,"
                                    ),
                                )
                                .props("outlined dark dense")
                                .classes("w-full")
                            )
                            _lf_body = (
                                ui.textarea(
                                    _("Letter body (separate paragraphs with blank lines)"),
                                    value="\n\n".join(_dp.get("body_paras", [])),
                                )
                                .props("outlined dark dense")
                                .classes("w-full")
                                .style("min-height:140px;")
                            )
                            _lf_closing = (
                                ui.input(
                                    _("Closing"),
                                    value=_sp.get("closing", "Mit freundlichen Grüßen"),
                                )
                                .props("outlined dark dense")
                                .classes("w-full")
                            )

                            ui.separator().classes("my-3")

                            def _build_docx_bytes():
                                from services.docx_service import generate_letter_docx

                                _body_paras = [
                                    p.strip()
                                    for p in _lf_body.value.split("\n\n")
                                    if p.strip()
                                ]
                                return generate_letter_docx(
                                    sender={
                                        "name": _lf_sname.value,
                                        "company": _lf_scompany.value,
                                        "street": _lf_sstreet.value,
                                        "plz": _lf_splz.value,
                                        "city": _lf_scity.value,
                                        "phone": _lf_sphone.value,
                                        "email": _lf_semail.value,
                                    },
                                    recipient={
                                        "name": _lf_rname.value,
                                        "street": _lf_rstreet.value,
                                        "postcode": _lf_rplz.value,
                                        "city": _lf_rcity.value,
                                    },
                                    subject=_lf_subject.value,
                                    salutation=_lf_salutation.value,
                                    body_paras=_body_paras,
                                    closing=_lf_closing.value
                                    or "Mit freundlichen Grüßen",
                                    source_cross_ref=_lf_crossref.value.strip(),
                                    src_doc_info=_lf_bezug.value.strip(),
                                    reference_doc_id=_dp.get("reference_doc_id"),
                                )

                            async def _download_letter():
                                try:
                                    import re as _re
                                    from datetime import date as _d

                                    _docx_bytes = _build_docx_bytes()
                                    _fn_date = _d.today().strftime("%y_%m_%d")
                                    _fn_subj = _re.sub(
                                        r"[^\wÀ-ſ]+", "_", _lf_subject.value.strip()
                                    )[:28].strip("_")
                                    _fn_name = (
                                        _lf_rname.value.strip().split()[0]
                                        if _lf_rname.value.strip()
                                        else "Empfaenger"
                                    )
                                    _fn_name = _re.sub(r"[^\wÀ-ſ]", "", _fn_name)
                                    _filename = (
                                        f"{_fn_date}_Brief_{_fn_subj}_{_fn_name}.docx"
                                    )
                                    ui.download(
                                        _docx_bytes,
                                        _filename,
                                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                    )
                                except Exception as ex:
                                    ui.notify(_("Error: {err}").format(err=ex), type="negative")

                            with ui.row().classes("justify-end gap-2 mt-2"):
                                ui.button(
                                    _("Cancel"), on_click=_letter_dlg.close
                                ).props("flat dark dense").classes("text-gray-400")
                                ui.button(
                                    _("Download letter"),
                                    icon="download",
                                    on_click=_download_letter,
                                ).props("dark").classes("text-purple-300")

                _letter_dlg.open()

            # ── Email copy dialog (triggered by create_email tool) ────────────
            if _pending_email[0] is not None:
                _ep = _pending_email[0]
                _ep_body_paras = _ep.get("body_paras", [])
                _ep_salutation = _ep.get("salutation", "")
                _ep_full_body = (
                    "\n\n".join([_ep_salutation] + _ep_body_paras)
                    if _ep_salutation
                    else "\n\n".join(_ep_body_paras)
                )

                with bubble[0]:
                    with ui.dialog().props("persistent") as _email_dlg:
                        with ui.card().style(
                            "background:var(--c-surface); width:min(440px, 95vw); max-width:min(640px, 95vw);"
                        ):
                            with ui.row().classes("items-center justify-between mb-2"):
                                ui.label(_("Email")).classes(
                                    "text-base font-semibold text-gray-100"
                                )
                                ui.button(
                                    icon="close", on_click=_email_dlg.close
                                ).props("flat dark dense").classes("text-gray-400")
                            ui.separator()

                            # An:
                            ui.label(_("To")).classes(
                                "text-xs text-gray-400 mt-3 mb-1 font-semibold uppercase tracking-wide"
                            )
                            with ui.row().classes("w-full items-center gap-2"):
                                ui.label(_ep.get("recipient_email", "")).classes(
                                    "flex-1 text-sm text-gray-200 font-mono px-3 py-2 rounded"
                                ).style("background:var(--c-bg); word-break:break-all;")

                                async def _copy_addr(_v=_ep.get("recipient_email", "")):
                                    await ui.run_javascript(
                                        f"navigator.clipboard.writeText({json.dumps(_v)})"
                                    )
                                    ui.notify(_("Copied!"), timeout=1200)

                                ui.button(
                                    icon="content_copy", on_click=_copy_addr
                                ).props("flat dark dense").classes(
                                    "text-gray-500"
                                ).tooltip(_("Copy address"))

                            # Betreff:
                            ui.label(_("Subject")).classes(
                                "text-xs text-gray-400 mt-3 mb-1 font-semibold uppercase tracking-wide"
                            )
                            with ui.row().classes("w-full items-center gap-2"):
                                ui.label(_ep.get("subject", "")).classes(
                                    "flex-1 text-sm text-gray-200 px-3 py-2 rounded"
                                ).style("background:var(--c-bg);")

                                async def _copy_subj(_v=_ep.get("subject", "")):
                                    await ui.run_javascript(
                                        f"navigator.clipboard.writeText({json.dumps(_v)})"
                                    )
                                    ui.notify(_("Copied!"), timeout=1200)

                                ui.button(
                                    icon="content_copy", on_click=_copy_subj
                                ).props("flat dark dense").classes(
                                    "text-gray-500"
                                ).tooltip(_("Copy subject"))

                            # Nachricht:
                            ui.label(_("Message")).classes(
                                "text-xs text-gray-400 mt-3 mb-1 font-semibold uppercase tracking-wide"
                            )
                            with ui.column().classes("w-full gap-1"):
                                ui.textarea(value=_ep_full_body).props(
                                    "outlined dark dense readonly"
                                ).classes("w-full").style(
                                    "min-height:160px; font-size:0.8rem;"
                                )

                                async def _copy_body(_v=_ep_full_body):
                                    await ui.run_javascript(
                                        f"navigator.clipboard.writeText({json.dumps(_v)})"
                                    )
                                    ui.notify(_("Message copied!"), timeout=1200)

                                ui.button(
                                    _("Copy message"),
                                    icon="content_copy",
                                    on_click=_copy_body,
                                ).props("flat dark dense").classes(
                                    "text-gray-500 text-xs self-end"
                                )

                            _mailto_url = (
                                "mailto:"
                                + urllib.parse.quote(
                                    _ep.get("recipient_email", ""), safe="@"
                                )
                                + "?"
                                + urllib.parse.urlencode(
                                    {
                                        "subject": _ep.get("subject", ""),
                                        "body": _ep_full_body,
                                    },
                                    quote_via=urllib.parse.quote,
                                )
                            )
                            with ui.row().classes("justify-end gap-2 mt-3"):
                                ui.button(_("Close"), on_click=_email_dlg.close).props(
                                    "flat dark dense"
                                ).classes("text-gray-400")

                                async def _open_mailto(_url=_mailto_url):
                                    await ui.run_javascript(
                                        f"window.location.href = {json.dumps(_url)}"
                                    )

                                ui.button(
                                    _("Open in mail app"),
                                    icon="open_in_new",
                                    on_click=_open_mailto,
                                ).props("unelevated dark").classes("text-purple-300")

                _email_dlg.open()

            # ── PDF save dialog (triggered by generate_chat_pdf tool) ─────────
            if _pending_pdf[0] is not None:
                _pp = _pending_pdf[0]
                _pp_username = ng_app.storage.user.get("paperless_user") or ""
                _pp_model = _s.get("ollama_model") or _s.get("backend") or "AI"

                with bubble[0]:
                    with ui.dialog().props("persistent") as _pdf_dlg:
                        with ui.card().style(
                            "background:var(--c-surface); width:min(540px, 95vw); max-width:min(760px, 95vw); max-height:90vh; overflow-y:auto;"
                        ):
                            with ui.row().classes("items-center justify-between mb-2"):
                                ui.label(_("Save document to Paperless")).classes(
                                    "text-base font-semibold text-gray-100"
                                )
                                ui.button(icon="close", on_click=_pdf_dlg.close).props(
                                    "flat dark dense"
                                ).classes("text-gray-400")
                            ui.separator()

                            ui.label(_("Title")).classes(
                                "text-xs text-gray-400 mt-3 mb-1 font-semibold uppercase tracking-wide"
                            )
                            _pdf_title = (
                                ui.input(value=_pp.get("title", ""))
                                .props("outlined dark dense")
                                .classes("w-full")
                            )

                            _pdf_edit_mode = [False]
                            with ui.row().classes("items-center justify-between mt-3 mb-1"):
                                ui.label(_("Content")).classes(
                                    "text-xs text-gray-400 font-semibold uppercase tracking-wide"
                                )
                                _pdf_mode_btn = ui.button(
                                    _("Edit"), icon="edit"
                                ).props("flat dark dense").classes("text-xs text-gray-500")

                            _pdf_content = (
                                ui.textarea(value=_pp.get("content_markdown", ""))
                                .props("outlined dark dense")
                                .classes("w-full")
                                .style("min-height:220px; font-size:0.78rem; font-family:monospace;")
                            )
                            _pdf_content.set_visibility(False)

                            _pdf_preview = (
                                ui.markdown(_pp.get("content_markdown", ""), sanitize=False)
                                .classes("chat-md w-full")
                                .style(
                                    "min-height:220px; padding:10px 12px; border:1px solid var(--c-border);"
                                    "border-radius:4px; overflow-y:auto; background:var(--c-bg);"
                                )
                            )

                            def _toggle_pdf_mode():
                                if not _pdf_edit_mode[0]:
                                    # → edit
                                    _pdf_edit_mode[0] = True
                                    _pdf_preview.set_visibility(False)
                                    _pdf_content.set_visibility(True)
                                    _pdf_mode_btn.set_text(_("Preview"))
                                    _pdf_mode_btn.props("icon=visibility")
                                else:
                                    # → preview
                                    _pdf_edit_mode[0] = False
                                    _pdf_preview.set_content(_pdf_content.value)
                                    _pdf_content.set_visibility(False)
                                    _pdf_preview.set_visibility(True)
                                    _pdf_mode_btn.set_text(_("Edit"))
                                    _pdf_mode_btn.props("icon=edit")

                            _pdf_mode_btn.on_click(_toggle_pdf_mode)

                            _pdf_status = ui.label("").classes("text-xs mt-2")

                            async def _do_save_pdf():
                                _pdf_status.set_text(_("Creating PDF…"))
                                _pdf_status.classes(
                                    remove="text-red-400 text-green-400",
                                    add="text-gray-400",
                                )
                                await asyncio.sleep(0)
                                try:
                                    # Imported here (not at module level) to keep the
                                    # chat page import-light — but inside the try, so an
                                    # import failure surfaces in the UI instead of
                                    # killing the handler silently.
                                    from services.clients import get_session_paperless
                                    from werkbank.export import upload_pdf as _upload_pdf

                                    _pl = get_session_paperless()
                                    _pdf_status.set_text(_("Uploading…"))
                                    await asyncio.sleep(0)
                                    _upload_task_id, _filename = await _upload_pdf(
                                        content_markdown=_pdf_content.value,
                                        title=_pdf_title.value,
                                        username=_pp_username,
                                        model_name=_pp_model,
                                        filename_slug=_pp.get("filename_topic", "Dokument"),
                                        paperless_client=_pl,
                                    )
                                    _pdf_status.set_text(
                                        _("Saved as: {filename}").format(filename=_filename)
                                    )
                                    _pdf_status.classes(
                                        remove="text-gray-400 text-red-400",
                                        add="text-green-400",
                                    )
                                except Exception as _ex:
                                    _pdf_status.set_text(_("Error: {err}").format(err=_ex))
                                    _pdf_status.classes(
                                        remove="text-gray-400 text-green-400",
                                        add="text-red-400",
                                    )

                            with ui.row().classes("justify-end gap-2 mt-3"):
                                ui.button(_("Cancel"), on_click=_pdf_dlg.close).props(
                                    "flat dark dense"
                                ).classes("text-gray-400")
                                ui.button(
                                    _("Save to Paperless"),
                                    icon="save",
                                    on_click=_do_save_pdf,
                                ).props("unelevated dark").classes("text-purple-300")

                _pdf_dlg.open()

            # ── Kanban task dialog ────────────────────────────────────────────
            if _pending_kanban[0] is not None:
                _kp = _pending_kanban[0]
                _wb_username = ng_app.storage.user.get("paperless_user") or ""
                _wb_token    = get_session_token() or ""

                with bubble[0]:
                    with ui.dialog().props("persistent") as _kanban_dlg:
                        with ui.card().style(
                            "background:var(--c-surface);width:min(560px,95vw);max-height:90vh;overflow-y:auto;"
                        ):
                            ui.label(_("Start deep research")).classes(
                                "text-base font-semibold text-gray-100 mb-1"
                            )
                            ui.label(
                                _("Review and adjust the task, then choose the model.")
                            ).classes("text-xs text-gray-500 mb-3")

                            _kb_title = ui.input(
                                _("Title"), value=_kp.get("title", "")
                            ).props("outlined dark dense").classes("w-full mb-2")

                            _kb_request = ui.textarea(
                                _("Task"), value=_kp.get("request", "")
                            ).props("outlined dark").classes("w-full").style(
                                "min-height:140px;font-family:monospace;font-size:12px;"
                                "background:var(--c-bg);color:var(--c-text-2);"
                            )

                            from services.model_registry import get_models as _get_wb_models
                            _wb_models = [
                                m["name"] for m in _get_wb_models(_wb_username, _wb_token)
                                if m.get("enabled", True)
                            ]
                            _kb_model = ui.select(
                                _wb_models,
                                label=_("Model"),
                                value=_wb_models[0] if _wb_models else None,
                            ).props("outlined dark dense").classes("w-full mt-2")

                            _kb_status = ui.label("").classes("text-xs mt-2")

                            async def _start_kanban_task():
                                from werkbank import repository as _wbr
                                from werkbank.models import TaskStatus as _TaskStatus
                                from werkbank.scheduler import register_token as _reg_tok
                                try:
                                    _kb_status.set_text(_("Creating task…"))
                                    _kb_status.classes(add="text-gray-400", remove="text-red-400 text-green-400")
                                    task = _wbr.create_task(
                                        _wb_username,
                                        _kb_request.value.strip(),
                                        _kb_model.value,
                                        language=ng_app.storage.user.get("language", DEFAULT_LANG),
                                    )
                                    _wbr.update_task_title(task.id, _wb_username, _kb_title.value.strip())
                                    # Request already reformulated by chat LLM — skip Planner,
                                    # write it as refined_request and go straight to QUEUED.
                                    _wbr.update_task_refined_request(
                                        task.id, _wb_username, _kb_request.value.strip()
                                    )
                                    _reg_tok(task.id, _wb_token)
                                    _wbr.update_task_status(task.id, _wb_username, _TaskStatus.QUEUED)
                                    _kb_status.set_text(_("Task #{id} running ✓").format(id=task.id))
                                    _kb_status.classes(add="text-green-400", remove="text-gray-400 text-red-400")
                                    await asyncio.sleep(1.5)
                                    _kanban_dlg.close()
                                except Exception as _ex:
                                    _kb_status.set_text(_("Error: {err}").format(err=_ex))
                                    _kb_status.classes(add="text-red-400", remove="text-gray-400 text-green-400")

                            with ui.row().classes("justify-end gap-2 mt-4"):
                                ui.button(_("Cancel"), on_click=_kanban_dlg.close).props(
                                    "flat dark dense"
                                ).classes("text-gray-400")
                                ui.button(
                                    _("Start task"), icon="auto_awesome",
                                    on_click=_start_kanban_task,
                                ).props("unelevated dark").classes("bg-purple-700 text-white")

                _kanban_dlg.open()

            # ── Browser downloads (triggered by download_document tool) ────────
            if _pending_downloads:

                async def _do_doc_download(doc_id: int) -> None:
                    # do_send runs as a background task whose slot stack is empty
                    # after awaits — ui.download/ui.notify need an explicit slot
                    # to resolve the client.
                    try:
                        _dl_bytes, _dl_name = await get_session_paperless().download_document_named(doc_id)
                        print(
                            f"[download] doc={doc_id} file={_dl_name!r} "
                            f"size={len(_dl_bytes)} — pushing to browser",
                            flush=True,
                        )
                        with bubble[0]:
                            ui.download(
                                _dl_bytes,
                                _dl_name,
                                media_type="application/octet-stream",
                            )
                    except Exception as _dl_err:
                        print(f"[download] doc={doc_id} FAILED: {_dl_err!r}", flush=True)
                        with bubble[0]:
                            ui.notify(
                                _(
                                    "Download of document #{id} failed: {err}"
                                ).format(id=doc_id, err=_dl_err),
                                type="negative",
                            )

                _dl_ids = [
                    int(d.get("document_id") or 0) for d in _pending_downloads
                ]
                # Ask-before-download: render a button per document and wait for
                # the user's click. No automatic push — the download only starts
                # on the explicit gesture (which also dodges browser pop-up blocks).
                with bubble[0]:
                    with ui.row().classes("gap-1 mt-1 flex-wrap"):
                        for _did in _dl_ids:
                            ui.button(
                                _("Download document #{id}").format(id=_did),
                                icon="download",
                                on_click=lambda _e, _d=_did: _do_doc_download(_d),
                            ).props("flat dark dense").classes(
                                "text-purple-300 text-xs"
                            )

            send_btn.props(add="icon=send color=purple")
            _s["running"] = False
            _s["stop_requested"] = False
            ng_app.storage.user["chat_history"] = list(_s["messages"])
            ng_app.storage.user["chat_token_count"] = _s["total_tokens"]
            ng_app.storage.user["chat_context_window"] = _s["context_window"]
            messages_scroll.scroll_to(percent=1.0)

            # Auto-save new messages to DB
            new_msgs = _s["messages"][_msg_save_start:]
            if new_msgs and _username:
                if not _s.get("conv_id"):
                    first_user = next(
                        (m["content"] for m in _s["messages"] if m["role"] == "user"),
                        _("New conversation"),
                    )
                    _s["conv_id"] = _chat_hist.create_conversation(_username, first_user[:60])
                    ng_app.storage.user["chat_conv_id"] = _s["conv_id"]
                    _refresh_history_list()
                _chat_hist.append_messages(_s["conv_id"], _username, new_msgs)

    # ── Restore or initialize chat ────────────────────────────────────────────
    _saved = ng_app.storage.user.get("chat_history") or []
    if _saved:
        _s["messages"] = list(_saved)
        _s["total_tokens"] = ng_app.storage.user.get("chat_token_count", 0)
        _s["context_window"] = ng_app.storage.user.get("chat_context_window", 0)
        _s["current_ctx_tokens"] = _s["total_tokens"]
        # Restore conv_id so messages are linked to existing conversation, not new
        _stored_conv_id = ng_app.storage.user.get("chat_conv_id")
        if _stored_conv_id and _username:
            _known_ids = {c["id"] for c in _chat_hist.list_conversations(_username)}
            if _stored_conv_id in _known_ids:
                _s["conv_id"] = _stored_conv_id
        with messages_container:
            for _msg in _s["messages"]:
                _render_stored_msg(_msg)
        _update_context_progress()
        messages_scroll.scroll_to(percent=1.0)
    else:
        with messages_container:
            _welcome_msg()
    _refresh_pinned_chips()
    _refresh_history_list()

    # ── History drawer drag-resize ────────────────────────────────────────────
    ui.add_head_html("""<script>
(function initHistResize() {
    function setup() {
        var handle = document.querySelector('.chat-hist-resize');
        var drawer = document.querySelector('.chat-history-drawer');
        if (!handle || !drawer) { setTimeout(setup, 600); return; }
        var dragging = false;
        function setW(x) {
            var w = Math.max(180, Math.min(420, x));
            drawer.style.setProperty('--hist-w', w + 'px');
        }
        handle.addEventListener('mousedown', function(e) {
            dragging = true;
            handle.classList.add('dragging');
            document.body.style.cursor = 'col-resize';
            document.body.style.userSelect = 'none';
            e.preventDefault();
        });
        document.addEventListener('mousemove', function(e) {
            if (dragging) setW(e.clientX);
        });
        document.addEventListener('mouseup', function() {
            if (dragging) {
                dragging = false;
                handle.classList.remove('dragging');
                document.body.style.cursor = '';
                document.body.style.userSelect = '';
            }
        });
        handle.addEventListener('touchmove', function(e) {
            setW(e.touches[0].clientX);
            e.preventDefault();
        }, {passive: false});
    }
    setTimeout(setup, 400);
})();
</script>""")

    # ── Desktop: Suchergebnisse-Panel initial öffnen (Mobile bleibt zu) ───────
    try:
        await ui.context.client.connected()
        _is_desktop = await ui.run_javascript(
            "window.matchMedia('(min-width:768px)').matches", timeout=3.0
        )
        if _is_desktop:
            _set_docs_visible(True)
    except Exception:
        pass
