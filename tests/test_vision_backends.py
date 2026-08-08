"""Ingestion works with cloud models, not just a local Ollama.

Settings > Processing lists every enabled registry model, but the vision client
only ever spoke Ollama's native /api/chat — so picking a Claude or OpenAI entry
silently produced a broken sync. Three transports now exist behind one interface.

The awkward part is credentials: registry entries are encrypted per user with
that user's Paperless token, so the key cannot be copied into the global
settings store. build_vision_client therefore resolves the entry from whoever is
signed in, and falls back to the .env Ollama path when there is no user context.
"""

import base64
import json

import httpx
import pytest

from models.extraction import PageImage
from services import vision as V


@pytest.fixture
def page():
    return PageImage(page_number=1, total_pages=3, image_bytes=b"\xff\xd8jpegbytes")


class _Recorder:
    """Captures the outgoing request instead of performing it."""

    def __init__(self, response: dict, status: int = 200):
        self.response = response
        self.status = status
        self.calls: list[dict] = []

    def install(self, monkeypatch):
        rec = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None, timeout=None):
                rec.calls.append({"url": url, "json": json, "headers": headers or {}})
                return httpx.Response(
                    rec.status, json=rec.response, request=httpx.Request("POST", url)
                )

        monkeypatch.setattr(V.httpx, "AsyncClient", lambda *a, **k: _Client())
        return self


_EXTRACTION = {
    "page_text": "Rechnung Nr. 42",
    "tables": [],
    "actions": [],
    "page_summary": "Eine Rechnung",
    "cross_references": [],
}


# ── Ollama: unchanged behaviour ───────────────────────────────────────────────


def test_ollama_uses_the_native_endpoint_with_schema_constrained_decoding(page, monkeypatch):
    """`format` is why ingestion is reliable on dense pages — it must stay."""
    rec = _Recorder({"message": {"content": json.dumps(_EXTRACTION)}}).install(monkeypatch)
    client = V.OllamaVisionClient(base_url="http://box:11434", model="qwen-vl")

    import asyncio

    result = asyncio.run(client.analyze_document(page, "Rechnung", "Rechnung"))

    sent = rec.calls[0]
    assert sent["url"] == "http://box:11434/api/chat"
    assert sent["json"]["format"] == V.EXTRACTION_JSON_SCHEMA
    assert sent["json"]["messages"][0]["images"] == [
        base64.b64encode(page.image_bytes).decode()
    ]
    assert result.page_text == "Rechnung Nr. 42"


# ── OpenAI-compatible ─────────────────────────────────────────────────────────


def test_openai_compatible_sends_image_and_json_schema(page, monkeypatch):
    rec = _Recorder(
        {"choices": [{"message": {"content": json.dumps(_EXTRACTION)}}]}
    ).install(monkeypatch)
    client = V.OpenAICompatibleVisionClient(
        base_url="https://api.example.com/v1", model="gpt-vision", api_key="sk-test"
    )

    import asyncio

    result = asyncio.run(client.analyze_document(page, "Rechnung", "Rechnung"))

    sent = rec.calls[0]
    assert sent["url"] == "https://api.example.com/v1/chat/completions"
    assert sent["headers"]["Authorization"] == "Bearer sk-test"
    assert sent["json"]["response_format"]["json_schema"]["schema"] == V.EXTRACTION_JSON_SCHEMA
    content = sent["json"]["messages"][0]["content"]
    assert content[0]["type"] == "image_url"
    assert content[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert result.page_summary == "Eine Rechnung"


def test_openai_compatible_appends_v1_when_missing(page, monkeypatch):
    rec = _Recorder({"choices": [{"message": {"content": json.dumps(_EXTRACTION)}}]}).install(
        monkeypatch
    )
    client = V.OpenAICompatibleVisionClient(base_url="http://lmstudio:1234", model="m")

    import asyncio

    asyncio.run(client.analyze_document(page, "x", "x"))
    assert rec.calls[0]["url"] == "http://lmstudio:1234/v1/chat/completions"


def test_openai_compatible_omits_auth_header_without_a_key(page, monkeypatch):
    rec = _Recorder({"choices": [{"message": {"content": json.dumps(_EXTRACTION)}}]}).install(
        monkeypatch
    )
    client = V.OpenAICompatibleVisionClient(base_url="http://local:8000/v1", model="m")

    import asyncio

    asyncio.run(client.analyze_document(page, "x", "x"))
    assert "Authorization" not in rec.calls[0]["headers"]


def test_openai_compatible_retries_without_response_format_on_400(page, monkeypatch):
    """Many compatible servers reject response_format — a page must not be lost."""
    import asyncio

    calls: list[dict] = []

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None, timeout=None):
            calls.append(json)
            req = httpx.Request("POST", url)
            if len(calls) == 1:
                return httpx.Response(400, json={"error": "unsupported"}, request=req)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": json_dumps_fenced()}}]},
                request=req,
            )

    def json_dumps_fenced():
        return "```json\n" + json.dumps(_EXTRACTION) + "\n```"

    monkeypatch.setattr(V.httpx, "AsyncClient", lambda *a, **k: _Client())
    client = V.OpenAICompatibleVisionClient(base_url="http://x/v1", model="m")

    result = asyncio.run(client.analyze_document(page, "x", "x"))

    assert len(calls) == 2
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]
    assert result.page_text == "Rechnung Nr. 42"  # code fence stripped


# ── Anthropic ─────────────────────────────────────────────────────────────────


