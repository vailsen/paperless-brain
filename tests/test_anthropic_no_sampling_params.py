"""The Anthropic path sends no sampling parameters.

This is pinned because it broke in production and nowhere else. The dependency
range was `anthropic>=0.117`, the dev machine resolved 0.117 (where
`messages.create()` still took `temperature`) and the Docker image resolved
1.4.0 (where it does not) — so every Anthropic structured call in the container
died with `AsyncMessages.create() got an unexpected keyword argument
'temperature'` while the same code passed locally.

Current Claude models reject sampling parameters server-side too, so the fix is
the same on either SDK: don't send them. The OpenAI-compatible path, which every
local and gateway model takes, still honours `temperature` and is unaffected.
"""

import asyncio
import sys
import types

import pytest

SAMPLING_PARAMS = ("temperature", "top_p", "top_k")


class _Block:
    type = "tool_use"
    name = "revise_note"
    input = {"text": "ok", "summary": "s"}


class _Response:
    content = [_Block()]
    stop_reason = "tool_use"


def _fake_anthropic(seen: dict) -> types.ModuleType:
    """A stand-in for the SDK that records the kwargs it was called with."""

    class _Messages:
        async def create(self, **kwargs):
            seen.update(kwargs)
            return _Response()

        def stream(self, **kwargs):
            seen.update(kwargs)
            raise AssertionError("streaming is not exercised by this test")

    class _AsyncAnthropic:
        def __init__(self, **kwargs):
            seen["client_kwargs"] = kwargs
            self.messages = _Messages()

    mod = types.ModuleType("anthropic")
    mod.AsyncAnthropic = _AsyncAnthropic
    return mod


@pytest.fixture
def seen(monkeypatch):
    box: dict = {}
    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic(box))
    return box


def test_structured_call_sends_no_sampling_parameters(seen):
    from werkbank.llm_lane import _call_claude_structured

    asyncio.run(_call_claude_structured(
        "key", "claude-opus-5", "system", [{"role": "user", "content": "hi"}],
        {"type": "object"}, "revise_note", 4000, 0.1,
    ))
    for param in SAMPLING_PARAMS:
        assert param not in seen, f"{param} must not reach the Anthropic SDK"
    assert seen["max_tokens"] == 4000       # everything else still goes through
    assert seen["tool_choice"]["name"] == "revise_note"


def test_plain_call_sends_no_sampling_parameters(seen):
    from werkbank.llm_lane import _call_claude

    class _Text:
        type = "text"
        text = "hello"

    _Response.content = [_Text()]
    try:
        asyncio.run(_call_claude(
            "key", "claude-opus-5", "system", [{"role": "user", "content": "hi"}],
            None, 4000, 0.3,
        ))
    finally:
        _Response.content = [_Block()]
    for param in SAMPLING_PARAMS:
        assert param not in seen, f"{param} must not reach the Anthropic SDK"


def test_the_openai_compatible_path_still_gets_a_temperature():
    """The parameter is not dead — it is only wrong for one backend."""
    import inspect

    from werkbank.llm_lane import _call_openai_compatible_structured

    assert "temperature" in inspect.signature(
        _call_openai_compatible_structured
    ).parameters


def test_claude_chat_backend_asks_for_no_temperature():
    """`_thinking_params` used to force temperature to 1.0 for extended thinking
    and hand it back to the caller. That rule only existed while the parameter
    did."""
    for first_party in (True, False):
        extra, max_tokens = _backend(first_party=first_party)._thinking_params()
        assert isinstance(max_tokens, int)
        assert not any(p in extra for p in SAMPLING_PARAMS)


# ── Thinking parameters ───────────────────────────────────────────────────────
#
# The same class fronts first-party Anthropic and Anthropic-compatible endpoints
# (MiniMax and friends), and the two need different thinking blocks: first party
# removed `budget_tokens` and 400s on it, while the compatible endpoints
# implement it and know nothing about `adaptive`. Nothing in the model string
# says which is which — an empty base_url does.


def _backend(**kw):
    from services.chat_service import DEFAULT_MAX_OUTPUT_TOKENS, ClaudeChatBackend

    b = ClaudeChatBackend.__new__(ClaudeChatBackend)
    b.first_party = kw.get("first_party", True)
    b.think = kw.get("think", True)
    b.thinking_budget = kw.get("thinking_budget", 4096)
    b.effort = kw.get("effort", "")
    b.max_output_tokens = kw.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
    return b


