"""The tool history of one chat answer: dialog and two downloads.

- **Tool history (.md)** — every model request (tokens, time to the first
  token, total time) and every tool call (input, duration, the whole result).
- **Raw conversation (.md)** — what the model was sent on its last request:
  system prompt, tool schemas and every message of the loop, its answer
  appended. Thinking is never in it (`chat_service._transcript` removes it).

The dialog shows each result collapsed and renders it only when opened: a
16-step turn of document details and fetched pages is half a megabyte, and
sending all of it to the browser up front made the dialog slow on a phone.
Both live only as long as the page — nothing here is stored.
"""

from __future__ import annotations

import json
from datetime import datetime

from nicegui import ui

def _secs(ms: int) -> str:
    return f"{ms / 1000:.1f} s" if ms >= 1000 else f"{ms} ms"


def _fence(text: str, lang: str = "") -> str:
    """A code fence that survives backticks inside the text."""
    longest = run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{text}\n{fence}"


def call_summary(call: dict) -> str:
    """One line per model request: tokens and timings."""
    parts = []
    if call.get("cache_read") or call.get("cache_write"):
        parts.append(
            f"in {call.get('input_tokens', 0):,} + cache {call.get('cache_read', 0):,} read"
            f" / {call.get('cache_write', 0):,} written"
        )
    elif call.get("estimated_prompt"):
        parts.append(
            f"prompt ≈{call['estimated_prompt']:,} (evaluated {call.get('input_tokens', 0):,})"
        )
    else:
        parts.append(f"in {call.get('input_tokens', 0):,}")
    parts.append(f"out {call.get('output_tokens', 0):,}")
    parts.append(f"first token {_secs(call.get('first_token_ms', 0))}")
    total = call.get("total_ms", 0)
    parts.append(f"total {_secs(total)}")
    gen_s = (total - call.get("first_token_ms", 0)) / 1000
    if call.get("output_tokens") and gen_s > 0.2:
        parts.append(f"{call['output_tokens'] / gen_s:.0f} tok/s")
    if call.get("stop"):
        parts.append(call["stop"])
    return " · ".join(parts).replace(",", " ")


def totals(trace: list[dict], calls: list[dict]) -> dict:
    return {
        "model_ms": sum(c.get("total_ms", 0) for c in calls),
        "tool_ms": sum(t.get("duration_ms", 0) for t in trace),
        "input": sum(c.get("input_tokens", 0) + c.get("cache_read", 0) + c.get("cache_write", 0)
                     for c in calls),
        "output": sum(c.get("output_tokens", 0) for c in calls),
        "calls": len(calls),
        "tools": len(trace),
    }


def _steps(trace: list[dict], calls: list[dict]) -> list[tuple[int, dict | None, list[dict]]]:
    """(iteration, model request, tool calls of that request), in order."""
    iters = sorted({c["iteration"] for c in calls} | {t["iter"] for t in trace})
    by_call = {c["iteration"]: c for c in calls}
    return [(i, by_call.get(i), [t for t in trace if t["iter"] == i]) for i in iters]


def trace_markdown(trace: list[dict], calls: list[dict], *, model: str = "", question: str = "",
                   now: datetime | None = None) -> str:
    tot = totals(trace, calls)
    out = ["# Tool history", ""]
    if question:
        out += [f"> {' '.join(question.split())[:300]}", ""]
    meta = [f"{(now or datetime.now()):%Y-%m-%d %H:%M}"]
    if model:
        meta.append(f"model {model}")
    meta.append(f"{tot['calls']} model requests ({_secs(tot['model_ms'])})")
    meta.append(f"{tot['tools']} tool calls ({_secs(tot['tool_ms'])})")
    meta.append(f"tokens in {tot['input']:,} / out {tot['output']:,}".replace(",", " "))
    out += ["*" + " · ".join(meta) + "*", ""]
    for i, call, tools in _steps(trace, calls):
        out += [f"## Iteration {i}", ""]
        if call:
            out += [f"Model: {call_summary(call)}", ""]
        for t in tools:
            dur = f" · {_secs(t['duration_ms'])}" if t.get("duration_ms") is not None else ""
            out += [f"### {t.get('name') or t.get('label', '')}{dur}", ""]
            out += [_fence(json.dumps(t.get("input") or {}, ensure_ascii=False, indent=2), "json"), ""]
            full = t.get("output_full", t.get("output", ""))
            out += [f"Result ({len(full):,} chars):".replace(",", " "), "", _fence(full or "(empty)"), ""]
    return "\n".join(out).rstrip() + "\n"


def _content_md(content) -> list[str]:
    """One message's content: text as is, tool calls and results as fences."""
    if isinstance(content, str):
        return [content, ""] if content else []
    out: list[str] = []
    for b in content or []:
        if not isinstance(b, dict):
            out += [str(b), ""]
            continue
        kind = b.get("type")
        if kind == "text":
            out += [b.get("text", ""), ""]
        elif kind == "tool_use":
            out += [f"**Tool call** `{b.get('name')}` ({b.get('id', '')})", "",
                    _fence(json.dumps(b.get("input") or {}, ensure_ascii=False, indent=2), "json"), ""]
        elif kind == "tool_result":
            c = b.get("content")
            text = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False, indent=2)
            out += [f"**Tool result** ({b.get('tool_use_id', '')})", "", _fence(text), ""]
        else:
            out += [_fence(json.dumps(b, ensure_ascii=False, indent=2), "json"), ""]
    return out


