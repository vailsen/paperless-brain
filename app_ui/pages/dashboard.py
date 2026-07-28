# app_ui/pages/dashboard.py
import asyncio
import calendar
import hashlib
import html as _html
import json
import math
import os
import re
import socket
from datetime import date, datetime

import httpx
from nicegui import ui

from services.session_auth import get_session_token

from app_ui.cluster_dialog import create_cluster_dialog
from app_ui.document_dialog import create_document_dialog
from app_ui.layout import page_layout, require_auth
from config.extraction_rules import PROMPT_VERSION
from config.settings import settings
from i18n import get_translator
from models.result_document import DocumentResult
from pipelines.delete import delete_document
from pipelines.ingest import ingest_document
from pipelines.paperless_db_sync import check_sync_state
from services import sync_state
from services.clients import (
    chroma,
    cross_ref_index,
    get_session_paperless,
    paperless,
    sidecar_service,
    thumbnail_service,
    vision,
)
from services.credential_store import load_credentials, save_credentials
from services.ollama_watchdog import on_manual_shutdown, on_wol

# ── module-level sync state (persists across page navigations) ───────────────

_SYNC_LOG: list[str] = []

# ── helpers ──────────────────────────────────────────────────────────────────


def _ring_svg(value: int, total: int, size: int = 100) -> str:
    cx = cy = size / 2
    r = size / 2 - 10
    circumference = 2 * math.pi * r
    pct = (value / total) if total > 0 else 0
    filled = circumference * pct
    gap = circumference - filled

    if total > 0 and value < total * 0.5:
        color = "#f59e0b"
    elif total > 0 and value < total * 0.25:
        color = "#ef4444"
    else:
        color = "#a855f7"

    return (
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}">'
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="var(--c-border)" stroke-width="8"/>'
        f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{color}" stroke-width="8"'
        f' stroke-linecap="round"'
        f' stroke-dasharray="{filled:.1f} {gap:.1f}"'
        f' transform="rotate(-90 {cx} {cy})"/>'
        f"</svg>"
    )


_DOW_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def _month_html(year: int, month: int, day_data: dict[int, list[dict]]) -> str:
    """Render calendar month. day_data: {day: [{description, paperless_id}, ...]}"""
    cal = calendar.monthcalendar(year, month)
    today = date.today()
    header = "".join(
        f'<th style="width:32px;padding:4px 0;font-size:.7rem;color:var(--c-text-muted);">{d}</th>'
        for d in _DOW_DE
    )
    rows = []
    for week in cal:
        cells = []
        for day in week:
            if day == 0:
                cells.append("<td></td>")
                continue
            is_today = year == today.year and month == today.month and day == today.day
            actions = day_data.get(day, [])
            is_dl = bool(actions)

            if is_today and is_dl:
                bg = "background:#7c3aed;color:#fff;font-weight:700;"
            elif is_today:
                bg = "background:var(--c-border);color:var(--c-text);font-weight:600;"
            elif is_dl:
                bg = "background:#581c87;color:#e9d5ff;font-weight:600;"
            else:
                bg = "color:var(--c-text-muted);"

            extra = ""
            if is_dl:
                # tooltip: concatenate action descriptions
                tooltip_lines = []
                for a in actions[:5]:
                    desc = a.get("description", "")
                    if len(desc) > 60:
                        desc = desc[:57] + "…"
                    tooltip_lines.append(desc)
                if len(actions) > 5:
                    tooltip_lines.append(f"… +{len(actions) - 5} weitere")
                tooltip = _html.escape("\n".join(tooltip_lines))

                # first doc id for click handler
                first_id = actions[0].get("paperless_id")
                cursor = "cursor:pointer;"
                onclick = (
                    f' onclick="window.__openDocById({first_id})"' if first_id else ""
                )
                extra = f' title="{tooltip}"{onclick}'
                bg += cursor

            style = f"width:32px;height:28px;text-align:center;font-size:.75rem;border-radius:6px;{bg}"
            cells.append(f'<td style="{style}"{extra}>{day}</td>')
        rows.append(f"<tr>{''.join(cells)}</tr>")
    return (
        '<table style="border-collapse:collapse;width:100%;">'
        f"<thead><tr>{header}</tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def _load_actions() -> list[dict]:
    index_path = settings.app_path / settings.extraction_sidecar_path / "index.json"
    try:
        with open(index_path) as f:
            return json.load(f).get("actions", [])
    except (OSError, json.JSONDecodeError):
        return []


def _last_sync_label() -> str:
    index_path = settings.app_path / settings.extraction_sidecar_path / "index.json"
    try:
        mtime = os.path.getmtime(index_path)
        return datetime.fromtimestamp(mtime).strftime("%d.%m.%Y %H:%M")
    except OSError:
        return "—"


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _local_ollama_urls(username: str = "", token: str = "") -> list[str]:
    """Distinct base URLs of the user's local-lane models, in registry order.

    `lane` marks the concurrency lane, not the machine — it says "runs on my own
    GPU", not "runs on THIS host". So several local models may well point at
    different boxes, and the registry order is just the user's sort order in
    Settings. Callers that need one specific machine have to handle >1 entry.
    """
    if not (username and token):
        return []
    urls: list[str] = []
    try:
        from services.model_registry import get_models

        for mdl in get_models(username, token):
            if mdl.get("lane") != "local" or not mdl.get("base_url"):
                continue
            url = mdl["base_url"].rstrip("/")
            if url.endswith("/v1"):
                url = url[:-3]
            if url not in urls:
                urls.append(url)
    except Exception:
        pass
    return urls


def _resolve_ollama_server(username: str = "", token: str = "") -> str:
    """Return Ollama base URL: settings → first local-lane model → empty.

    Good enough for the reachability probe (any local host answering means the
    GPU is up). Power actions must use `_power_target()` instead, which refuses
    to guess between several hosts.
    """
    if settings.ollama_server:
        return settings.ollama_server
    urls = _local_ollama_urls(username, token)
    return urls[0] if urls else ""


def _power_hosts(username: str = "", token: str = "") -> list[str]:
    """Candidate hosts for wake/shutdown — exactly one means unambiguous.

    OLLAMA_SERVER is the explicit answer when set. Otherwise the local-lane
    models have to agree: sending a magic packet to the wrong subnet is harmless,
    but `sudo shutdown -h now` on the wrong machine is not, and the single global
    OLLAMA_HOST_LAN_MAC_ADDRESS_WOL / OLLAMA_SSH_USER already assume there is
    exactly one such box. Callers refuse to act on more than one.
    """
    if settings.ollama_server:
        ip = _ollama_host_ip(username, token)
        return [ip] if ip else []
    hosts: list[str] = []
    for url in _local_ollama_urls(username, token):
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)", url)
        if m and m.group(1) not in hosts:
            hosts.append(m.group(1))
    return hosts


def _wol_broadcast(username: str = "", token: str = "") -> str:
    srv = _resolve_ollama_server(username, token)
    m = re.search(r"(\d+\.\d+\.\d+)\.\d+", srv)
    return f"{m.group(1)}.255" if m else "255.255.255.255"


def _ollama_host_ip(username: str = "", token: str = "") -> str:
    srv = _resolve_ollama_server(username, token)
    m = re.search(r"(\d+\.\d+\.\d+\.\d+)", srv)
    return m.group(1) if m else ""


