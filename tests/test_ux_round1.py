"""UX round 1 (docs/ux-overhaul-plan.md, "Shipped first"): app shortcuts, the
lean document list, the urgent reason, the overview context, failing fast on a
model host that does not answer."""

import asyncio
import socket
from datetime import date
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent


# ── App shortcuts (app_ui/pwa_manifest.py) ────────────────────────────────────

def test_manifest_language_follows_accept_language():
    from app_ui.pwa_manifest import pick_language

    assert pick_language("de-DE,de;q=0.9,en;q=0.8") == "de"
    assert pick_language("fr-FR,en;q=0.5") == "en"
    assert pick_language(None) == "en"


def test_shortcuts_without_transcription_have_no_memo(monkeypatch):
    from app_ui import memo_dialog, pwa_manifest

    monkeypatch.setattr(memo_dialog, "memo_configured", lambda: False)
    m = pwa_manifest.build_manifest(str(ROOT / "app_ui/static/manifest.json"), "en")
    assert [s["url"] for s in m["shortcuts"]] == ["/chat", "/brain"]
    monkeypatch.setattr(memo_dialog, "memo_configured", lambda: True)
    m = pwa_manifest.build_manifest(str(ROOT / "app_ui/static/manifest.json"), "de")
    assert [s["url"] for s in m["shortcuts"]] == ["/chat", "/brain", "/?memo=1"]
    assert m["start_url"] == "/start"  # the user's start page; the dashboard by default


def test_every_shortcut_icon_exists():
    from app_ui.pwa_manifest import shortcuts

    for s in shortcuts("en", memo=True):
        purposes = {i["purpose"] for i in s["icons"]}
        assert purposes == {"any", "maskable"}
        for icon in s["icons"]:
            assert (ROOT / "app_ui" / icon["src"].lstrip("/")).is_file(), icon["src"]


# ── Lean document list (services/paperless.py) ───────────────────────────────

def _doc(i):
    return {"id": i, "title": f"Doc {i}", "correspondent": 1, "document_type": None, "tags": [5],
            "owner": None, "created": "2026-01-01T00:00:00Z", "added": "2026-01-02T00:00:00Z",
            "modified": "2026-01-03T00:00:00Z", "original_file_name": f"{i}.pdf",
            "mime_type": "application/pdf", "page_count": 1, "notes": []}


@pytest.fixture
def fake_paperless(monkeypatch):
    """Documents 1..7 in pages of 3; records what was asked for."""
    seen: list[httpx.Request] = []
    docs = [_doc(i) for i in range(1, 8)]

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        path = req.url.path
        if path == "/api/documents/":
            size = int(req.url.params["page_size"])
            page = int(req.url.params["page"])
            chunk = docs[(page - 1) * size: page * size]
            return httpx.Response(200, json={"count": len(docs), "next": None, "results": chunk})
        if path == "/api/tags/":
            return httpx.Response(200, json={"results": [{"id": 5, "name": "Rechnung"}]})
        if path == "/api/correspondents/":
            return httpx.Response(200, json={"results": [{"id": 1, "name": "Stadtwerke"}]})
        return httpx.Response(200, json={"results": []})

    real = httpx.AsyncClient

    class _Client(real):
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(handler)
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    from services.paperless import PaperlessClient

    PaperlessClient._SHARED_MAPS.clear()
    return seen


def test_catalogue_asks_for_metadata_only_and_reads_every_page(fake_paperless):
    from services.paperless import PaperlessClient

    docs = asyncio.run(PaperlessClient("http://pl.invalid", "tok").list_catalogue(page_size=3))
    assert [d.id for d in docs] == list(range(1, 8))
    assert docs[0].tags == ["Rechnung"] and docs[0].correspondent == "Stadtwerke"
    doc_reqs = [r for r in fake_paperless if r.url.path == "/api/documents/"]
    assert sorted(int(r.url.params["page"]) for r in doc_reqs) == [1, 2, 3]
    fields = doc_reqs[0].url.params["fields"].split(",")
    assert "content" not in fields and "title" in fields


def test_name_maps_are_shared_by_clients_of_one_token_only(fake_paperless):
    from services.paperless import PaperlessClient

    asyncio.run(PaperlessClient("http://pl.invalid", "tok").list_catalogue(page_size=10))
    asyncio.run(PaperlessClient("http://pl.invalid", "tok").list_catalogue(page_size=10))
    assert sum(r.url.path == "/api/tags/" for r in fake_paperless) == 1
    asyncio.run(PaperlessClient("http://pl.invalid", "other").list_catalogue(page_size=10))
    assert sum(r.url.path == "/api/tags/" for r in fake_paperless) == 2


