"""SDK 2.x sends the key as `Authorization: Bearer`; an Anthropic-format
gateway behind a custom base URL gets it as `X-Api-Key`, as every 1.x request
sent it (`services/anthropic_client.py`)."""

import asyncio

import pytest

anthropic = pytest.importorskip("anthropic")


def _headers(client) -> dict:
    """The headers one request of `client` goes out with."""
    seen: dict = {}

    async def go():
        import httpx2

        def handler(request):
            seen.update({k.lower(): v for k, v in request.headers.items()})
            return httpx2.Response(200, json={
                "id": "m", "type": "message", "role": "assistant", "model": "x", "content": [],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1}})

        client._client._transport = httpx2.MockTransport(handler)  # noqa: SLF001
        await client.messages.create(model="x", max_tokens=1, messages=[{"role": "user", "content": "hi"}])

    asyncio.run(go())
    return seen


def test_a_gateway_gets_the_key_as_x_api_key():
    pytest.importorskip("httpx2")
    from services.anthropic_client import async_client

    h = _headers(async_client("sk-test", "http://gateway.invalid"))
    assert h.get("x-api-key") == "sk-test" and "authorization" not in h


def test_first_party_keeps_the_sdk_default():
    pytest.importorskip("httpx2")
    from services.anthropic_client import async_client

    h = _headers(async_client("sk-test"))
    assert h.get("authorization") == "Bearer sk-test" and "x-api-key" not in h