def test_anthropic_forces_the_schema_through_a_tool(page, monkeypatch):
    rec = _Recorder(
        {"content": [{"type": "tool_use", "name": "emit_page_extraction", "input": _EXTRACTION}]}
    ).install(monkeypatch)
    client = V.AnthropicVisionClient(model="claude-x", api_key="sk-ant")

    import asyncio

    result = asyncio.run(client.analyze_document(page, "Rechnung", "Rechnung"))

    sent = rec.calls[0]
    assert sent["url"].endswith("/v1/messages")
    assert sent["headers"]["x-api-key"] == "sk-ant"
    assert sent["json"]["tool_choice"] == {"type": "tool", "name": "emit_page_extraction"}
    assert sent["json"]["tools"][0]["input_schema"] == V.EXTRACTION_JSON_SCHEMA
    block = sent["json"]["messages"][0]["content"][0]
    assert block["type"] == "image" and block["source"]["media_type"] == "image/jpeg"
    assert result.page_text == "Rechnung Nr. 42"


def test_anthropic_summarize_reads_text_blocks(monkeypatch):
    rec = _Recorder({"content": [{"type": "text", "text": "Kurzfassung"}]}).install(monkeypatch)
    client = V.AnthropicVisionClient(model="claude-x", api_key="k")

    import asyncio

    assert asyncio.run(client.summarize_document("page summaries")) == "Kurzfassung"
    assert rec.calls[0]["json"]["messages"][0]["content"] == rec.calls[0]["json"]["messages"][0]["content"]


# ── Routing ───────────────────────────────────────────────────────────────────


@pytest.fixture
def registry(monkeypatch):
    entries: dict[str, dict] = {}

    def _get_by_name(name, username, token):
        return entries.get(name)

    import services.model_registry as mr

    monkeypatch.setattr(mr, "get_by_name", _get_by_name)
    return entries


def _stub_name(monkeypatch, name: str):
    import werkbank.settings_store as ws

    monkeypatch.setattr(ws, "get_ingest_model_name", lambda: name)


def test_no_user_context_falls_back_to_ollama(monkeypatch, registry):
    """Scripts and background callers have no session — .env path must still work."""
    _stub_name(monkeypatch, "Claude Vision")
    assert isinstance(V.build_vision_client("", ""), V.OllamaVisionClient)


def test_no_stored_name_falls_back_to_ollama(monkeypatch, registry):
    _stub_name(monkeypatch, "")
    assert isinstance(V.build_vision_client("bob", "tok"), V.OllamaVisionClient)


def test_deleted_registry_entry_falls_back_instead_of_crashing(monkeypatch, registry):
    _stub_name(monkeypatch, "Gone")
    assert isinstance(V.build_vision_client("bob", "tok"), V.OllamaVisionClient)


def test_anthropic_entry_routes_to_the_anthropic_client(monkeypatch, registry):
    registry["Claude Vision"] = {
        "name": "Claude Vision", "backend": "anthropic",
        "model": "claude-x", "api_key": "sk-ant", "base_url": "",
    }
    _stub_name(monkeypatch, "Claude Vision")
    client = V.build_vision_client("bob", "tok")
    assert isinstance(client, V.AnthropicVisionClient)
    assert client.model == "claude-x"


def test_anthropic_entry_without_a_key_gets_no_key(monkeypatch, registry):
    """There is no global key any more — the registry entry is the only source."""
    registry["C"] = {"name": "C", "backend": "anthropic", "model": "m", "api_key": ""}
    _stub_name(monkeypatch, "C")
    assert V.build_vision_client("bob", "tok")._api_key == ""


def test_openai_compatible_entry_routes_with_its_own_base_url(monkeypatch, registry):
    registry["MiniMax"] = {
        "name": "MiniMax", "backend": "openai_compatible",
        "model": "abab-vl", "api_key": "sk-mm", "base_url": "https://api.minimax.io/v1",
    }
    _stub_name(monkeypatch, "MiniMax")
    client = V.build_vision_client("bob", "tok")
    assert isinstance(client, V.OpenAICompatibleVisionClient)
    assert client._base_url == "https://api.minimax.io/v1"
    assert client.model == "abab-vl"


def test_openai_compatible_entry_without_a_base_url_falls_back(monkeypatch, registry):
    """Nothing to talk to — better the configured Ollama than a broken URL."""
    registry["Broken"] = {"name": "Broken", "backend": "openai_compatible", "model": "m"}
    _stub_name(monkeypatch, "Broken")
    assert isinstance(V.build_vision_client("bob", "tok"), V.OllamaVisionClient)


# ── The guard applies to every transport ──────────────────────────────────────


def test_extraction_guard_runs_for_cloud_backends_too(page, monkeypatch):
    """The degenerate-output retry lives in the base class precisely so a new
    transport cannot skip it — a looped page reaching the sidecar is invisible."""
    import asyncio

    looped = dict(_EXTRACTION, page_text="ha " * 5000)
    rec = _Recorder({"choices": [{"message": {"content": json.dumps(looped)}}]}).install(
        monkeypatch
    )
    client = V.OpenAICompatibleVisionClient(base_url="http://x/v1", model="m")

    result = asyncio.run(client.analyze_document(page, "x", "x"))

    assert len(rec.calls) == 2  # retried once
    assert len(result.page_text.split()) <= V.MAX_PLAUSIBLE_PAGE_WORDS  # then salvaged