def test_scoped_catalogue_never_lists_outside_the_scope(fake_paperless):
    from services.expert_scope import Scope, ScopedPaperlessClient

    scope = Scope(correspondents=frozenset({99}))
    docs = asyncio.run(ScopedPaperlessClient("http://pl.invalid", "tok", scope).list_catalogue(page_size=10))
    assert docs == []


# ── Findings: why a finding is urgent by itself ───────────────────────────────

def test_urgent_reason_names_the_deadline_or_the_rating():
    from app_ui import findings_view as fv
    from proactive.findings import Finding

    today = date(2026, 11, 27)
    _ = lambda s: s

    def f(**kw):
        return Finding(finding_id="x", kind="deadline", title="t", **kw)

    assert fv.urgent_reason(f(deadline="2026-11-30"), today, _) == "Urgent: due in 3 days"
    assert fv.urgent_reason(f(deadline="2026-11-28"), today, _) == "Urgent: due tomorrow"
    assert fv.urgent_reason(f(deadline="2026-11-27"), today, _) == "Urgent: due today"
    assert fv.urgent_reason(f(deadline="2026-11-20"), today, _) == "Urgent: overdue"
    assert fv.urgent_reason(f(urgency="high"), today, _) == "Urgent: rated high priority"


# ── Chat: a model host that does not answer ───────────────────────────────────

def test_reachable_is_false_for_a_closed_port():
    from services import model_host

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens there now
    assert asyncio.run(model_host.reachable(f"http://127.0.0.1:{port}/v1", timeout=1)) is False
    assert asyncio.run(model_host.reachable("", timeout=1)) is False


def test_reachable_is_true_for_a_listening_port():
    from services import model_host

    async def go():
        server = await asyncio.start_server(lambda r, w: w.close(), "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            return await model_host.reachable(f"http://127.0.0.1:{port}", timeout=1)

    assert asyncio.run(go()) is True


def test_turn_errors_name_the_host_instead_of_an_empty_error():
    from app_ui.pages.chat import UserFacingError, _turn_error_text

    _ = lambda s: s
    req = httpx.Request("POST", "http://gpu.lan:11434/v1/chat/completions")
    assert _turn_error_text(httpx.ConnectTimeout("", request=req), _) == \
        "Cannot reach the model host gpu.lan:11434. Is it switched on?"
    assert _turn_error_text(UserFacingError("Said as is."), _) == "Said as is."
    assert _turn_error_text(RuntimeError(""), _) == "Error: RuntimeError"


# ── Reload diagnostics (main.py) ──────────────────────────────────────────────

def test_draft_and_revive_scripts_are_on_every_page():
    from app_ui import layout

    assert "pb-draft:" in layout.DRAFT_JS and "__pbDraftClear" in layout.DRAFT_JS
    assert "/api/pb-client-log" in layout.SOCKET_REVIVE_JS
    assert "wasDiscarded" in layout.SOCKET_REVIVE_JS
    src = (ROOT / "app_ui/layout.py").read_text()
    assert "ui.add_head_html(DRAFT_JS)" in src


# ── Findings dock on the overview ─────────────────────────────────────────────

def test_overview_context_lists_urgent_due_and_dossiers(monkeypatch):
    from types import SimpleNamespace

    from app_ui import findings_view as fv
    from proactive import dossiers, findings
    from proactive.findings import Finding

    today = date(2026, 11, 27)
    items = [
        Finding(finding_id="a", kind="deadline", title="Kündigen bis", deadline="2026-11-30", doc_ids=[7]),
        Finding(finding_id="b", kind="deadline", title="Verlängerung", deadline="2027-01-10"),
        Finding(finding_id="c", kind="insight", title="Preisänderung", topic="strom", topic_label="Strom"),
        Finding(finding_id="d", kind="insight", title="Erledigt", status="done"),
    ]
    monkeypatch.setattr(findings, "list_findings", lambda u: items)
    ins = SimpleNamespace(created="2026-11-20T10:00", title="Tarif geprüft")
    ds = [SimpleNamespace(display="Strom", open_questions=["Wechseln?"], insights=[ins])]
    monkeypatch.setattr(dossiers, "list_dossiers", lambda u: ds)
    text = fv.overview_context_text("alice", today)
    urgent, due, other, dos = text.split("\n\n")[1:5]
    assert "Kündigen bis" in urgent and "#7" in urgent and "finding a" in urgent
    assert "Verlängerung" in due
    assert "Preisänderung" in other and "topic: Strom" in other
    assert "Erledigt" not in text
    assert "Strom · 1 open questions" in dos and "Tarif geprüft" in dos
