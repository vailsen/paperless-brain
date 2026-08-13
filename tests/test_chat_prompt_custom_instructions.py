"""The one part of the chat system prompt the user writes.

The tool blocks stay in code — they are calling contracts, and a renamed tool
there stops being called with no error. What the user gets is an additive block,
and these tests pin the two properties that make that safe: it is subordinated
to the anti-fabrication ground rules, and it is bounded.
"""

import pytest

from config.chat_prompts import (
    CUSTOM_INSTRUCTIONS_HEADER,
    MAX_CUSTOM_INSTRUCTIONS_CHARS,
    _CORE,
    build_system_prompt,
)

INSTRUCTIONS = "My archive is mostly invoices. Reference numbers look like RE-2026-0142."


def test_instructions_are_included():
    prompt = build_system_prompt(custom_instructions=INSTRUCTIONS)
    assert INSTRUCTIONS in prompt


def test_nothing_is_added_when_the_box_is_empty():
    """An empty setting must not leave a dangling header in the prompt."""
    assert CUSTOM_INSTRUCTIONS_HEADER not in build_system_prompt()
    assert CUSTOM_INSTRUCTIONS_HEADER not in build_system_prompt(custom_instructions="   ")


def test_instructions_are_subordinated_to_the_ground_rules():
    """The safety design in one assertion.

    Later text outweighs earlier text, so the user block sits after _CORE and
    would otherwise outrank the rules that stop the model inventing document
    IDs for an archive it has never seen. The header is what prevents that, so
    it must appear with the instructions, not merely somewhere in the prompt.
    """
    prompt = build_system_prompt(custom_instructions=INSTRUCTIONS)
    header_at = prompt.index(CUSTOM_INSTRUCTIONS_HEADER)
    assert header_at < prompt.index(INSTRUCTIONS)
    # The ground rules still precede the whole thing.
    assert prompt.index(_CORE) < header_at


def test_instructions_come_before_the_language_directive():
    """Two adjacent instructions about how to answer must not be interleaved."""
    prompt = build_system_prompt(custom_instructions=INSTRUCTIONS)
    assert prompt.index(INSTRUCTIONS) < prompt.index("Respond in the language")


def test_overlong_instructions_are_truncated_not_rejected():
    """A prompt that loses its tail beats a chat that refuses to start."""
    long = "x" * (MAX_CUSTOM_INSTRUCTIONS_CHARS + 500)
    prompt = build_system_prompt(custom_instructions=long)
    assert "x" * MAX_CUSTOM_INSTRUCTIONS_CHARS in prompt
    assert "x" * (MAX_CUSTOM_INSTRUCTIONS_CHARS + 1) not in prompt


def test_instructions_survive_with_tool_groups_disabled():
    """The block is not tied to any tool group — it applies to every session."""
    prompt = build_system_prompt(active_groups=set(), custom_instructions=INSTRUCTIONS)
    assert INSTRUCTIONS in prompt


@pytest.mark.parametrize("groups", [None, set(), {"documents"}, {"documents", "web"}])
def test_default_prompt_is_unchanged_without_instructions(groups):
    """Nobody who never opens the setting gets a different prompt than before."""
    before = build_system_prompt(active_groups=groups, username="alice")
    after = build_system_prompt(active_groups=groups, username="alice", custom_instructions="")
    assert before == after
