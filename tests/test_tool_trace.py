"""Tool history downloads: every model request and tool call, the raw request."""

from datetime import datetime

from app_ui.tool_trace import call_summary, totals, trace_markdown, transcript_markdown

NOW = datetime(2026, 11, 14, 9, 30)

TRACE = [
    {"iter": 1, "name": "search", "label": "Searching", "input": {"semantic_query": "car"},
     "output": "x" * 2000, "output_full": "x" * 5000 + " END", "duration_ms": 420},
    {"iter": 2, "name": "get_document_details", "label": "Details", "input": {"document_id": 7},
     "output": "```code``` inside", "output_full": "```code``` inside", "duration_ms": 1500},
]
CALLS = [
    {"iteration": 1, "input_tokens": 120, "output_tokens": 40, "cache_read": 9000, "cache_write": 0,
     "estimated_prompt": 0, "first_token_ms": 800, "total_ms": 1800, "stop": "tool_use"},
    {"iteration": 2, "input_tokens": 300, "output_tokens": 200, "cache_read": 0, "cache_write": 0,
     "estimated_prompt": 11000, "first_token_ms": 2000, "total_ms": 6000, "stop": "stop"},
]


def test_history_holds_the_whole_result_not_the_capped_one():
    md = trace_markdown(TRACE, CALLS, model="Opus", question="Where is my car insurance?", now=NOW)
    assert "x" * 5000 + " END" in md
    assert "## Iteration 1" in md and "## Iteration 2" in md
    assert "420 ms" in md and "1.5 s" in md
    assert "model Opus" in md and "Where is my car insurance?" in md


def test_a_backtick_run_in_a_result_does_not_close_the_fence():
    md = trace_markdown(TRACE, CALLS, now=NOW)
    assert "````\n```code``` inside\n````" in md


def test_token_lines_say_what_the_numbers_are():
    assert "cache 9 000 read" in call_summary(CALLS[0])
    # OpenAI-compatible: our estimate of the prompt, the server's evaluated count next to it
    s = call_summary(CALLS[1])
    assert "prompt ≈11 000 (evaluated 300)" in s and "50 tok/s" in s
    t = totals(TRACE, CALLS)
    assert t["input"] == 120 + 9000 + 300 and t["output"] == 240 and t["tool_ms"] == 1920


def test_raw_conversation_has_system_prompt_messages_and_tools_both_shapes():
    anthropic = [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Looking."},
            {"type": "tool_use", "id": "t1", "name": "search", "input": {"q": "a"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "RESULT-A"}]},
        {"role": "assistant", "content": "Done."},
    ]
    md = transcript_markdown("SYSTEM-PROMPT", [{"name": "search"}], anthropic, model="m", now=NOW)
    for part in ("SYSTEM-PROMPT", "**Tool call** `search`", "RESULT-A", "Done.", "## Tool schemas"):
        assert part in md
    openai = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "search", "arguments": '{"q": "b"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "RESULT-B"},
    ]
    md = transcript_markdown("S", [], openai, now=NOW)
    assert '"q": "b"' in md and "RESULT-B" in md and "## 2. tool (c1)" in md


def test_transcript_event_drops_thinking():
    from services.chat_service import _transcript

    ev = _transcript("S", [], [{"role": "assistant", "content": [
        {"type": "thinking", "thinking": "SECRET", "signature": "x"},
        {"type": "redacted_thinking", "data": "y"},
        {"type": "text", "text": "visible"}]}], "answer")
    flat = str(ev.messages)
    assert "SECRET" not in flat and "redacted" not in flat and "visible" in flat
    assert ev.messages[-1] == {"role": "assistant", "content": "answer"}


def test_steps_put_parallel_tools_under_their_one_request():
    from app_ui.tool_trace import step_list

    calls = [{"iteration": 1, "total_ms": 4400}, {"iteration": 2, "total_ms": 900}]
    trace = [{"iter": 1, "name": "search", "duration_ms": 5},
             {"iter": 1, "name": "analytical_search", "duration_ms": 3},
             {"iter": 2, "name": "get_document_details", "duration_ms": 9}]
    steps = step_list(trace, calls)
    assert [(s["kind"], s["iter"]) for s in steps] == [
        ("model", 1), ("tool", 1), ("tool", 1), ("model", 2), ("tool", 2)]
    assert steps[0]["n_tools"] == 2 and not any(s["running"] for s in steps)


def test_live_steps_show_the_request_and_tool_in_progress():
    from app_ui.tool_trace import step_list

    calls = [{"iteration": 1, "total_ms": 4400}]
    trace = [{"iter": 1, "name": "search"}]          # no result yet
    steps = step_list(trace, calls, running=True, iteration=1)
    assert steps[-1]["kind"] == "tool" and steps[-1]["running"]
    trace[0]["duration_ms"] = 12
    steps = step_list(trace, calls, running=True, iteration=2)   # next request under way
    assert steps[-1] == {"kind": "model", "key": "m2", "iter": 2, "call": None, "n_tools": 0, "running": True}
    assert not step_list(trace, calls, running=False, iteration=2)[-1]["running"]


def test_sources_beyond_three_fold_away(monkeypatch):
    from app_ui.pages import chat

    monkeypatch.setattr(chat, "get_translator", lambda: (lambda s: s))
    few = chat._citation_list([(1, 7, 1), (2, 8, 0)], {})
    assert "pb-cite-more" not in few
    many = chat._citation_list([(1, 7, 1), (2, 7, 1), (3, 8, 0), (4, 9, 2), (5, 10, 1)], {})
    head, _sep, folded = many.partition('<details class="pb-cite-more">')
    assert head.count("pb-cite-doc\"") == 3 and folded.count("pb-cite-doc\"") == 1
    assert "Show 1 more" in many and "<b>1, 2</b>" in many