def transcript_markdown(system: str, tools: list[dict], messages: list[dict], *, model: str = "",
                        now: datetime | None = None) -> str:
    """The raw request as Markdown — both message shapes (Anthropic content
    blocks, OpenAI `tool_calls` / role `tool`)."""
    out = ["# Raw conversation", ""]
    meta = [f"{(now or datetime.now()):%Y-%m-%d %H:%M}"]
    if model:
        meta.append(f"model {model}")
    meta.append(f"{len(messages)} messages, {len(tools)} tools")
    out += ["*" + " · ".join(meta) + "*", "", "## System prompt", "", _fence(system or ""), ""]
    for n, m in enumerate(messages, 1):
        role = m.get("role", "?")
        head = f"## {n}. {role}"
        if role == "tool":
            head += f" ({m.get('tool_call_id', '')})"
        out += [head, ""]
        if role == "tool":
            out += [_fence(str(m.get("content") or "")), ""]
        else:
            out += _content_md(m.get("content"))
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    pass
            out += [f"**Tool call** `{fn.get('name')}` ({tc.get('id', '')})", "",
                    _fence(json.dumps(args, ensure_ascii=False, indent=2), "json"), ""]
    out += ["## Tool schemas", "", _fence(json.dumps(tools, ensure_ascii=False, indent=2), "json"), ""]
    return "\n".join(out).rstrip() + "\n"


def build_dialog(trace: list[dict], calls: list[dict], transcript, *, model: str = "", question: str = ""):
    """The dialog; the caller places it (a bubble) and opens it."""
    from i18n import get_translator

    _ = get_translator()
    tot = totals(trace, calls)

    def _dl_trace() -> None:
        ui.download(trace_markdown(trace, calls, model=model, question=question).encode("utf-8"),
                    _("tool-history.md"), media_type="text/markdown")

    def _dl_raw() -> None:
        md = transcript_markdown(transcript.system, transcript.tools, transcript.messages, model=model)
        ui.download(md.encode("utf-8"), _("raw-conversation.md"), media_type="text/markdown")

    with ui.dialog() as dlg, ui.card().style("background:var(--c-surface); width:min(760px, 95vw);"):
        with ui.row().classes("items-center justify-between w-full no-wrap mb-1"):
            ui.label(_("Tool history")).classes("text-sm font-semibold text-gray-100")
            with ui.row().classes("items-center gap-1 no-wrap"):
                ui.button(icon="download", color=None, on_click=_dl_trace).props(
                    "flat dark dense").classes("text-gray-400").tooltip(_("Download tool history (.md)"))
                if transcript is not None:
                    ui.button(icon="description", color=None, on_click=_dl_raw).props(
                        "flat dark dense").classes("text-gray-400").tooltip(
                        _("Download raw conversation with system prompt, without thinking (.md)"))
                ui.button(icon="close", color=None, on_click=dlg.close).props(
                    "flat dark dense").classes("text-gray-400")
        ui.label(
            _("{calls} model requests ({model_s}) · {tools} tool calls ({tool_s}) · "
              "tokens in {tin} / out {tout}").format(
                calls=tot["calls"], model_s=_secs(tot["model_ms"]), tools=tot["tools"],
                tool_s=_secs(tot["tool_ms"]), tin=f"{tot['input']:,}".replace(",", " "),
                tout=f"{tot['output']:,}".replace(",", " "))
        ).classes("text-xs text-gray-500")
        ui.separator()
        with ui.column().style("max-height:65vh; overflow-y:auto; gap:8px; margin-top:8px; width:100%;"):
            for i, call, tools in _steps(trace, calls):
                with ui.row().classes("items-center gap-2 w-full no-wrap"):
                    ui.badge(f"Iter {i}", color="primary").classes("text-xs")
                    if call:
                        ui.label(call_summary(call)).classes("text-xs text-gray-500 min-w-0").style(
                            "overflow-wrap:anywhere")
                for step in tools:
                    _step_card(step, _)
    return dlg


def _step_card(step: dict, _) -> None:
    with ui.card().style("background:var(--c-bg); width:100%; padding:10px;"):
        with ui.row().classes("items-center gap-2 mb-1 no-wrap"):
            ui.label(step["label"].rstrip(".")).classes("text-xs text-gray-300 font-medium")
            if step.get("duration_ms") is not None:
                ui.label(_secs(step["duration_ms"])).classes("text-xs text-gray-500")
        inp = step.get("input")
        if inp:
            ui.label(json.dumps(inp, ensure_ascii=False, indent=2)).classes(
                "text-xs text-gray-500 font-mono whitespace-pre-wrap").style("overflow-wrap:anywhere")
        else:
            ui.label(_("(no parameters)")).classes("text-xs text-gray-600 italic")
        full = step.get("output_full", step.get("output", ""))
        if not full:
            return
        ui.separator().style("margin:4px 0;")
        exp = ui.expansion(
            _("Result · {n} characters").format(n=f"{len(full):,}".replace(",", " "))
        ).props("dense dark").classes("w-full text-xs text-gray-400")
        shown = [False]

        def _open(e, exp=exp, full=full) -> None:
            if e.value and not shown[0]:
                shown[0] = True
                with exp:
                    ui.label(full).classes(
                        "text-xs text-green-500 font-mono whitespace-pre-wrap").style("overflow-wrap:anywhere")

        exp.on_value_change(_open)