async def _fetch_paperless_count() -> int | None:
    try:
        async with httpx.AsyncClient(
            headers={"Authorization": f"Token {settings.paperless_superuser_token}"},
            timeout=5,
        ) as client:
            resp = await client.get(
                f"{settings.paperless_url.rstrip('/')}/api/documents/?page_size=1"
            )
            resp.raise_for_status()
            return resp.json().get("count", 0)
    except Exception:
        return None


async def _check_ollama(username: str = "", token: str = "") -> str | None:
    try:
        srv = _resolve_ollama_server(username, token)
        m = re.search(r"://([^:/]+)(?::(\d+))?", srv)
        if not m:
            return None
        host, port = m.group(1), int(m.group(2) or 11434)
        # asyncio.open_connection respects wait_for cancellation even on DROP firewalls;
        # httpx's connect timeout does not reliably fire against silent packet drops.
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=2.0
        )
        writer.close()
        await writer.wait_closed()
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{srv.rstrip('/')}/api/version")
            resp.raise_for_status()
            return resp.json().get("version", "ok")
    except Exception:
        return None


async def _check_paperless() -> bool:
    try:
        async with httpx.AsyncClient(
            headers={"Authorization": f"Token {settings.paperless_superuser_token}"},
            timeout=3,
        ) as client:
            resp = await client.get(f"{settings.paperless_url.rstrip('/')}/api/")
            return resp.status_code < 400
    except Exception:
        return False


async def _safe_chroma_count() -> int | None:
    try:

        def _count() -> int:
            results = chroma.collection.get(
                where={"chunk_index": {"$eq": 0}}, include=[]
            )
            return len(results["ids"])

        return await asyncio.wait_for(asyncio.to_thread(_count), timeout=10.0)
    except Exception:
        return None


# ── page ─────────────────────────────────────────────────────────────────────


