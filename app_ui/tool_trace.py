"""The tool history of one chat answer: dialog and two downloads.

- **Tool history (.md)** — every model request (tokens, time to the first
  token, total time) and every tool call (input, duration, the whole result).
- **Raw conversation (.md)** — what the model was sent on its last request:
  system prompt, tool schemas and every message of the loop, its answer
  appended. Thinking is never in it (`chat_service._transcript` removes it).

The dialog lists the steps on the left and shows one on the right, so only
the chosen step's result reaches the browser: a 16-step turn of document
details and fetched pages is half a megabyte, and sending all of it up front
made the dialog slow on a phone. It is built when the turn starts and grows
while the model works (`TraceView.update`). Both live only as long as the
page — nothing here is stored.
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


def step_list(trace: list[dict], calls: list[dict], *, running: bool = False,
              iteration: int = 0) -> list[dict]:
    """The steps the dialog lists, in order: each model request, then the tools
    it asked for. While a turn runs, the request being made now is a step too
    (``running``), and so is a tool without a result yet.

    Several tools under one request are not several iterations: the model
    asked for all of them in one answer (parallel tool use); they run, and
    the next request sees all their results at once.
    """
    out: list[dict] = []
    seen_iters = set()
    for i, call, tools in _steps(trace, calls):
        seen_iters.add(i)
        out.append({"kind": "model", "key": f"m{i}", "iter": i, "call": call, "n_tools": len(tools),
                    "running": call is None and running})
        for k, t in enumerate(tools):
            out.append({"kind": "tool", "key": f"t{i}.{k}", "iter": i, "step": t,
                        "running": running and t.get("duration_ms") is None})
    if running and iteration and iteration not in seen_iters:
        out.append({"kind": "model", "key": f"m{iteration}", "iter": iteration, "call": None,
                    "n_tools": 0, "running": True})
    return out


_CSS = """
.pb-tt{display:grid;grid-template-columns:minmax(200px,34%) 1fr;gap:10px;min-height:0;flex:1}
.pb-tt-nav{overflow-y:auto;min-height:0;display:flex;flex-direction:column;gap:2px;padding-right:2px}
.pb-tt-item{display:flex;flex-direction:column;gap:3px;padding:6px 8px;border-radius:8px;cursor:pointer;
  min-width:0;color:var(--c-text-2)}
.pb-tt-item:hover{background:var(--c-surface-3)}
.pb-tt-item.is-sel{background:var(--c-accent-soft);color:var(--c-text)}
.pb-tt-item.is-tool{margin-left:14px}
.pb-tt-row{display:flex;align-items:center;gap:6px;min-width:0;font-size:.78rem}
.pb-tt-name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pb-tt-dur{flex-shrink:0;font-size:.7rem;color:var(--c-text-muted);font-variant-numeric:tabular-nums}
.pb-tt-bar{height:3px;border-radius:2px;background:var(--c-text-muted);opacity:.45;min-width:2px}
.pb-tt-detail{overflow-y:auto;min-height:0;min-width:0;display:flex;flex-direction:column;gap:8px}
.pb-tt-k{font-size:.7rem;color:var(--c-text-muted)}
.pb-tt-grid{display:grid;grid-template-columns:auto 1fr;gap:2px 12px;font-size:.8rem;color:var(--c-text-2)}
.pb-tt-pre{font-family:ui-monospace,monospace;font-size:.72rem;white-space:pre-wrap;overflow-wrap:anywhere;
  background:var(--c-bg);border-radius:6px;padding:8px;color:var(--c-text-2);margin:0}