def test_first_party_asks_for_adaptive_never_a_budget():
    extra, _max_tokens = _backend(first_party=True)._thinking_params()
    assert extra["thinking"] == {"type": "adaptive"}
    assert "budget_tokens" not in extra["thinking"]


def test_a_compatible_endpoint_keeps_its_budget():
    """These implement budget_tokens; `adaptive` would mean nothing to them."""
    extra, _max_tokens = _backend(first_party=False, thinking_budget=8192)._thinking_params()
    assert extra["thinking"] == {"type": "enabled", "budget_tokens": 8192}


def test_effort_is_only_sent_when_the_user_picked_one():
    """Omitted means the API's own default, which is not ours to guess."""
    extra, _m = _backend(first_party=True, effort="")._thinking_params()
    assert "output_config" not in extra
    extra, _m = _backend(first_party=True, effort="low")._thinking_params()
    assert extra["output_config"] == {"effort": "low"}


def test_effort_never_reaches_a_compatible_endpoint():
    extra, _m = _backend(first_party=False, effort="max")._thinking_params()
    assert "output_config" not in extra


def test_auto_sends_no_thinking_block_at_all():
    """A valid request on every model and every backend — that is the point."""
    extra, _m = _backend(think=None)._thinking_params()
    assert extra == {}


def test_thinking_off_is_the_same_on_both_backends():
    for first_party in (True, False):
        extra, _m = _backend(first_party=first_party, think=False)._thinking_params()
        assert extra == {"thinking": {"type": "disabled"}}


def test_a_large_budget_still_buys_room_on_first_party():
    """The budget is not sent there any more, but thinking tokens still come out
    of max_tokens — so the room it bought must not silently disappear."""
    _e, first = _backend(first_party=True, thinking_budget=40_000)._thinking_params()
    _e, compat = _backend(first_party=False, thinking_budget=40_000)._thinking_params()
    assert first == compat > 40_000


# ── max_output_tokens ─────────────────────────────────────────────────────────
#
# The settings field promised "0 = default 16384" and did nothing on this
# backend: max_tokens was hardcoded to 12_000 and the value never reached the
# class. It is the field a user reaches for to give answers more room, so being
# inert on one backend is worse than being absent.


def test_the_configured_ceiling_is_used():
    for think in (None, False, True):
        _e, max_tokens = _backend(think=think, max_output_tokens=40_000)._thinking_params()
        assert max_tokens == 40_000, f"think={think}"


def test_the_default_ceiling_matches_what_the_settings_field_promises():
    from services.chat_service import DEFAULT_MAX_OUTPUT_TOKENS

    assert DEFAULT_MAX_OUTPUT_TOKENS == 16_384
    _e, max_tokens = _backend(think=None)._thinking_params()
    assert max_tokens == 16_384


def test_the_budget_raises_a_floor_it_never_lowers_the_ceiling():
    """The API needs max_tokens > budget_tokens, so a generous budget under a
    small ceiling has to lift it — otherwise the request is simply rejected."""
    _e, max_tokens = _backend(
        first_party=False, thinking_budget=30_000, max_output_tokens=8_000
    )._thinking_params()
    assert max_tokens > 30_000


def test_a_generous_ceiling_is_not_shrunk_by_a_small_budget():
    _e, max_tokens = _backend(thinking_budget=1024, max_output_tokens=64_000)._thinking_params()
    assert max_tokens == 64_000


def test_both_backends_get_the_same_ceiling():
    """Switching a model's backend must not silently change how long its
    answers may be."""
    kw = {"thinking_budget": 20_000, "max_output_tokens": 24_000}
    _e, first = _backend(first_party=True, **kw)._thinking_params()
    _e, compat = _backend(first_party=False, **kw)._thinking_params()
    assert first == compat


def test_an_unknown_effort_value_never_reaches_the_api():
    """`effort` is a closed enum. A value from an older config or a typo must be
    dropped at construction, not forwarded for the API to reject."""
    from services.chat_service import ClaudeChatBackend

    backend = ClaudeChatBackend.__new__(ClaudeChatBackend)
    ClaudeChatBackend.__init__(
        backend, api_key="k", model="claude-opus-5", think=True, effort="turbo"
    )
    assert backend.effort == ""
    extra, _max_tokens = backend._thinking_params()
    assert "output_config" not in extra