@ui.page("/")
async def dashboard():
    if not require_auth():
        return
    page_layout()

    _ = get_translator()

    def _month_name(m: int) -> str:
        # Render-time month names (msgid = German). _MONTH_DE is import-time and
        # cannot be wrapped there; these literals are what pybabel extracts.
        return [
            _("January"), _("February"), _("March"), _("April"),
            _("May"), _("June"), _("July"), _("August"),
            _("September"), _("October"), _("November"), _("December"),
        ][m - 1]

    from nicegui import app as ng_app

    _username = ng_app.storage.user.get("paperless_user", "")
    _token = get_session_token()
    _creds = load_credentials(_username, _token) if _username and _token else {}
    _hidden_keys: set[str] = set(
        _creds.get("dashboard", {}).get("hidden_actions")
        or ng_app.storage.user.get("hidden_actions", [])
    )
    _show_hidden: list[bool] = [False]
    _render_deadline: list = [None]  # forward ref set in _refresh_status

    from app_ui.pages.browser import _render_card

    def _pinned_ids() -> set[int]:
        return set(ng_app.storage.user.get("pinned_doc_ids") or [])

    def _on_pin(result: DocumentResult) -> None:
        doc_id = result.document.id
        cache: list[dict] = list(ng_app.storage.user.get("pinned_docs_cache") or [])
        if doc_id in _pinned_ids():
            cache = [d for d in cache if d["id"] != doc_id]
            ui.notify(_("#{id} unpinned").format(id=doc_id), timeout=1500)
        else:
            cache.append({"id": doc_id, "title": result.document.title})
            ui.notify(_("#{id} pinned").format(id=doc_id), type="positive", timeout=1500)
        ng_app.storage.user["pinned_doc_ids"] = [d["id"] for d in cache]
        ng_app.storage.user["pinned_docs_cache"] = cache

    open_document, _doc_dialog = create_document_dialog(
        pin_fn=lambda r: _on_pin(r),
        get_pin_state_fn=lambda doc_id: doc_id in _pinned_ids(),
        open_cluster_fn=lambda doc_id: open_cluster(doc_id),
    )

    open_cluster = create_cluster_dialog(
        open_document_fn=open_document,
        pin_fn=lambda r: _on_pin(r),
        get_pinned_ids_fn=_pinned_ids,
        render_card_fn=_render_card,
    )

    ui.add_head_html("""<style>
    html, body { overflow: hidden !important; }
    .q-page { min-height: 0 !important; overflow: hidden !important; }
    .dash-card {
        background: var(--c-surface);
        border: 1px solid var(--c-border);
        border-radius: 12px;
        padding: 20px 24px;
        display: flex;
        flex-direction: column;
    }
    .deadline-row td { transition: background .1s; }
    .deadline-row:hover td { background: rgba(124,58,237,0.10) !important; }
    .doc-link {
        color: #7c3aed;
        cursor: pointer;
        text-decoration: none;
        font-family: monospace;
    }
    .doc-link:hover { color: #a78bfa; text-decoration: underline; }
    </style>""")

    with ui.element("div").style(
        "height:calc(100dvh - var(--q-header-height,52px)); overflow-y:auto; width:100%;"
    ):
        with ui.column().style(
            "width:100%; max-width:1200px; margin:0 auto; padding:24px 24px 40px; gap:20px;"
        ):
            ui.label(_("Dashboard")).classes("text-gray-100 text-2xl font-bold")

            # ── Hilfe-Bereich ─────────────────────────────────────────────────
            with (
                ui.expansion(_("Help — Module overview"), icon="help_outline")
                .props("dense dark")
                .classes("w-full text-gray-400 text-sm bg-gray-900 rounded-lg px-2")
            ):
                with (
                    ui.grid(columns=2)
                    .classes("w-full gap-4 mt-2 mb-1")
                    .style("align-items:start")
                ):
                    for _icon, _title, _desc in [
                        (
                            "chat",
                            _("Chat"),
                            _(
                                "AI assistant with access to your document archive, your memory, the web, email and calendar. Ask questions, have documents summarized, draft letters or run research. Pick the AI model you want at the top."
                            ),
                        ),
                        (
                            "auto_awesome",
                            _("Deep research"),
                            _(
                                "Autonomous multi-step agent for complex tasks. Enter a request → the AI plans it, breaks it into sub-tasks and carries them out on its own (document research, web search, email analysis, calculations). The result is presented for review — saved only after approval. Examples: 'Create an overview of all insurance policies', 'Analyze my utility bills from the last 3 years', 'Research current funding programs for my project'."
                            ),
                        ),
                        (
                            "folder_open",
                            _("Browser"),
                            _(
                                "Full-screen view of the document archive (Paperless-ngx). Search by full text or semantic similarity, filter by type, correspondent, tag or date. Open documents directly, pin important ones to the dashboard or cluster similar documents via vector search. Every document you reference in chat or deep research comes from this archive."
                            ),
                        ),
                        (
                            "psychology",
                            _("Memory"),
                            _(
                                "The assistant's long-term memory: stored facts about you, your contracts, vehicles and preferences. The chat reads this information automatically for matching requests. Use 'Dreaming' (dashboard) to have the memory cleaned up automatically."
                            ),
                        ),
                    ]:
                        with ui.row().classes("items-start gap-3"):
                            ui.icon(_icon, size="sm").classes(
                                "text-purple-400 flex-shrink-0"
                            ).style("align-self:flex-start;margin-top:1px")
                            with (
                                ui.column()
                                .classes("gap-0.5")
                                .style("align-self:flex-start")
                            ):
                                ui.label(_title).classes(
                                    "text-gray-200 font-semibold text-sm"
                                )
                                ui.label(_desc).classes(
                                    "text-gray-500 text-xs leading-snug"
                                )

            # ── top row: 3 stat cards (equal height via align-items:stretch) ────
            with ui.row().style(
                "width:100%; gap:20px; flex-wrap:wrap; align-items:stretch;"
            ):
                # 1) Sync-Status card
                with (
                    ui.element("div")
                    .classes("dash-card flex-1")
                    .style("min-width:260px;")
                ):
                    with ui.row().classes("items-center gap-4 w-full flex-1"):
                        ring_html = ui.html("").style("flex-shrink:0;")
                        with ui.column().style("gap:4px; flex:1;"):
                            with ui.row().classes("items-baseline gap-1"):
                                sync_value = ui.label("—").style(
                                    "font-size:1.75rem;font-weight:700;color:var(--c-text);"
                                )
                                ui.label(_("Documents")).classes("text-gray-400 text-sm")
                            sync_sub = ui.label(_("Loading...")).classes(
                                "text-gray-500 text-xs"
                            )
                            last_sync_label_el = ui.label(
                                _("Last sync: {ts}").format(ts=_last_sync_label())
                            ).classes("text-gray-600 text-xs")
                            outdated_label_el = ui.label("").classes("text-amber-400 text-xs")
                            outdated_label_el.set_visibility(False)
                    ui.separator().style("border-color:var(--c-surface); margin-top:12px; margin-bottom:8px;")
                    with ui.column().classes("gap-1 w-full"):
                        with ui.row().classes("items-center justify-between w-full"):
                            ui.label("PAPERLESS NGX").style(
                                "font-size:.6rem;font-weight:700;letter-spacing:.1em;"
                                "color:var(--c-text-muted);text-transform:uppercase;"
                            )
                            with ui.row().classes("items-center gap-0"):
                                sync_btn = (
                                    ui.button(icon="sync")
                                    .props("flat dense round size=sm")
                                    .classes("text-purple-400")
                                    .tooltip(_("Synchronize Paperless now"))
                                )
                                log_btn = (
                                    ui.button(icon="info")
                                    .props("flat dense round size=sm")
                                    .classes("text-gray-500")
                                    .tooltip(_("Sync log"))
                                )
                        with ui.row().classes("items-center justify-between w-full"):
                            ui.label("BRAIN VAULT").style(
                                "font-size:.6rem;font-weight:700;letter-spacing:.1em;"
                                "color:var(--c-text-muted);text-transform:uppercase;"
                            )
                            with ui.row().classes("items-center gap-0"):
                                vault_sync_btn = (
                                    ui.button(icon="cloud_sync")
                                    .props("flat dense round size=sm")
                                    .classes("text-teal-400")
                                    .tooltip(_("Synchronize vault now"))
                                )
                                dream_btn = (
                                    ui.button(icon="bedtime")
                                    .props("flat dense round size=sm")
                                    .classes("text-indigo-400")
                                    .tooltip(_("Analyze and clean up memory"))
                                )

                # 2) System-Status card — capture dot/badge refs
                _status_items: list[tuple] = []

                with (
                    ui.element("div")
                    .classes("dash-card flex-1")
                    .style("min-width:200px;")
                ):
                    ui.label(_("System status")).style(
                        "font-size:.7rem;font-weight:600;letter-spacing:.08em;"
                        "color:var(--c-text-muted);text-transform:uppercase;margin-bottom:12px;"
                    )
                    with ui.column().style("gap:8px; flex:1; justify-content:center;"):
                        ollama_dot = ollama_badge = wake_btn = stop_btn = None
                        for label_text, key in [
                            ("Paperless NGX", "paperless"),
                            ("ChromaDB", "chroma"),
                            (_("Ollama (Local LLM)"), "ollama"),
                        ]:
                            with ui.row().classes("items-center gap-2"):
                                dot = ui.element("div").style(
                                    "width:8px;height:8px;border-radius:50%;"
                                    "background:var(--c-border);flex-shrink:0;"
                                )
                                ui.label(label_text).classes(
                                    "text-gray-300 text-sm flex-1"
                                )
                                badge = ui.label("…").classes("text-gray-500 text-xs")
                                if key == "ollama":
                                    ollama_dot = dot
                                    ollama_badge = badge
                                    if settings.ollama_host_lan_mac_address_wol:
                                        wake_btn = (
                                            ui.button(icon="power")
                                            .props("flat dense round size=xs")
                                            .classes("text-green-500")
                                            .tooltip(_("Wake up (WoL)"))
                                        )
                                    if settings.ollama_ssh_user:
                                        stop_btn = (
                                            ui.button(icon="power_off")
                                            .props("flat dense round size=xs")
                                            .classes("text-red-400")
                                            .tooltip(_("Shut down (SSH)"))
                                        )
                            _status_items.append((dot, badge, key))

                        ui.separator().classes("my-1")

                        # E-Mail and Calendar config status (filled after cred load)
                        with ui.row().classes("items-center gap-2"):
                            imap_dot = ui.element("div").style(
                                "width:8px;height:8px;border-radius:50%;"
                                "background:var(--c-border);flex-shrink:0;"
                            )
                            ui.label(_("Email (IMAP)")).classes(
                                "text-gray-300 text-sm flex-1"
                            )
                            imap_badge = ui.label("…").classes("text-gray-500 text-xs")
                        with ui.row().classes("items-center gap-2"):
                            cal_dot = ui.element("div").style(
                                "width:8px;height:8px;border-radius:50%;"
                                "background:var(--c-border);flex-shrink:0;"
                            )
                            ui.label(_("Calendar")).classes("text-gray-300 text-sm flex-1")
                            cal_badge = ui.label("…").classes("text-gray-500 text-xs")

                # 3) Fristen overview card
                with (
                    ui.element("div")
                    .classes("dash-card flex-1")
                    .style("min-width:200px;")
                ):
                    ui.label(_("Deadlines")).style(
                        "font-size:.7rem;font-weight:600;letter-spacing:.08em;"
                        "color:var(--c-text-muted);text-transform:uppercase;margin-bottom:12px;"
                    )
                    with ui.column().style("gap:4px; flex:1; justify-content:center;"):
                        with ui.row().classes("items-end gap-3"):
                            deadline_count_label = ui.label("—").style(
                                "font-size:2.5rem;font-weight:700;color:#a855f7;"
                            )
                            with ui.column().style("gap:2px;margin-bottom:4px;"):
                                deadline_upcoming_label = ui.label("").classes(
                                    "text-gray-400 text-xs"
                                )
                                deadline_overdue_label = ui.label("").classes(
                                    "text-red-400 text-xs"
                                )

            # ── bottom: calendar + table ─────────────────────────────────────
            with ui.element("div").classes("dash-card").style("width:100%;"):
                with (
                    ui.row()
                    .classes("items-center justify-between w-full")
                    .style("margin-bottom:16px;")
                ):
                    ui.label(_("Calendar & deadlines")).style(
                        "font-size:.7rem;font-weight:600;letter-spacing:.08em;"
                        "color:var(--c-text-muted);text-transform:uppercase;"
                    )
                    with ui.row().classes("items-center gap-1"):
                        btn_prev = ui.button(icon="chevron_left").props(
                            "flat dark dense"
                        )
                        btn_next = ui.button(icon="chevron_right").props(
                            "flat dark dense"
                        )

                calendar_row = ui.row().style(
                    "gap:24px; flex-wrap:wrap; margin-bottom:24px;"
                )
                table_container = ui.element("div").style(
                    "width:100%; overflow-y:auto; max-height:55vh;"
                )

    # ── JS bridge (same pattern as chat.py) ──────────────────────────────────
    _doc_handler = ui.element("div").style("display:none;")

    async def _handle_doc_open(e) -> None:
        try:
            args = e.args
            if isinstance(args, (list, tuple)):
                args = args[0] if args else None
            doc_id = int(args)
            doc = await get_session_paperless().get_document(doc_id)
            await open_document(DocumentResult(document=doc))
        except (TypeError, ValueError, Exception):
            pass

    _doc_handler.on("docOpen", _handle_doc_open)
    _doc_listener_id = list(_doc_handler._event_listeners.keys())[0]

    ui.add_head_html(f"""<script>
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
</script>""")

    # ── JS bridge: hide/unhide action rows ───────────────────────────────────
    _hide_handler = ui.element("div").style("display:none;")

    async def _handle_hide_action(e) -> None:
        try:
            args = e.args
            if isinstance(args, (list, tuple)):
                args = args[0] if args else None
            key = str(args or "")
            if key in _hidden_keys:
                _hidden_keys.discard(key)
            else:
                _hidden_keys.add(key)
            ng_app.storage.user["hidden_actions"] = list(_hidden_keys)
            if _username and _token:
                _nc = load_credentials(_username, _token)
                _nc.setdefault("dashboard", {})["hidden_actions"] = list(_hidden_keys)
                await asyncio.to_thread(save_credentials, _username, _token, _nc)
            if _render_deadline[0]:
                _render_deadline[0]()
        except Exception:
            pass

    _hide_handler.on("hideAction", _handle_hide_action)
    _hide_listener_id = list(_hide_handler._event_listeners.keys())[0]

    ui.add_head_html(f"""<script>
window.__toggleHideAction = function(key) {{
    if (!window.socket || !window.did_handshake) {{
        setTimeout(function() {{ window.__toggleHideAction(key); }}, 100);
        return;
    }}
    window.socket.emit('event', {{
        id: {_hide_handler.id},
        client_id: window.clientId,
        listener_id: '{_hide_listener_id}',
        args: [JSON.stringify(key)]
    }});
}};
</script>""")

    # ── sync log dialog ───────────────────────────────────────────────────────
    with (
        ui.dialog() as log_dialog,
        ui.card().style(
            "width:min(720px,95vw);max-width:95vw;"
            "background:var(--c-bg);border:1px solid var(--c-border);padding:16px;"
        ),
    ):
        with ui.row().classes("items-center justify-between w-full mb-2"):
            ui.label(_("Sync log")).classes("text-gray-100 font-semibold")
            ui.button(icon="close", on_click=log_dialog.close).props(
                "flat round dense"
            ).classes("text-gray-400")
        log_scroll = ui.scroll_area().style("height:380px;width:100%;max-width:100%;overflow-x:hidden;")
        with log_scroll:
            log_column = ui.column().classes("gap-0 p-1 w-full").style("max-width:100%;")

    def _refresh_outdated() -> None:
        try:
            n = len(sidecar_service.outdated_ids(PROMPT_VERSION))
        except Exception:
            n = 0
        try:
            if n > 0:
                outdated_label_el.set_text(
                    _("⚠ {n} docs with outdated prompt version (current v{v})").format(
                        n=n, v=PROMPT_VERSION
                    )
                )
                outdated_label_el.set_visibility(True)
            else:
                outdated_label_el.set_visibility(False)
        except RuntimeError:
            pass

    async def do_sync(reingest_limit: int | None = None) -> None:
        if sync_state.is_running[0]:
            ui.notify(_("Sync already running…"), type="warning")
            return
        sync_state.is_running[0] = True
        _SYNC_LOG.clear()
        sync_btn.props("loading")
        vault_sync_btn.disable()
        log_column.clear()

        def _log(msg: str) -> None:
            ts = datetime.now().strftime("%H:%M:%S")
            entry = f"[{ts}] {msg}"
            _SYNC_LOG.append(entry)
            try:
                with log_column:
                    ui.label(entry).classes(
                        "text-xs font-mono text-gray-300 leading-5 w-full"
                    ).style("white-space:pre-wrap; word-break:break-word;")
                log_scroll.scroll_to(percent=1.0)
            except RuntimeError:
                pass  # client navigated away; entry still in _SYNC_LOG

        def _notify(msg: str, **kwargs) -> None:
            try:
                ui.notify(msg, **kwargs)
            except RuntimeError:
                pass

        _log("Sync gestartet…")
        try:
            state = await check_sync_state(paperless, chroma)
            _log(
                f"Status: {len(state.new_ids)} neu · "
                f"{len(state.deleted_ids)} gelöscht · "
                f"{len(state.current_ids)} aktuell"
            )

            batch = state.new_ids
            for i, doc_id in enumerate(batch, 1):
                _log(f"Ingest #{doc_id} ({i}/{len(batch)})…")
                try:
                    await ingest_document(
                        doc_id,
                        paperless,
                        chroma,
                        vision,
                        sidecar_service,
                        thumbnail_service,
                    )
                    _log(f"  ✓ #{doc_id}")
                    _notify(_("✓ #{id} imported").format(id=doc_id), type="positive", timeout=2000)
                except Exception as exc:
                    _log(f"  ✗ #{doc_id}: {exc}")
                    _notify(_("✗ #{id} error").format(id=doc_id), type="negative", timeout=3000)
                await asyncio.sleep(0)

            for doc_id in state.deleted_ids:
                _log(f"Lösche #{doc_id}…")
                try:
                    await delete_document(
                        doc_id, chroma, sidecar_service, thumbnail_service
                    )
                    _log(f"  ✓ #{doc_id} gelöscht")
                    _notify(_("#{id} removed").format(id=doc_id), timeout=2000)
                except Exception as exc:
                    _log(f"  ✗ #{doc_id}: {exc}")
                await asyncio.sleep(0)

            # Re-ingest documents whose sidecar has an old prompt version.
            # Capped at reingest_limit (None = all) — re-ingesting the whole
            # backlog can take very long.
            outdated = sidecar_service.outdated_ids(PROMPT_VERSION)
            if outdated:
                to_redo = outdated if reingest_limit is None else outdated[:reingest_limit]
                _log(
                    f"Veraltet: {len(outdated)} · neu einlesen: {len(to_redo)}"
                    + ("" if reingest_limit is None else f" (Limit {reingest_limit})")
                )
                for j, doc_id in enumerate(to_redo, 1):
                    _log(f"Neu einlesen #{doc_id} ({j}/{len(to_redo)})…")
                    try:
                        await delete_document(
                            doc_id, chroma, sidecar_service, thumbnail_service
                        )
                        await ingest_document(
                            doc_id, paperless, chroma, vision,
                            sidecar_service, thumbnail_service,
                        )
                        _log(f"  ✓ #{doc_id}")
                    except Exception as exc:
                        _log(f"  ✗ #{doc_id}: {exc}")
                    await asyncio.sleep(0)

            # Final step: LLM review of all extracted actions/deadlines.
            # Drops content-free entries and cross-document duplicates of the
            # same real deadline before the index is rebuilt. Verdicts persist
            # (action_review.json) — only new actions hit the LLM.
            try:
                _review_creds = (
                    load_credentials(_username, _token) if _username and _token else {}
                )
                _review_model = _review_creds.get("dream_model", "")
                if _review_model:
                    from services.action_review import collect_actions, review_actions
                    from werkbank.llm_lane import create_llm as _mk_llm

                    _acts = collect_actions(sidecar_service.extr_path)
                    _log(f"Prüfe Fristen/Aktionen ({len(_acts)} gesamt)…")
                    _n_rev, _n_drop = await review_actions(
                        sidecar_service.extr_path,
                        _acts,
                        _mk_llm(_review_model, _username, _token),
                    )
                    _log(f"  ✓ {_n_rev} neu geprüft · {_n_drop} verworfen")
                else:
                    _log(
                        "Fristen-Review übersprungen (kein Modell unter "
                        "Einstellungen > Gedächtnis-Pflege konfiguriert)"
                    )
            except Exception as exc:
                _log(f"  ✗ Fristen-Review fehlgeschlagen: {exc}")

            sidecar_service.create_index_file()
            cross_ref_index.rebuild(
                str(settings.app_path / settings.extraction_sidecar_path)
            )
            _log("Index aktualisiert")
            _refresh_outdated()
            _log("✅ Sync abgeschlossen")
            _notify(_("Sync complete"), type="positive")

            new_p = await _fetch_paperless_count()
            new_c = await _safe_chroma_count()
            p_val = new_p or 0
            c_val = new_c or 0
            try:
                last_sync_label_el.set_text(f"Letzter Sync: {_last_sync_label()}")
                ring_html.set_content(_ring_svg(c_val, p_val))
                if new_p:
                    pct = int(c_val / p_val * 100) if p_val > 0 else 0
                    sync_value.set_text(f"{c_val} / {p_val}")
                    sync_sub.set_text(_("{pct} % indexed in ChromaDB").format(pct=pct))
            except RuntimeError:
                pass
        except Exception as exc:
            _log(f"❌ Fehler: {exc}")
            _notify(_("Sync error: {err}").format(err=exc), type="negative")
        finally:
            sync_state.is_running[0] = False
            try:
                sync_btn.props(remove="loading")
                vault_sync_btn.enable()
            except RuntimeError:
                pass

    # Sync dialog: ask how many outdated docs to re-ingest (empty = all)
    with ui.dialog() as _sync_dlg, ui.card().style(
        "background:var(--c-surface); width:min(400px,92vw);"
    ):
        ui.label(_("Sync")).classes("text-base font-semibold text-gray-100")
        ui.label(
            _(
                "New documents are always ingested. Additionally, outdated documents (old prompt version) are re-ingested — up to how many? Empty = all. Large numbers can take a long time."
            )
        ).classes("text-xs text-gray-400 mt-1")
        _sync_limit_num = (
            ui.number(_("Max. re-ingests (empty = all)"), min=1, step=1, format="%d")
            .props("outlined dark dense")
            .classes("w-full mt-2")
        )
        with ui.row().classes("justify-end gap-2 mt-3 w-full"):
            ui.button(_("Cancel"), on_click=_sync_dlg.close).props(
                "flat dark dense"
            ).classes("text-gray-400")

            async def _start_sync() -> None:
                v = _sync_limit_num.value
                limit = int(v) if v else None
                _sync_dlg.close()
                await do_sync(limit)

            ui.button(_("Start"), icon="sync", on_click=_start_sync).props(
                "unelevated dark"
            ).classes("text-purple-300")

    sync_btn.on_click(_sync_dlg.open)
    log_btn.on_click(log_dialog.open)

    async def dream_brain() -> None:
        from services.brain_cleanup import apply as _cleanup_apply
        from services.brain_cleanup import run as _cleanup_run
        from services.clients import brain as _brain_svc
        from services.credential_store import load_credentials as _lc
        from werkbank.llm_lane import create_llm as _create_llm

        # Resolve dream model
        _creds = _lc(_username, _token) if _username and _token else {}
        _dream_model_name = _creds.get("dream_model", "")
        if not _dream_model_name:
            ui.notify(
                _("No model configured for dreaming. Please set one in Settings > Memory maintenance."),
                type="warning",
            )
            return

        dream_btn.props(add="loading")
        try:
            facts = await _brain_svc.get_all(_username)
            if not facts:
                ui.notify(_("No memory entries available."), type="info")
                return

            llm = _create_llm(_dream_model_name, _username, _token)
            actions, _llm_raw = await _cleanup_run(facts, llm=llm)
        except Exception as exc:
            _err_text = str(exc)
            with ui.dialog() as _err_dlg:
                with (
                    ui.card()
                    .classes("bg-gray-900")
                    .style("width:min(95vw,640px);max-height:80vh;overflow-y:auto;")
                ):
                    ui.label(_("Error during memory analysis")).classes(
                        "text-base font-semibold text-red-400 mb-2"
                    )
                    ui.label(_err_text).classes(
                        "text-xs text-gray-300 whitespace-pre-wrap font-mono"
                    )
                    ui.button(_("Close"), on_click=_err_dlg.close).props(
                        "flat dark dense"
                    ).classes("text-gray-400 mt-3")
            _err_dlg.open()
            return
        finally:
            dream_btn.props(remove="loading")

        if not actions:
            with ui.dialog() as _clean_dlg:
                with (
                    ui.card()
                    .classes("bg-gray-900")
                    .style("width:min(95vw,560px);max-height:75vh;overflow-y:auto;")
                ):
                    ui.label(_("Memory analysis")).classes(
                        "text-base font-semibold text-gray-100 mb-2"
                    )
                    ui.label(_("No actions suggested.")).classes(
                        "text-sm text-green-400 mb-3"
                    )
                    with ui.expansion(_("LLM response (debug)")).classes(
                        "w-full text-gray-500"
                    ):
                        ui.label(_llm_raw or _("(empty)")).classes(
                            "text-xs text-gray-400 whitespace-pre-wrap font-mono"
                        )
                    ui.button(_("Close"), on_click=_clean_dlg.close).props(
                        "flat dark dense"
                    ).classes("text-gray-400 mt-3")
            _clean_dlg.open()
            return

        # ── Review dialog ─────────────────────────────────────────────────
        with ui.dialog().props("persistent") as _dream_dlg:
            with (
                ui.card()
                .classes("bg-gray-900")
                .style("width:min(95vw,680px);max-height:85vh;overflow-y:auto;")
            ):
                with ui.row().classes("items-center justify-between w-full mb-3"):
                    with ui.column().classes("gap-0"):
                        ui.label(_("Memory cleanup")).classes(
                            "text-base font-semibold text-gray-100"
                        )
                        ui.label(
                            _("{n} actions suggested — deselect to skip").format(n=len(actions))
                        ).classes("text-xs text-gray-500")
                    ui.button(icon="close", on_click=_dream_dlg.close).props(
                        "flat dark dense"
                    ).classes("text-gray-400")

                ui.separator().classes("mb-3")

                _action_checks: list = []
                for a in actions:
                    with (
                        ui.card()
                        .classes("w-full mb-2")
                        .style("background:var(--c-bg);padding:10px;")
                    ):
                        with ui.row().classes("items-start gap-3 w-full"):
                            cb = ui.checkbox(value=True)
                            _action_checks.append((cb, a))
                            with ui.column().classes("flex-1 gap-1 min-w-0"):
                                _action_cfg = {
                                    "delete": (_("🗑 Delete"), "text-red-400"),
                                    "update": (_("✏️ Update"), "text-blue-400"),
                                    "update_tags": (
                                        _("🏷️ Update tags"),
                                        "text-indigo-400",
                                    ),
                                }
                                _alabel, _acolor = _action_cfg.get(
                                    a.action, ("?", "text-gray-400")
                                )
                                with ui.row().classes("items-center gap-2"):
                                    ui.label(_alabel).classes(
                                        f"text-xs font-semibold {_acolor}"
                                    )
                                    ui.label(getattr(a, "fact_idx", "")).classes(
                                        "text-xs font-mono text-gray-600"
                                    )
                                ui.label(a.reason).classes(
                                    "text-xs text-gray-400 italic"
                                )
                                ui.label(
                                    _("Current: {text}").format(
                                        text=f"{a.original_text[:200]}{'…' if len(a.original_text) > 200 else ''}"
                                    )
                                ).classes("text-xs text-gray-300").style(
                                    "word-break:break-word"
                                )
                                if a.action == "update" and a.new_text:
                                    ui.label(_("New: {text}").format(text=a.new_text)).classes(
                                        "text-xs text-green-400"
                                    ).style("word-break:break-word")
                                if a.action == "update_tags":
                                    _old_tags = (
                                        ", ".join(
                                            getattr(a, "original_tags", None) or []
                                        )
                                        or "–"
                                    )
                                    _new_tags = ", ".join(a.new_tags or []) or "–"
                                    ui.label(_("Tags old: {tags}").format(tags=_old_tags)).classes(
                                        "text-xs text-gray-500"
                                    )
                                    ui.label(_("Tags new: {tags}").format(tags=_new_tags)).classes(
                                        "text-xs text-green-400"
                                    )

                async def _apply_selected():
                    for cb, a in _action_checks:
                        a.selected = cb.value
                    selected = [a for _, a in _action_checks if a.selected]
                    if not selected:
                        ui.notify(_("No actions selected."), type="info")
                        _dream_dlg.close()
                        return
                    try:
                        deleted, updated = await _cleanup_apply(
                            selected, brain=_brain_svc
                        )
                        ui.notify(
                            _("Cleanup complete: {d} deleted, {u} updated.").format(
                                d=deleted, u=updated
                            ),
                            type="positive",
                        )
                    except Exception as exc:
                        ui.notify(_("Error: {err}").format(err=exc), type="negative")
                    _dream_dlg.close()

                ui.separator().classes("my-3")
                with ui.row().classes("justify-end gap-2"):
                    ui.button(_("Cancel"), on_click=_dream_dlg.close).props(
                        "flat dark"
                    ).classes("text-gray-400")
                    ui.button(
                        _("Run selected"),
                        icon="auto_fix_high",
                        on_click=_apply_selected,
                    ).props("unelevated dark").classes("bg-indigo-700 text-white")

        _dream_dlg.open()

    dream_btn.on_click(dream_brain)

    async def vault_sync_now() -> None:
        if not _username:
            ui.notify(_("No user signed in."), type="warning")
            return
        vault_sync_btn.props(add="loading")
        sync_btn.disable()
        _sync_note = ui.notification(
            _("📂 Synchronizing vault…"),
            spinner=True,
            timeout=None,
            position="bottom-right",
            type="ongoing",
        )
        try:
            from vault.sync import sync_user as _vault_sync
            await _vault_sync(_username, force=True)
            _sync_note.dismiss()
            ui.notify(_("Vault synchronized."), type="positive", timeout=2000)
        except Exception as exc:
            _sync_note.dismiss()
            ui.notify(_("Vault sync error: {err}").format(err=exc), type="negative")
        finally:
            vault_sync_btn.props(remove="loading")
            sync_btn.enable()

    vault_sync_btn.on_click(vault_sync_now)

    # Restore sync state when navigating back to this page
    if sync_state.is_running[0]:
        sync_btn.props(add="loading")

        async def _watch_sync_done() -> None:
            if not sync_state.is_running[0]:
                sync_btn.props(remove="loading")
                _sync_watch_timer.active = False

        _sync_watch_timer = ui.timer(0.5, _watch_sync_done)
    for entry in _SYNC_LOG:
        with log_column:
            ui.label(entry).classes("text-xs font-mono text-gray-300 leading-5")

    # ── WoL / SSH shutdown ────────────────────────────────────────────────────
    _poll_timer: list = [None]

    def _ambiguous_power_target() -> str:
        """Warning text when the power buttons cannot tell which machine to hit."""
        hosts = _power_hosts(_username, _token)
        if len(hosts) < 2:
            return ""
        return _(
            "Several local models point at different hosts ({hosts}). Set "
            "OLLAMA_SERVER in .env to say which machine the power buttons control."
        ).format(hosts=", ".join(hosts))

    async def wake_ollama() -> None:
        mac = settings.ollama_host_lan_mac_address_wol
        if not mac:
            ui.notify(
                _("OLLAMA_HOST_LAN_MAC_ADDRESS_WOL not configured"), type="warning"
            )
            return
        # Waking the wrong subnet costs nothing, so warn and carry on.
        if warn := _ambiguous_power_target():
            ui.notify(warn, type="warning", timeout=10000)
        broadcast = _wol_broadcast(_username, _token)
        try:
            mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
            magic = b"\xff" * 6 + mac_bytes * 16
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.sendto(magic, (broadcast, 9))
        except Exception as exc:
            ui.notify(_("WoL error: {err}").format(err=exc), type="negative")
            return

        on_wol(_ollama_host_ip(_username, _token))
        ui.notify(_("Magic packet → {target}").format(target=broadcast), type="positive")
        ollama_badge.set_text(_("Waking up…"))
        ollama_dot.style(
            "width:8px;height:8px;border-radius:50%;flex-shrink:0;background:#f59e0b;"
        )
        wake_btn.disable()

        if _poll_timer[0]:
            _poll_timer[0].active = False

        async def _poll() -> None:
            ver = await _check_ollama(_username, _token)
            if ver:
                ollama_dot.style(
                    "width:8px;height:8px;border-radius:50%;flex-shrink:0;background:#22c55e;"
                )
                ollama_badge.set_text(ver)
                wake_btn.enable()
                _poll_timer[0].active = False
                _poll_timer[0] = None
                ui.notify(_("Ollama is online!"), type="positive")

        _poll_timer[0] = ui.timer(5.0, _poll)

    async def _run_shutdown() -> None:
        ssh_user = settings.ollama_ssh_user
        # Powering off the wrong machine is not recoverable from here — refuse
        # rather than pick whichever local model happens to sort first.
        if warn := _ambiguous_power_target():
            ui.notify(warn, type="negative", timeout=15000)
            return
        host = _ollama_host_ip(_username, _token)
        if not ssh_user or not host:
            ui.notify(_("OLLAMA_SSH_USER not configured"), type="warning")
            return

        ollama_badge.set_text(_("Shutting down…"))
        try:
            proc = await asyncio.create_subprocess_exec(
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "ConnectTimeout=5",
                "-o",
                "BatchMode=yes",  # fail instead of hanging on a password prompt
                f"{ssh_user}@{host}",
                "sudo shutdown -h now",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, err = await asyncio.wait_for(proc.communicate(), timeout=8.0)
            except asyncio.TimeoutError:
                err = b""  # SSH may drop before returning exit code — treat as success
            else:
                # A fast non-zero exit means the command never ran: no key in the
                # container, wrong user, sudo asking for a password. Say so —
                # reporting "shutting down" here is how this failure stays silent.
                if proc.returncode:
                    detail = (err or b"").decode(errors="replace").strip().splitlines()
                    ui.notify(
                        _("Shutdown failed: {err}").format(
                            err=detail[-1] if detail else _("ssh exited with {code}").format(code=proc.returncode)
                        ),
                        type="negative",
                        timeout=10000,
                    )
                    ollama_badge.set_text(_("Online"))
                    return
        except Exception as exc:
            ui.notify(_("SSH error: {err}").format(err=exc), type="negative")
            ollama_badge.set_text(_("Online"))
            return

        if _poll_timer[0]:
            _poll_timer[0].active = False
            _poll_timer[0] = None
        ollama_dot.style(
            "width:8px;height:8px;border-radius:50%;flex-shrink:0;background:#ef4444;"
        )
        on_manual_shutdown()
        ollama_badge.set_text(_("Offline"))
        if wake_btn is not None:
            wake_btn.enable()
        ui.notify(_("Server is shutting down…"), type="info")

    # Password-protected shutdown dialog
    with ui.dialog() as _shutdown_pw_dlg:
        with ui.card().style("background:var(--c-surface); min-width:320px;"):
            ui.label(_("Shut down server")).classes("text-gray-100 font-semibold mb-2")
            _pw_input = (
                ui.input(_("Password"), password=True, password_toggle_button=True)
                .props("outlined dark dense")
                .classes("w-full")
            )
            _pw_err = ui.label(_("Wrong password")).classes("text-red-400 text-xs mt-1")
            _pw_err.set_visibility(False)
            with ui.row().classes("justify-end gap-2 mt-3"):
                ui.button(_("Cancel"), on_click=_shutdown_pw_dlg.close).props(
                    "flat dark dense"
                ).classes("text-gray-400")

                async def _confirm_shutdown() -> None:
                    if _pw_input.value != settings.shutdown_password:
                        _pw_err.set_visibility(True)
                        return
                    _shutdown_pw_dlg.close()
                    await _run_shutdown()

                ui.button(_("Shut down"), on_click=_confirm_shutdown).props(
                    "dark dense"
                ).classes("text-red-400")

    async def stop_ollama() -> None:
        if settings.shutdown_password:
            _pw_input.set_value("")
            _pw_err.set_visibility(False)
            _shutdown_pw_dlg.open()
        else:
            await _run_shutdown()

    if wake_btn is not None:
        wake_btn.on_click(wake_ollama)
    if stop_btn is not None:
        stop_btn.on_click(stop_ollama)

    # ── async data load ───────────────────────────────────────────────────────
    paperless_count, chroma_count, ollama_ver, paperless_ok = await asyncio.gather(
        _fetch_paperless_count(),
        _safe_chroma_count(),
        _check_ollama(_username, _token),
        _check_paperless(),
    )

    # sync ring
    p = paperless_count or 0
    c = chroma_count or 0
    ring_html.set_content(_ring_svg(c, p))
    if paperless_count is not None:
        pct = int(c / p * 100) if p > 0 else 0
        sync_value.set_text(f"{c} / {p}")
        sync_sub.set_text(_("{pct} % indexed in ChromaDB").format(pct=pct))
    else:
        sync_value.set_text(str(c))
        sync_sub.set_text(_("Paperless not reachable"))

    _refresh_outdated()

    # status indicators
    status_data = {
        "paperless": (paperless_ok, _("Online") if paperless_ok else _("Offline")),
        "chroma": (
            chroma_count is not None,
            _("{n} docs").format(n=chroma_count) if chroma_count is not None else _("Error"),
        ),
        "ollama": (ollama_ver is not None, ollama_ver or _("Offline")),
    }
    for dot_el, badge_el, key in _status_items:
        ok, detail = status_data[key]
        dot_el.style(
            f"width:8px;height:8px;border-radius:50%;flex-shrink:0;"
            f"background:{'#22c55e' if ok else '#ef4444'};"
        )
        badge_el.set_text(detail)

    # E-Mail / Calendar config status (no live test — just check if configured)
    _creds = load_credentials(_username, _token) if _username and _token else {}

    _imap_cfg = _creds.get("imap", {})
    _imap_ok = bool(
        _imap_cfg.get("host")
        and _imap_cfg.get("username")
        and _imap_cfg.get("password")
    )
    imap_dot.style(
        f"width:8px;height:8px;border-radius:50%;flex-shrink:0;"
        f"background:{'#22c55e' if _imap_ok else 'var(--c-border)'};"
    )
    imap_badge.set_text(
        _imap_cfg.get("host", _("Not configured"))
        if _imap_ok
        else _("Not configured")
    )

    _cal_cfg = _creds.get("calendar", {})
    _ical_urls = _cal_cfg.get("ical_urls") or (
        [_cal_cfg["ical_url"]] if _cal_cfg.get("ical_url") else []
    )
    if _ical_urls:
        _cal_ok = True
        _cal_detail = _("iCal ({n} calendars)").format(n=len(_ical_urls))
    elif _cal_cfg.get("url") and _cal_cfg.get("username"):
        _cal_ok, _cal_detail = True, "CalDAV"
    else:
        _cal_ok, _cal_detail = False, _("Not configured")
    cal_dot.style(
        f"width:8px;height:8px;border-radius:50%;flex-shrink:0;"
        f"background:{'#22c55e' if _cal_ok else 'var(--c-border)'};"
    )
    cal_badge.set_text(_cal_detail)

    # deadlines — filtered to docs accessible by the current user. index.json is
    # built by the superuser sync and holds every owner's actions, so this filter
    # is the only thing standing between the user and other people's document
    # content. It fails closed: no token or an API error hides the deadlines.
    actions = _load_actions()
    if actions:
        if _token:
            from services.sidecar_service import filter_visible_actions

            actions = await filter_visible_actions(actions, get_session_paperless())
        else:
            actions = []

    # Merge the user's manual due-dates (kind=deadline brain notes)
    if _username:
        try:
            from services.clients import brain
            for _dl in await brain.get_deadlines(_username):
                actions.append({
                    "paperless_id": None,
                    "deadline": _dl.due,
                    "description": _dl.text,
                    "deadline_certain": True,
                    "manual": True,
                })
        except Exception:
            pass

    today = date.today()
    dated = [a for a in actions if _parse_date(a.get("deadline")) is not None]
    dated.sort(key=lambda a: _parse_date(a["deadline"]))
    overdue = list(reversed([a for a in dated if _parse_date(a["deadline"]) < today]))
    future = list(reversed([a for a in dated if _parse_date(a["deadline"]) >= today]))

    deadline_count_label.set_text(str(len(future)))
    deadline_upcoming_label.set_text(_("upcoming"))
    if overdue:
        deadline_overdue_label.set_text(_("{n} overdue").format(n=len(overdue)))

    # build per-day action data for calendar
    deadline_by_ym: dict[tuple, dict[int, list[dict]]] = {}
    for a in dated:
        d = _parse_date(a["deadline"])
        if d:
            ym = (d.year, d.month)
            deadline_by_ym.setdefault(ym, {}).setdefault(d.day, []).append(a)

    # calendar rendering (offset-aware, called on nav too)
    _cal_offset = [0]

    def render_calendar() -> None:
        calendar_row.clear()
        with calendar_row:
            for delta in range(3):
                raw_m = today.month + delta + _cal_offset[0]
                y = today.year + (raw_m - 1) // 12
                m = ((raw_m - 1) % 12) + 1
                with ui.element("div").style("flex:1; min-width:220px;"):
                    ui.label(f"{_month_name(m)} {y}").style(
                        "font-size:.8rem;font-weight:600;color:var(--c-text-2);margin-bottom:8px;"
                    )
                    ui.html(
                        _month_html(y, m, deadline_by_ym.get((y, m), {})),
                        sanitize=False,
                    )

    def _nav(delta: int):
        _cal_offset[0] += delta
        render_calendar()

    btn_prev.on_click(lambda: _nav(-3))
    btn_next.on_click(lambda: _nav(3))

    render_calendar()

    # deadline table — future first (asc), then overdue (desc, most recent first)
    all_rows = future + overdue
    _PAGE_SIZE = 100
    _dl_page = [0]
    th_style = (
        "font-size:.65rem;color:var(--c-text-muted);text-align:left;"
        "text-transform:uppercase;letter-spacing:.06em;padding:6px 10px;"
    )

    def _action_key(a: dict) -> str:
        raw = f"{a.get('paperless_id', '')}|{a.get('deadline', '')}|{a.get('description', '')}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    def render_deadline_table() -> None:
        visible_rows = [a for a in all_rows if _action_key(a) not in _hidden_keys]
        hidden_count = len(all_rows) - len(visible_rows)
        display_rows = all_rows if _show_hidden[0] else visible_rows

        page = _dl_page[0]
        start = page * _PAGE_SIZE
        end = start + _PAGE_SIZE
        page_rows = display_rows[start:end]
        total_pages = max(1, (len(display_rows) + _PAGE_SIZE - 1) // _PAGE_SIZE)
        if page >= total_pages:
            _dl_page[0] = 0
            page = 0
            start = 0
            end = _PAGE_SIZE
            page_rows = display_rows[:_PAGE_SIZE]

        table_container.clear()
        with table_container:
            if not all_rows:
                ui.label(_("No deadlines found.")).classes("text-gray-500 text-sm p-2")
                return

            # hidden-row control strip
            if hidden_count or _show_hidden[0]:
                with (
                    ui.row()
                    .classes("items-center gap-2 px-3 py-1")
                    .style("background:var(--c-surface-2); border-bottom:1px solid var(--c-surface);")
                ):
                    ui.icon("visibility_off", size="xs").classes("text-gray-600")
                    ui.label(_("{n} hidden").format(n=hidden_count)).classes(
                        "text-xs text-gray-600 flex-1"
                    )
                    lbl = _("Show") if not _show_hidden[0] else _("Hide")

                    def _toggle_show() -> None:
                        _show_hidden[0] = not _show_hidden[0]
                        render_deadline_table()

                    ui.button(lbl, on_click=_toggle_show).props(
                        "flat dark dense"
                    ).classes("text-xs text-gray-400")

            rows_html = ""
            future_visible = [
                a
                for a in future
                if _action_key(a) not in _hidden_keys or _show_hidden[0]
            ]
            for i, a in enumerate(page_rows):
                global_i = start + i
                key = _action_key(a)
                is_hidden = key in _hidden_keys
                d = _parse_date(a["deadline"])
                is_past = d and d < today
                is_sep = global_i == len(future_visible) and is_past
                date_str = d.strftime("%d.%m.%Y") if d else "—"
                date_color = (
                    "var(--c-text-muted)" if is_hidden else ("#ef4444" if is_past else "var(--c-text)")
                )
                certain = "●" if a.get("deadline_certain") else "○"
                certain_title = _("Certain") if a.get("deadline_certain") else _("Uncertain")
                desc = a.get("description", "")
                if len(desc) > 90:
                    desc = desc[:87] + "…"
                doc_id = a.get("paperless_id", "")
                doc_cell = (
                    f'<a class="doc-link" href="#" '
                    f'onclick="event.preventDefault();window.__openDocById({doc_id})">#{doc_id}</a>'
                    if doc_id
                    else "—"
                )
                tr_style = (
                    "border-top:2px solid var(--c-border);"
                    if is_sep
                    else "border-top:1px solid var(--c-surface);"
                )
                hide_icon = "visibility" if is_hidden else "visibility_off"
                hide_title = _("Show again") if is_hidden else _("Hide")
                hide_btn = (
                    f"<button onclick=\"event.stopPropagation();window.__toggleHideAction('{key}')\" "
                    f'title="{hide_title}" '
                    f'style="background:none;border:none;cursor:pointer;padding:2px 4px;'
                    f'pointer-events:auto;line-height:1;vertical-align:middle;">'
                    f'<span class="material-icons" style="font-size:14px;color:var(--c-border-strong);'
                    f'pointer-events:none;">{hide_icon}</span></button>'
                )
                row_opacity = "opacity:.45;" if is_hidden else ""
                rows_html += (
                    f'<tr class="deadline-row" style="{tr_style}{row_opacity}">'
                    f'<td style="padding:7px 10px;font-size:.75rem;color:{date_color};'
                    f'white-space:nowrap;font-family:monospace;">{date_str}</td>'
                    f'<td style="padding:7px 4px;font-size:.7rem;color:var(--c-text-muted);" title="{certain_title}">{certain}</td>'
                    f'<td style="padding:7px 10px;font-size:.75rem;color:var(--c-text-2);">{_html.escape(desc)}</td>'
                    f'<td style="padding:7px 10px;">{doc_cell}</td>'
                    f'<td style="padding:7px 6px;text-align:right;">{hide_btn}</td>'
                    f"</tr>"
                )

            ui.html(
                f'<table style="width:100%;border-collapse:collapse;">'
                f'<thead style="position:sticky;top:0;background:var(--c-surface);z-index:1;">'
                f'<tr style="border-bottom:1px solid var(--c-border);">'
                f'<th style="{th_style}">{_("Date")}</th>'
                f'<th style="padding:6px 4px;width:20px;font-size:.65rem;color:var(--c-text-muted);"></th>'
                f'<th style="{th_style}">{_("Description")}</th>'
                f'<th style="{th_style}">{_("Doc")}</th>'
                f'<th style="width:28px;"></th>'
                f"</tr></thead>"
                f'<tbody style="background:var(--c-bg);">{rows_html}</tbody>'
                f"</table>",
                sanitize=False,
            )

            if total_pages > 1:
                with ui.row().classes(
                    "items-center justify-between px-3 py-2 border-t border-gray-700"
                ):

                    def _go_prev() -> None:
                        _dl_page[0] -= 1
                        render_deadline_table()

                    def _go_next() -> None:
                        _dl_page[0] += 1
                        render_deadline_table()

                    prev_btn = (
                        ui.button(icon="chevron_left")
                        .props("flat dark dense")
                        .classes("text-gray-400")
                    )
                    prev_btn.set_enabled(page > 0)
                    prev_btn.on_click(_go_prev)
                    ui.label(
                        _("Page {page} / {total}  ·  {count} entries").format(
                            page=page + 1, total=total_pages, count=len(display_rows)
                        )
                    ).classes("text-xs text-gray-500")
                    next_btn = (
                        ui.button(icon="chevron_right")
                        .props("flat dark dense")
                        .classes("text-gray-400")
                    )
                    next_btn.set_enabled(page < total_pages - 1)
                    next_btn.on_click(_go_next)

    _render_deadline[0] = render_deadline_table
    render_deadline_table()
