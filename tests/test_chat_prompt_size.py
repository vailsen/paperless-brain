"""The context meter and the prompt size a local model reports.

Ollama counts only the prompt tokens it had to evaluate: a prefix reused from
its cache is left out, so a long conversation read as a few thousand tokens
(2 % of a 141k window). A reported count far below the request's own size is
that, not a smaller prompt."""

from services.chat_service import estimate_prompt_tokens, reported_or_estimate


def test_the_estimate_counts_messages_and_tool_schemas():
    msgs = [{"role": "system", "content": "x" * 3500}, {"role": "user", "content": "y" * 350}]
    bare = estimate_prompt_tokens(msgs)
    assert 1000 < bare < 1200  # 3.5 characters a token, plus the JSON around each message
    tools = [{"type": "function", "function": {"name": "search", "description": "z" * 700}}]
    assert estimate_prompt_tokens(msgs, tools) > bare + 200


def test_a_cache_sized_count_is_replaced_by_the_estimate():
    assert reported_or_estimate(2_800, 30_000) == 30_000  # Ollama after a cache hit
    assert reported_or_estimate(29_000, 30_000) == 29_000  # a real count wins
    assert reported_or_estimate(0, 500) == 500  # a server that reports nothing