.pb-tt-pre.is-out{max-height:none}
.pb-tt-back{display:none}
@media (max-width:640px){
  .pb-tt{grid-template-columns:1fr}
  .pb-tt.show-detail .pb-tt-nav{display:none}
  .pb-tt:not(.show-detail) .pb-tt-detail{display:none}
  .pb-tt-back{display:inline-flex}
}
"""


class TraceView:
    """The tool history of one answer: steps on the left, the chosen step on
    the right (one under the other on a phone, with a back button).

    It holds the turn's own lists, so it can be opened while the answer is
    still being written: `update()` after every event redraws it when open.
    Until the user picks a step, the newest one is shown.
    """

    def __init__(self, trace: list[dict], calls: list[dict], transcript=None, *,
                 model: str = "", question: str = ""):
        from i18n import get_translator

        self._ = get_translator()
        self.trace, self.calls = trace, calls
        self.transcript = transcript      # callable → TranscriptEvent | None, or the event
        self.model, self.question = model, question
        self.running = False
        self.iteration = 0
        self.sel: str | None = None       # None: follow the newest step
        self.phone_detail = False
        self._sig = None
        self._build()

    # ── public ───────────────────────────────────────────────────────────────
    def open(self) -> None:
        self._sig = None
        self.update()
        self.dlg.open()

    def update(self) -> None:
        """Redraw if open and something changed (cheap to call per event)."""
        if not self.dlg.value:
            return
        sig = (len(self.trace), len(self.calls), self.running, self.iteration, self.sel, self.phone_detail,
               sum(1 for t in self.trace if t.get("duration_ms") is not None))
        if sig == self._sig:
            return
        self._sig = sig
        self._head.refresh()
        self._nav.refresh()
        self._detail.refresh()

    def _tx(self):
        return self.transcript() if callable(self.transcript) else self.transcript

    # ── layout ───────────────────────────────────────────────────────────────
    def _build(self) -> None:
        _ = self._
        ui.add_css(_CSS)

        def _dl_trace() -> None:
            ui.download(trace_markdown(self.trace, self.calls, model=self.model,
                                       question=self.question).encode("utf-8"),
                        _("tool-history.md"), media_type="text/markdown")

        def _dl_raw() -> None:
            tx = self._tx()
            if tx is None:
                ui.notify(_("Available when the answer is finished."), timeout=1500)
                return
            md = transcript_markdown(tx.system, tx.tools, tx.messages, model=self.model)
            ui.download(md.encode("utf-8"), _("raw-conversation.md"), media_type="text/markdown")

        with ui.dialog() as self.dlg, ui.card().classes("gap-2").style(
                "background:var(--c-surface); width:min(980px, 96vw); max-width:none; height:min(760px, 90vh);"
                "display:flex; flex-direction:column; flex-wrap:nowrap; padding:12px;"):
            with ui.row().classes("items-center justify-between w-full no-wrap"):
                ui.label(_("Tool history")).classes("text-sm font-semibold").style("color:var(--c-text)")
                with ui.row().classes("items-center gap-1 no-wrap"):
                    ui.button(icon="download", color=None, on_click=_dl_trace).props(
                        "flat dense round").classes("card-action-btn").tooltip(_("Download tool history (.md)"))
                    ui.button(icon="description", color=None, on_click=_dl_raw).props(
                        "flat dense round").classes("card-action-btn").tooltip(
                        _("Download raw conversation with system prompt, without thinking (.md)"))
                    ui.button(icon="close", color=None, on_click=self.dlg.close).props(
                        "flat dense round").classes("card-action-btn")
            self._head()
            self._grid = ui.element("div").classes("pb-tt w-full")
            with self._grid:
                self._nav()
                self._detail()

    @ui.refreshable_method
    def _head(self) -> None:
        _ = self._
        tot = totals(self.trace, self.calls)
        text = _("{calls} model requests ({model_s}) · {tools} tool calls ({tool_s}) · "
                 "tokens in {tin} / out {tout}").format(
            calls=tot["calls"], model_s=_secs(tot["model_ms"]), tools=tot["tools"],
            tool_s=_secs(tot["tool_ms"]), tin=f"{tot['input']:,}".replace(",", " "),
            tout=f"{tot['output']:,}".replace(",", " "))
        with ui.row().classes("items-center gap-2 w-full no-wrap"):
            if self.running:
                ui.spinner(size="xs").classes("pb-accent-text")
            ui.label(text).classes("text-xs min-w-0").style("color:var(--c-text-muted)")

    def _steps_now(self) -> list[dict]:
        return step_list(self.trace, self.calls, running=self.running, iteration=self.iteration)

    def _selected(self, steps: list[dict]) -> dict | None:
        if not steps:
            return None
        if self.sel:
            hit = next((s for s in steps if s["key"] == self.sel), None)
            if hit:
                return hit
        return steps[-1]

    def _pick(self, key: str) -> None:
        self.sel = key
        self.phone_detail = True
        self._grid.classes(add="show-detail")
        self.update()

    def _back(self) -> None:
        self.phone_detail = False
        self._grid.classes(remove="show-detail")
        self.update()

    @staticmethod
    def _ms(step: dict) -> int | None:
        if step["kind"] == "model":
            return (step["call"] or {}).get("total_ms")
        return step["step"].get("duration_ms")

    @ui.refreshable_method
    def _nav(self) -> None:
        _ = self._
        steps = self._steps_now()
        cur = self._selected(steps)
        longest = max([self._ms(s) or 0 for s in steps] + [1])
        with ui.element("div").classes("pb-tt-nav"):
            if not steps:
                ui.label(_("Waiting for the model …")).classes("text-xs").style("color:var(--c-text-muted)")
            for s in steps:
                cls = "pb-tt-item" + (" is-tool" if s["kind"] == "tool" else "") + (
                    " is-sel" if cur and s["key"] == cur["key"] else "")
                el = ui.element("div").classes(cls)
                el.on("click", lambda _e, k=s["key"]: self._pick(k))
                ms = self._ms(s)
                with el:
                    with ui.element("div").classes("pb-tt-row"):
                        if s["running"]:
                            ui.spinner(size="0.9em")
                        elif s["kind"] == "model":
                            ui.icon("psychology", size="1rem")
                        else:
                            ui.icon("build", size="0.9rem")
                        if s["kind"] == "model":
                            name = _("Model request {n}").format(n=s["iter"])
                            if s["n_tools"] > 1:
                                name += " · " + _("{n} tools at once").format(n=s["n_tools"])
                        else:
                            name = (s["step"].get("label") or s["step"].get("name") or "").rstrip(".")
                        ui.label(name).classes("pb-tt-name")
                        if ms is not None:
                            ui.label(_secs(ms)).classes("pb-tt-dur")
                    if ms:
                        ui.element("div").classes("pb-tt-bar").style(f"width:{max(2, 100 * ms / longest):.0f}%")

    @ui.refreshable_method
    def _detail(self) -> None:
        _ = self._
        steps = self._steps_now()
        cur = self._selected(steps)
        with ui.element("div").classes("pb-tt-detail"):
            ui.button(_("All steps"), icon="arrow_back", on_click=self._back).props(
                "flat dense no-caps").classes("pb-tt-back self-start")
            if cur is None:
                return
            if cur["kind"] == "model":
                self._model_detail(cur)
            else:
                self._tool_detail(cur)

    def _model_detail(self, s: dict) -> None:
        _ = self._
        ui.label(_("Model request {n}").format(n=s["iter"])).classes("text-sm font-semibold").style(
            "color:var(--c-text)")
        call = s["call"]
        if call is None:
            ui.label(_("The model is working on this request …") if s["running"]
                     else _("No timing reported for this request.")).classes("text-xs").style(
                "color:var(--c-text-muted)")
        else:
            gen_s = (call.get("total_ms", 0) - call.get("first_token_ms", 0)) / 1000
            prompt = (f"≈{call['estimated_prompt']:,} ({_('evaluated')} {call.get('input_tokens', 0):,})"
                      if call.get("estimated_prompt") else f"{call.get('input_tokens', 0):,}")
            rows = [
                (_("Prompt tokens"), prompt.replace(",", " ")),
                (_("Output tokens"), f"{call.get('output_tokens', 0):,}".replace(",", " ")),
                (_("First token after"), _secs(call.get("first_token_ms", 0))),
                (_("Total time"), _secs(call.get("total_ms", 0))),
            ]
            if call.get("cache_read") or call.get("cache_write"):
                rows.append((_("Cache read / written"),
                             f"{call.get('cache_read', 0):,} / {call.get('cache_write', 0):,}".replace(",", " ")))
            if call.get("output_tokens") and gen_s > 0.2:
                rows.append((_("Speed"), f"{call['output_tokens'] / gen_s:.0f} tok/s"))
            if call.get("stop"):
                rows.append((_("Ended with"), _("tool calls") if call["stop"] in ("tool_calls", "tool_use")
                             else call["stop"]))
            with ui.element("div").classes("pb-tt-grid"):
                for k, v in rows:
                    ui.label(k).classes("pb-tt-k")
                    ui.label(v)
        tools = [t for t in self.trace if t["iter"] == s["iter"]]
        if tools:
            ui.label(_("Asked for {n} tool(s) in this one answer:").format(n=len(tools))).classes(
                "text-xs mt-2").style("color:var(--c-text-muted)")
            for t in tools:
                ui.label("· " + (t.get("label") or t.get("name") or "").rstrip(".")).classes("text-xs")
            if len(tools) > 1:
                ui.label(_("Several tools in one request are one step: the model asked for all of them "
                           "at once, and its next request sees all their results together.")).classes(
                    "text-xs").style("color:var(--c-text-muted)")
        elif call and not s["running"]:
            ui.label(_("No tools — this request wrote the answer.")).classes("text-xs mt-2").style(
                "color:var(--c-text-muted)")

    def _tool_detail(self, s: dict) -> None:
        _ = self._
        t = s["step"]
        with ui.row().classes("items-center gap-2 no-wrap w-full"):
            ui.label((t.get("label") or "").rstrip(".")).classes("text-sm font-semibold min-w-0").style(
                "color:var(--c-text)")
            if t.get("duration_ms") is not None:
                ui.label(_secs(t["duration_ms"])).classes("pb-tt-dur")
        ui.label(_("Tool {name} · model request {n}").format(name=t.get("name", ""), n=t["iter"])).classes(
            "pb-tt-k")
        ui.label(_("Input")).classes("pb-tt-k mt-1")
        inp = t.get("input")
        ui.label(json.dumps(inp, ensure_ascii=False, indent=2) if inp else _("(no parameters)")).classes(
            "pb-tt-pre")
        full = t.get("output_full", t.get("output", ""))
        if s["running"]:
            with ui.row().classes("items-center gap-2"):
                ui.spinner(size="xs")
                ui.label(_("Running …")).classes("text-xs")
            return
        with ui.row().classes("items-center justify-between w-full no-wrap mt-1"):
            ui.label(_("Result · {n} characters").format(n=f"{len(full):,}".replace(",", " "))).classes("pb-tt-k")
            if full:
                ui.button(icon="content_copy", color=None, on_click=lambda f=full: (
                    ui.run_javascript(f"navigator.clipboard.writeText({json.dumps(f)})"),
                    ui.notify(_("Copied"), timeout=1000))).props("flat dense round size=sm").classes(
                    "card-action-btn").tooltip(_("Copy result"))
        ui.label(full or _("(empty)")).classes("pb-tt-pre is-out")


def build_dialog(trace: list[dict], calls: list[dict], transcript, *, model: str = "", question: str = ""):
    """The dialog of a finished turn (kept for callers that have no live view)."""
    return TraceView(trace, calls, transcript, model=model, question=question).dlg
