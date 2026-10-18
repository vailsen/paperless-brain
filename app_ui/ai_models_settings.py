"""Settings > AI models: services (base URL + key) and the models each one hosts.

One entry per *service* — "Ollama local", "OpenRouter", "Claude" — holding the
address, the key and the concurrency lane once, with any number of models below
it. Storage and migration live in ``services/model_registry.py``; this is only
the editor.

Model display names are unique across all services because every setting that
picks a model stores that name. Renaming a model therefore also re-points the
choices that named it (chat, ingestion, memo cleanup, dreaming) — otherwise a
rename would silently unset them.
"""

from __future__ import annotations

import copy

from nicegui import ui

from i18n import get_translator
from services import model_registry as reg


def _field(label: str, placeholder: str = "", password: bool = False) -> ui.input:
    kw: dict = dict(label=label, placeholder=placeholder)
    if password:
        kw["password"] = True
        kw["password_toggle_button"] = True
    return ui.input(**kw).props("outlined dark dense").classes("w-full")


def _hint(html: str) -> None:
    with ui.element("div").classes(
        "bg-gray-900 border border-gray-700 rounded p-3 text-xs text-gray-400 mt-1 mb-3"
    ):
        ui.html(html, sanitize=False)


_ROW_CSS = """
.ai-prov { min-width:0; max-width:100%; }
.ai-prov-head, .ai-model-row { min-width:0; max-width:100%; }
.ai-ellipsis { overflow:hidden; white-space:nowrap; text-overflow:ellipsis; }
.ai-url { font-family: monospace; overflow-wrap:anywhere; word-break:break-all; }
"""


def _repoint_model_choices(username: str, token: str, old: str, new: str) -> None:
    """After a rename: everything that stored ``old`` now stores ``new``."""
    if not old or old == new:
        return
    reg.rename_references(username, token, old, new)
    try:
        from services.user_prefs import get_pref, set_pref

        if get_pref("chat_model_name", "") == old:
            set_pref("chat_model_name", new)
    except Exception:
        pass
    try:
        from werkbank import settings_store as ws

        if ws.get_ingest_model_name() == old:
            ws.set_value(ws.INGEST_MODEL_NAME, new)
    except Exception:
        pass


