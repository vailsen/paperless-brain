"""Tool-call arguments a model wrote in German.

A quote inside an argument value — `query="Rechnung „Milbich" 2023"` — makes the
whole argument object invalid JSON. Both the chat backend and the Werkbank
agentic lane used to answer that with `{}`: the tool then ran with no arguments
at all, returned nothing, and no log said why. The repair is the same one the
answers get; what it cannot repair is refused out loud.
"""

from __future__ import annotations

import asyncio

from services.chat_service import _parse_tool_arguments, execute_tool
from werkbank.llm_lane import _tool_args


def test_a_german_quote_in_an_argument_does_not_empty_the_call():
    raw = '{"query": "Rechnung „Milbich" 2023", "max_results": 5}'

    for parse in (_parse_tool_arguments, _tool_args):
        args = parse(raw)
        assert args["query"] == 'Rechnung „Milbich" 2023'
        assert args["max_results"] == 5


def test_valid_arguments_are_untouched():
    for parse in (_parse_tool_arguments, _tool_args):
        assert parse('{"query": "Vertrag", "page": 2}') == {"query": "Vertrag", "page": 2}


def test_unrepairable_arguments_are_reported_not_dropped():
    """`{}` is the dangerous answer: a search with no query answers "nothing
    found", and the model has no way to know it never asked anything."""
    broken = '{"query": '

    for parse in (_parse_tool_arguments, _tool_args):
        assert "_unparsed_arguments" in parse(broken)

    text, docs, extras = asyncio.run(execute_tool("search", parse(broken)))
    assert "was not run" in text
    assert (docs, extras) == ([], [])
