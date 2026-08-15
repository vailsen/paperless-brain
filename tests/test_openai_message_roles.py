"""Where a mid-conversation reminder may sit in an OpenAI-compatible request.

Ollama's /v1/chat/completions rejects a system message anywhere but position 0
with a 500 ("system message must be at the beginning"). Both reminders the
backend injects after a tool call — the tool-use guard and the answer-language
note — used to be system messages, which killed every turn that called a tool
on a model served that way. They must stay non-system, and the failure that
surfaces when a server refuses must carry the server's own reason.
"""

import json

import pytest

from services.chat_service import OpenAICompatibleChatBackend, _api_error_text

TOOLS = [
    {
        "name": "search",
        "description": "Search documents",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    }
]


def _sse(chunk: dict) -> str:
    return f"data: {json.dumps(chunk)}"


def _tool_call_response() -> list[str]:
    return [
        _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "search",
                                        "arguments": '{"query": "ACME"}',
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        ),
        "data: [DONE]",
    ]


def _text_response(text: str) -> list[str]:
    return [_sse({"choices": [{"delta": {"content": text}}]}), "data: [DONE]"]


class _FakeResponse:
    def __init__(self, lines: list[str], status_code: int = 200, body: str = ""):
        self._lines = lines
        self.status_code = status_code
        self.text = body

    async def aread(self):
        return self.text.encode()

    async def aclose(self):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStream:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    """Replays a scripted list of responses and records every payload sent."""

    def __init__(self, script: list[_FakeResponse], sent: list[dict]):
        self._script = script
        self._sent = sent

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, _method, _url, json=None, headers=None):
        self._sent.append(json)
        return _FakeStream(self._script.pop(0))


@pytest.fixture
def backend(monkeypatch):
    async def _no_probe(self):
        self.context_window = 8192

    async def _fake_tool(name, args, *a, **kw):
        return "Found: invoice ACME 2026-01, 120 EUR.", [], []

    monkeypatch.setattr(OpenAICompatibleChatBackend, "_ensure_ctx", _no_probe)
    monkeypatch.setattr("services.chat_service.execute_tool", _fake_tool)
    monkeypatch.setattr("services.chat_service._wol_touch", lambda: None)
    # Labels are translated, and get_translator() needs a browser session.
    monkeypatch.setattr("services.chat_service._tool_label", lambda name: name)
    return OpenAICompatibleChatBackend(
        base_url="http://ollama.invalid/v1", api_key="", model="test-model"
    )


def _run(backend, monkeypatch, script: list[_FakeResponse], **kwargs) -> list[dict]:
    import asyncio

    sent: list[dict] = []
    monkeypatch.setattr(
        "services.chat_service.httpx.AsyncClient",
        lambda **kw: _FakeClient(script, sent),
    )

    async def drive():
        async for _event in backend.run_turn(
            messages=[{"role": "user", "content": "What did I buy from ACME?"}],
            system="You are helpful.",
            tools=TOOLS,
            **kwargs,
        ):
            pass

    asyncio.run(drive())
    return sent


def _roles(payload: dict) -> list[str]:
    return [m["role"] for m in payload["messages"]]


def test_the_answer_language_reminder_is_not_a_system_message(backend, monkeypatch):
    script = [
        _FakeResponse(_tool_call_response()),
        _FakeResponse(_text_response("Eine Rechnung über 120 EUR.")),
    ]
    sent = _run(backend, monkeypatch, script, answer_language="de")

    assert len(sent) == 2
    roles = _roles(sent[1])
    assert roles[-1] != "system"
    assert "system" not in roles[1:]


def test_the_tool_use_guard_reminder_is_not_a_system_message(backend, monkeypatch):
    """The guard fires when a personal question comes back with no tool call."""
    script = [
        _FakeResponse(_text_response("You bought a torque wrench.")),
        _FakeResponse(_tool_call_response()),
        _FakeResponse(_text_response("An invoice for 120 EUR.")),
    ]
    sent = _run(backend, monkeypatch, script)

    assert len(sent) >= 2
    assert "system" not in _roles(sent[1])[1:]


def test_every_request_keeps_exactly_one_system_message_first(backend, monkeypatch):
    script = [
        _FakeResponse(_tool_call_response()),
        _FakeResponse(_text_response("Done.")),
    ]
    sent = _run(backend, monkeypatch, script, answer_language="en")

    for payload in sent:
        roles = _roles(payload)
        assert roles[0] == "system"
        assert roles.count("system") == 1


# ── The server's reason must survive the failure ─────────────────────────────


def test_a_refusal_carries_the_servers_own_message(backend, monkeypatch):
    body = json.dumps(
        {"error": {"message": "system message must be at the beginning"}}
    )
    script = [_FakeResponse([], status_code=500, body=body)]

    with pytest.raises(RuntimeError) as exc:
        _run(backend, monkeypatch, script)

    assert "system message must be at the beginning" in str(exc.value)


@pytest.mark.parametrize(
    "body, expected",
    [
        ('{"error": {"message": "context length exceeded"}}', "context length exceeded"),
        ('{"error": "model not found"}', "model not found"),
        ("<html>502 Bad Gateway</html>", "<html>502 Bad Gateway</html>"),
        ("", ""),
    ],
)
def test_error_bodies_of_every_shape_yield_their_message(body, expected):
    assert _api_error_text(body) == expected