def ai_models_setting(username: str, token: str) -> None:
    _ = get_translator()
    ui.add_css(_ROW_CSS)

    backend_label = {
        "anthropic": _("Anthropic-compatible"),
        "openai_compatible": _("OpenAI-compatible"),
    }

    def _load() -> list[dict]:
        return copy.deepcopy(reg.get_providers(username, token))

    def _save(providers: list[dict]) -> None:
        reg.save_providers(username, token, providers)
        provider_list.refresh()

    # ── Service dialog ────────────────────────────────────────────────────────

    def _open_provider_dialog(edit: dict | None = None) -> None:
        is_edit = edit is not None
        with ui.dialog().props("persistent") as dlg, ui.card().classes("bg-gray-900").style(
            "width:min(95vw,480px)"
        ):
            ui.label(_("Edit service") if is_edit else _("Add service")).classes(
                "text-base font-semibold text-gray-100 mb-3"
            )
            f_backend = ui.select(
                {"openai_compatible": _("OpenAI-compatible"), "anthropic": _("Anthropic-compatible")},
                label=_("Backend"),
                value=(edit or {}).get("backend", "openai_compatible"),
            ).props("outlined dark dense").classes("w-full")
            f_base_url = _field(_("Base URL"), "http://192.168.1.x:11434/v1")
            f_base_url.classes("mt-2").set_value((edit or {}).get("base_url", ""))
            f_name = _field(_("Display name"), _("e.g. Ollama local, OpenRouter, Claude"))
            f_name.classes("mt-2")
            f_name.set_value("" if (edit or {}).get("auto_named") else (edit or {}).get("name", ""))
            if (edit or {}).get("auto_named"):
                f_name.props(f'placeholder="{edit.get("name", "")}"')
            f_api_key = _field(_("API key"), _("sk-… (leave empty if not needed)"), password=True)
            f_api_key.classes("mt-2").set_value((edit or {}).get("api_key", ""))
            f_lane = ui.select(
                {"api": _("API (several in parallel)"), "local": _("Local (one request at a time)")},
                label=_("Concurrency lane"),
                value=(edit or {}).get("lane", "api"),
            ).props("outlined dark dense").classes("w-full mt-2")
            ui.label(
                _(
                    "The address of the service, not of an endpoint: without "
                    "/chat/completions or /messages — that part is added automatically."
                )
            ).classes("text-xs text-gray-500 mt-1")

            def _placeholder() -> None:
                ph = {
                    "anthropic": _("https://api.anthropic.com (empty = default)"),
                    "openai_compatible": "http://192.168.1.x:11434/v1",
                }.get(f_backend.value, "")
                f_base_url.props(f'placeholder="{ph}"')

            _placeholder()
            f_backend.on_value_change(lambda _e: _placeholder())

            def _submit() -> None:
                backend = f_backend.value
                raw_url = (f_base_url.value or "").strip()
                base_url = reg.normalize_base_url(raw_url, backend)
                if backend == "openai_compatible" and not base_url:
                    ui.notify(_("An OpenAI-compatible service needs a base URL."), type="warning")
                    return
                name = (f_name.value or "").strip() or reg.suggest_provider_name(backend, base_url)
                providers = _load()
                if any(
                    p["name"].casefold() == name.casefold() and p["id"] != (edit or {}).get("id")
                    for p in providers
                ):
                    ui.notify(_("A service with this name already exists."), type="warning")
                    return
                fields = {
                    "name": name,
                    "backend": backend,
                    "base_url": base_url,
                    "api_key": (f_api_key.value or "").strip(),
                    "lane": f_lane.value,
                    "auto_named": False,
                }
                if is_edit:
                    for p in providers:
                        if p["id"] == edit["id"]:
                            p.update(fields)
                else:
                    providers.append({**reg.new_provider(name), **fields})
                _save(providers)
                dlg.close()
                if base_url != raw_url.rstrip("/"):
                    ui.notify(
                        _("Base URL shortened to {url} — the endpoint is added automatically.").format(
                            url=base_url
                        ),
                        type="info",
                    )
                else:
                    ui.notify(_("Service saved."), type="positive")

            with ui.row().classes("w-full justify-end gap-2 mt-4"):
                ui.button(_("Cancel"), on_click=dlg.close).props("flat dark").classes("text-gray-400")
                ui.button(_("Save"), icon="save", on_click=_submit).props("unelevated dark").classes(
                    "bg-purple-700 text-white"
                )
        dlg.open()

    # ── Model dialog ──────────────────────────────────────────────────────────

    def _open_model_dialog(provider: dict, edit: dict | None = None) -> None:
        is_edit = edit is not None
        e = edit or {}
        with ui.dialog().props("persistent") as dlg, ui.card().classes("bg-gray-900").style(
            "width:min(95vw,480px)"
        ):
            ui.label(_("Edit model") if is_edit else _("Add model")).classes(
                "text-base font-semibold text-gray-100"
            )
            ui.label(provider.get("name", "")).classes("text-xs text-gray-500 mb-3")

            f_model = _field(_("Model ID"), _("e.g. qwen3:32b or claude-sonnet-4-6"))
            f_model.set_value(e.get("model", ""))
            f_name = _field(_("Display name"), _("empty = the model ID"))
            f_name.classes("mt-2")
            f_name.set_value(e.get("name", "") if is_edit else "")

            def _name_placeholder() -> None:
                f_name.props(f'placeholder="{(f_model.value or "").strip() or _("empty = the model ID")}"')

            _name_placeholder()
            f_model.on_value_change(lambda _e: _name_placeholder())

            f_temperature = ui.number(
                label=_("Temperature (0.0 precise – 1.5 creative)"),
                value=float(e.get("temperature", 0.3)),
                min=0.0, max=2.0, step=0.05, format="%.2f",
            ).props("outlined dark dense").classes("w-full mt-2")
            f_max_output = ui.number(
                label=_("Max. output tokens (thinking + answer, 0 = default 16384)"),
                value=int(e.get("max_output_tokens", 0)),
                min=0, step=1024, format="%.0f",
            ).props("outlined dark dense").classes("w-full mt-2")

            think = e.get("think")
            f_think = ui.select(
                {"auto": _("Auto (model default)"), "true": _("Thinking ON"), "false": _("Thinking OFF")},
                label=_("Thinking mode"),
                value="true" if think is True else ("false" if think is False else "auto"),
            ).props("outlined dark dense").classes("w-full mt-2")
            ui.label(
                _(
                    "\"Auto\" leaves the decision to the model — Claude then never "
                    "thinks, and other models only sometimes. Set it explicitly to "
                    "decide yourself."
                )
            ).classes("text-xs text-gray-500 mt-1")

            # Both thinking controls are Anthropic-only, and which one applies is
            # decided by the service: first party takes `effort` and rejects a
            # budget, an Anthropic-*compatible* endpoint takes the budget and
            # knows nothing about effort. Only the one that can be sent is shown.
            anthropic = provider.get("backend") == "anthropic"
            first_party = not (provider.get("base_url") or "").strip()
            f_effort = ui.select(
                {
                    "": _("Model default"),
                    "low": _("Low"),
                    "medium": _("Medium"),
                    "high": _("High"),
                    "xhigh": _("Very high"),
                    "max": _("Maximum"),
                },
                label=_("Thinking effort (Anthropic, no base URL)"),
                value=str(e.get("effort", "")),
            ).props("outlined dark dense").classes("w-full mt-2")
            f_effort.set_visibility(anthropic and first_party)
            f_thinking_budget = ui.number(
                label=_("Thinking budget in tokens (0 = 4096)"),
                value=int(e.get("thinking_budget", 0)),
                min=0, step=1024, format="%.0f",
            ).props("outlined dark dense").classes("w-full mt-2")
            f_thinking_budget.set_visibility(anthropic and not first_party)
            ui.label(
                _("⚠ Qwen3 thinking-loop risk at temp. < 0.5 — recommended: ≥ 0.6")
            ).classes("text-xs text-yellow-500 mt-1")

            # "OpenAI-compatible" says nothing about whether a model actually
            # calls tools. A model that answers a question about the user's own
            # mail from its weights invents an answer that reads exactly like a
            # real one, so this is worth its own control.
            f_supports_tools = ui.switch(
                _("Model can use tools"), value=bool(e.get("supports_tools", True))
            ).props("dark").classes("mt-3")
            ui.label(
                _(
                    "Off: this model is never offered tools. Use for models that "
                    "cannot call them reliably — better a model that says it does "
                    "not know than one that invents an answer."
                )
            ).classes("text-xs text-gray-500 mt-1")
            f_force_tool = ui.switch(
                _("Require a tool for personal questions"),
                value=bool(e.get("force_tool_first_turn", False)),
            ).props("dark").classes("mt-2")
            ui.label(
                _(
                    "For questions about your own documents, emails or purchases, "
                    "the first request already demands a tool call. Switch on for "
                    "models that answer such questions from memory."
                )
            ).classes("text-xs text-gray-500 mt-1")

            def _submit() -> None:
                model_id = (f_model.value or "").strip()
                if not model_id:
                    ui.notify(_("Model ID must not be empty."), type="warning")
                    return
                name = (f_name.value or "").strip() or model_id
                providers = _load()
                if reg.name_taken(providers, name, except_model_id=e.get("id", "")):
                    ui.notify(
                        _("Another model is already called \"{name}\" — choose a different display name.").format(
                            name=name
                        ),
                        type="warning",
                    )
                    return
                fields = {
                    "name": name,
                    "model": model_id,
                    "temperature": float(f_temperature.value or 0.3),
                    "max_output_tokens": int(f_max_output.value or 0),
                    "think": True if f_think.value == "true" else (False if f_think.value == "false" else None),
                    "thinking_budget": int(f_thinking_budget.value or 0),
                    "effort": f_effort.value or "",
                    "supports_tools": bool(f_supports_tools.value),
                    "force_tool_first_turn": bool(f_force_tool.value),
                }
                for p in providers:
                    if p["id"] != provider["id"]:
                        continue
                    if is_edit:
                        for m in p.get("models") or []:
                            if m["id"] == e["id"]:
                                m.update(fields)
                    else:
                        p.setdefault("models", []).append({**reg.new_model(name, model_id), **fields})
                _save(providers)
                if is_edit:
                    _repoint_model_choices(username, token, e.get("name", ""), name)
                dlg.close()
                ui.notify(_("Model saved."), type="positive")

            with ui.row().classes("w-full justify-end gap-2 mt-4"):
                ui.button(_("Cancel"), on_click=dlg.close).props("flat dark").classes("text-gray-400")
                ui.button(_("Save"), icon="save", on_click=_submit).props("unelevated dark").classes(
                    "bg-purple-700 text-white"
                )
        dlg.open()

    # ── Ordering / deleting ───────────────────────────────────────────────────

    def _move_provider(pid: str, step: int) -> None:
        providers = _load()
        i = next((k for k, p in enumerate(providers) if p["id"] == pid), None)
        if i is None or not 0 <= i + step < len(providers):
            return
        providers[i], providers[i + step] = providers[i + step], providers[i]
        _save(providers)

    def _move_model(pid: str, mid: str, step: int) -> None:
        providers = _load()
        for p in providers:
            if p["id"] != pid:
                continue
            ms = p.get("models") or []
            i = next((k for k, m in enumerate(ms) if m["id"] == mid), None)
            if i is not None and 0 <= i + step < len(ms):
                ms[i], ms[i + step] = ms[i + step], ms[i]
        _save(providers)

    def _delete_model(pid: str, mid: str) -> None:
        providers = _load()
        for p in providers:
            if p["id"] == pid:
                p["models"] = [m for m in p.get("models") or [] if m["id"] != mid]
        _save(providers)
        ui.notify(_("Model deleted."), type="info")

    def _confirm_delete_provider(p: dict) -> None:
        n = len(p.get("models") or [])
        with ui.dialog() as dlg, ui.card().classes("bg-gray-900").style("width:min(92vw,420px)"):
            ui.label(_("Delete service \"{name}\"?").format(name=p.get("name", ""))).classes(
                "text-base font-semibold text-gray-100"
            )
            # TODO i18n-plural
            ui.label(
                _("Its {n} model(s) are deleted with it. Settings that use one of them lose their choice.").format(n=n)
                if n else _("It has no models.")
            ).classes("text-sm text-gray-400")
            with ui.row().classes("w-full justify-end gap-2 mt-3"):
                ui.button(_("Cancel"), on_click=dlg.close).props("flat dense")

                def _do() -> None:
                    _save([x for x in _load() if x["id"] != p["id"]])
                    dlg.close()
                    ui.notify(_("Service deleted."), type="info")

                ui.button(_("Delete"), on_click=_do).props("unelevated dense color=red")
        dlg.open()

    # ── Header ────────────────────────────────────────────────────────────────

    with ui.row().classes("w-full items-center justify-between mb-1 no-wrap"):
        with ui.row().classes("items-center gap-3 no-wrap min-w-0"):
            ui.icon("smart_toy", size="sm").classes("text-gray-400")
            with ui.column().classes("gap-0 min-w-0"):
                ui.label(_("AI models")).classes("text-base font-semibold text-gray-100")
                ui.label(_("Services and their models — order = dropdown order")).classes(
                    "text-xs text-gray-500"
                )
        ui.button(_("Service"), icon="add", on_click=lambda: _open_provider_dialog()).props(
            "unelevated dark dense"
        ).classes("bg-purple-700 text-white flex-shrink-0")
    ui.separator().classes("mb-3")

    def _menu_button(entries: list[tuple[str, str, object, bool]]) -> None:
        """A ⋮ button with (icon, label, handler, enabled) entries.

        One button instead of four keeps a phone row wide enough for the name.
        """
        with ui.button(icon="more_vert", color=None).props("flat dense round").classes(
            "flex-shrink-0"
        ).style("color:var(--c-text-2)"):
            with ui.menu().props("auto-close"):
                for icon, label, handler, enabled in entries:
                    with ui.menu_item(on_click=handler).props(
                        "" if enabled else "disable"
                    ).classes("items-center gap-3"):
                        ui.icon(icon, size="xs").style("color:var(--c-text-2)")
                        ui.label(label)

    @ui.refreshable
    def provider_list() -> None:
        providers = reg.get_providers(username, token)
        if not providers:
            ui.label(_("No models configured yet.")).classes("text-xs text-gray-500 py-2")
            return
        if any(p.get("auto_named") for p in providers):
            with ui.element("div").classes("w-full rounded px-3 py-2 mb-2 text-xs").style(
                "color:var(--c-warn);background:var(--c-warn-bg);"
            ):
                ui.label(
                    _(
                        "Your models are now grouped by service — one entry per address. "
                        "The names marked \"suggested\" were made up from the address: "
                        "open each one and give it a name."
                    )
                ).classes("w-full")

        with ui.column().classes("w-full gap-2"):
            for pi, p in enumerate(providers):
                models = p.get("models") or []
                with ui.column().classes("w-full gap-0 rounded-lg bg-gray-900 px-3 py-2 ai-prov"):
                    # Service row — `w-full` on every label: inside ui.column()
                    # (align-items: flex-start) that is what gives the ellipsis
                    # something to truncate against.
                    with ui.row().classes("w-full items-start gap-1 no-wrap ai-prov-head"):
                        with ui.column().classes("flex-1 gap-0 min-w-0"):
                            ui.label(p.get("name", "")).classes(
                                "w-full text-sm font-semibold text-gray-100 ai-ellipsis"
                            )
                            with ui.row().classes("w-full items-center gap-2 no-wrap min-w-0"):
                                if p.get("auto_named"):
                                    ui.label(_("suggested")).classes(
                                        "text-xs px-1.5 rounded flex-shrink-0"
                                    ).style("color:var(--c-warn);background:var(--c-warn-bg);")
                                meta = backend_label.get(p.get("backend", ""), p.get("backend", ""))
                                meta += " · " + (_("local") if p.get("lane") == "local" else "API")
                                ui.label(meta).classes("text-xs text-gray-500 ai-ellipsis min-w-0")
                            if p.get("base_url"):
                                ui.label(p["base_url"]).classes("w-full text-xs text-gray-500 ai-url")
                        _menu_button([
                            ("add", _("Add model"), lambda prov=p: _open_model_dialog(prov), True),
                            ("edit", _("Edit service"), lambda prov=p: _open_provider_dialog(prov), True),
                            ("arrow_upward", _("Move up"), lambda pid=p["id"]: _move_provider(pid, -1), pi > 0),
                            ("arrow_downward", _("Move down"), lambda pid=p["id"]: _move_provider(pid, 1),
                             pi < len(providers) - 1),
                            ("delete", _("Delete"), lambda prov=p: _confirm_delete_provider(prov), True),
                        ])

                    # Its models
                    if not models:
                        with ui.row().classes("w-full items-center gap-2 pl-3 pt-1"):
                            ui.label(_("No models yet.")).classes("text-xs text-gray-500")
                            ui.button(
                                _("Add model"), icon="add", color=None,
                                on_click=lambda _e, prov=p: _open_model_dialog(prov),
                            ).props("flat dense no-caps").classes("text-xs").style("color:var(--c-text-2)")
                    for mi, m in enumerate(models):
                        with ui.row().classes(
                            "w-full items-center gap-1 no-wrap pl-3 py-1 ai-model-row cursor-pointer"
                        ).style("border-top:1px solid var(--c-border);").on(
                            "click", lambda _e, prov=p, mod=m: _open_model_dialog(prov, mod)
                        ):
                            with ui.column().classes("flex-1 gap-0 min-w-0"):
                                ui.label(m.get("name", "")).classes(
                                    "w-full text-sm text-gray-200 ai-ellipsis"
                                )
                                if m.get("model") and m.get("model") != m.get("name"):
                                    ui.label(m["model"]).classes(
                                        "w-full text-xs text-gray-500 font-mono ai-ellipsis"
                                    )
                            # A model without tools cannot see any of the user's
                            # data. Saying so here beats letting it answer
                            # plausibly and wrongly in chat.
                            if not m.get("supports_tools", True):
                                ui.label(_("no tools")).classes(
                                    "text-xs flex-shrink-0 px-1.5 rounded"
                                ).style("color:var(--c-warn);background:var(--c-warn-bg);").tooltip(
                                    _("Limited — cannot search documents, emails or notes.")
                                )
                            with ui.element("div").on("click.stop", lambda _e: None):
                                _menu_button([
                                    ("edit", _("Edit model"), lambda prov=p, mod=m: _open_model_dialog(prov, mod), True),
                                    ("arrow_upward", _("Move up"),
                                     lambda pid=p["id"], mid=m["id"]: _move_model(pid, mid, -1), mi > 0),
                                    ("arrow_downward", _("Move down"),
                                     lambda pid=p["id"], mid=m["id"]: _move_model(pid, mid, 1), mi < len(models) - 1),
                                    ("delete", _("Delete"),
                                     lambda pid=p["id"], mid=m["id"]: _delete_model(pid, mid), True),
                                ])

    provider_list()

    _hint(
        _(
            "One entry per service, with any number of models below it. Models appear in "
            "dropdowns in this order, grouped by service.<br><b>OpenAI-compatible</b>: Ollama "
            "(<code>http://host:11434/v1</code>), OpenRouter (<code>https://openrouter.ai/api/v1</code>), "
            "MiniMax, Moonshot, …<br><b>Anthropic-compatible</b>: Anthropic API (empty base URL), "
            "MiniMax and other providers with an Anthropic-compatible endpoint."
        )
    )
