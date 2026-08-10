# app_ui/pages/settings.py
"""User settings page — LLM models, IMAP and CalDAV / iCal configuration."""

import asyncio

from nicegui import app as ng_app, ui

from app_ui.layout import page_layout, require_auth
from config.settings import settings
from i18n import DEFAULT_LANG, SUPPORTED_LANGUAGES, get_translator
from services.credential_store import load_credentials, save_credentials
from services.session_auth import get_session_token


# ── Language selector ───────────────────────────────────────────────────────────


def language_setting() -> None:
    """Render the language selector. Switching = write storage + reload."""
    current = ng_app.storage.user.get("language", DEFAULT_LANG)
    if current not in SUPPORTED_LANGUAGES:
        current = DEFAULT_LANG

    def on_change(e) -> None:
        ng_app.storage.user["language"] = e.value
        ui.navigate.reload()

    ui.select(
        options=SUPPORTED_LANGUAGES,
        value=current,
        label="Sprache / Language",
        on_change=on_change,
    ).props("outlined dark dense").classes("w-full")


def theme_setting() -> None:
    """Render the dark/light selector. Switching = write storage + reload."""
    _ = get_translator()
    current = ng_app.storage.user.get("theme", "dark")

    def on_change(e) -> None:
        ng_app.storage.user["theme"] = e.value
        ui.navigate.reload()

    ui.select(
        options={"dark": _("Dark"), "light": _("Light")},
        value=current,
        label=_("Appearance"),
        on_change=on_change,
    ).props("outlined dark dense").classes("w-full")


def push_text_setting() -> None:
    """Render the "push AI text to Paperless" switch. Off by default.

    Per user rather than global: it changes what lands in the shared archive,
    so it stays the explicit choice of whoever runs the sync.
    """
    _ = get_translator()

    def on_change(e) -> None:
        ng_app.storage.user["push_text_to_paperless"] = bool(e.value)

    ui.checkbox(
        _("Push AI extracted text to Paperless-ngx"),
        value=bool(ng_app.storage.user.get("push_text_to_paperless", False)),
        on_change=on_change,
    ).classes("text-sm text-gray-300")


def voice_memo_setting(username: str = "", token: str = "") -> None:
    """Voice-memo section. Inert with an explanation when no service is set up.

    Greyed out rather than hidden: a user who never sees the section cannot
    discover that the feature exists or what it would take to switch it on.
    """
    _ = get_translator()
    from app_ui.memo_dialog import (
        DICTATION_LANGUAGES,
        chat_mic_engine,
        memo_configured,
        set_chat_mic_engine,
        set_dictation_language,
        set_memo_enabled,
    )

    configured = memo_configured()

    _section_header(
        "mic",
        _("Voice memos"),
        _("Speak a memo; it is filed into your vault as Markdown"),
    )

    if not configured:
        with ui.element("div").style("opacity:.55; pointer-events:none;"):
            ui.checkbox(_("Enable voice memos"), value=False).classes(
                "text-sm text-gray-300"
            )
        _hint(
            _(
                "No transcription service is configured, so this feature is off. "
                "PaperlessBrain does not ship one — point <code>WHISPER_URL</code> in "
                "your <code>.env</code> at any service with an OpenAI-compatible "
                "<code>/v1/audio/transcriptions</code> endpoint (for example a "
                "self-hosted Whisper container), optionally with "
                "<code>WHISPER_API_KEY</code>, <code>WHISPER_MODEL</code> and "
                "<code>WHISPER_LANGUAGE</code>, then restart. "
                "<b>Ollama cannot do transcription</b> — it needs its own service. "
                "Recording also requires the app to be reachable over HTTPS or "
                "localhost; browsers block microphone access otherwise."
            )
        )
        return

    ui.checkbox(
        _("Enable voice memos"),
        value=bool(ng_app.storage.user.get("voice_memos_enabled", True)),
        on_change=lambda e: set_memo_enabled(e.value),
    ).classes("text-sm text-gray-300")
    _hint(
        _(
            "A microphone button appears in the header. Hold the record button and "
            "speak — or swipe up while holding to lock the recording and stop it "
            "with a tap. The recording is transcribed and tidied up by your "
            "selected AI model, and you review the text before anything is saved. "
            "Memos are stored as "
            "Markdown files in the <code>{folder}</code> folder of your vault and "
            "are searchable in chat. Switch the dialog to <b>Conversation</b> to "
            "transcribe a recorded dialog as speaker turns — that needs a "
            "transcription service with speaker diarization. "
            "Reload the page after changing this."
        ).format(folder=settings.memo_subfolder)
    )

    ui.select(
        options={"": _("Server default"), **DICTATION_LANGUAGES},
        value=ng_app.storage.user.get("dictation_language", ""),
        label=_("Dictation language"),
        on_change=lambda e: set_dictation_language(e.value),
    ).props("outlined dark dense").classes("w-full mt-3")
    _hint(
        _(
            "The language you <b>speak</b> — independent of the interface "
            "language, since dictating German into an English interface is a "
            "normal combination. Applies to both engines: it is sent to the "
            "transcription service and it sets the browser's recognition "
            "language, which otherwise defaults to English and drops every "
            "German word. <i>Server default</i> uses "
            "<code>WHISPER_LANGUAGE</code> from the installation's "
            "<code>.env</code> (currently: <code>{lang}</code>). "
            "Reload the page after changing this."
        ).format(lang=settings.whisper_language or "auto")
    )

    ui.select(
        options={"whisper": _("Transcription service"), "browser": _("Browser dictation")},
        value=chat_mic_engine(),
        label=_("Microphone in chat"),
        on_change=lambda e: set_chat_mic_engine(e.value),
    ).props("outlined dark dense").classes("w-full mt-3")
    _hint(
        _(
            "Which engine the microphone button in the chat input uses. The "
            "<b>transcription service</b> recognises far better, especially for "
            "names and numbers, but shows nothing until you release the button "
            "and the recording has been sent off. <b>Browser dictation</b> (Web "
            "Speech API) writes along while you speak and needs no round trip, "
            "but is noticeably less accurate — and Firefox does not offer it at "
            "all, in which case the transcription service is used anyway. "
            "Either way the chat mic inserts your words unchanged; only memos "
            "are tidied up by AI. Reload the page after changing this."
        )
    )

    if not (username and token):
        return

    from services.credential_store import load_credentials as _lc_memo
    from services.credential_store import save_credentials as _sc_memo
    from services.model_registry import get_models as _get_models_memo

    _memo_creds = _lc_memo(username, token)
    _cur_memo_model = _memo_creds.get("memo_model", "")
    _memo_opts = {
        m["name"]: m["name"]
        for m in _get_models_memo(username, token)
        if m.get("enabled", True)
    }
    _memo_sel = (
        ui.select(
            _memo_opts,
            label=_("Model for memo cleanup"),
            value=_cur_memo_model if _cur_memo_model in _memo_opts else None,
        )
        .props("outlined dark dense clearable")
        .classes("w-full mt-3")
    )
    _hint(
        _(
            "Rewrites the raw dictation into readable Markdown and names it. "
            "This is a short, strictly formatted job, so a small local model is "
            "usually enough — it does not have to be your chat model. "
            "<b>Without a model here the chat model is used; without that, the raw "
            "transcript is filed and the topic is taken from its first words.</b>"
        )
    )

    def _save_memo_model() -> None:
        c = _lc_memo(username, token)
        c["memo_model"] = _memo_sel.value or ""
        _sc_memo(username, token, c)
        ui.notify(_("Saved."), type="positive")

    ui.button(_("Save"), icon="save", on_click=_save_memo_model).props(
        "unelevated dark dense"
    ).classes("bg-purple-700 text-white mt-3")


# ── Tiny UI helpers ───────────────────────────────────────────────────────────


def _section_header(icon: str, title: str, subtitle: str) -> None:
    with ui.row().classes("items-center gap-3 mb-1"):
        ui.icon(icon, size="sm").classes("text-gray-400")
        with ui.column().classes("gap-0"):
            ui.label(title).classes("text-base font-semibold text-gray-100")
            ui.label(subtitle).classes("text-xs text-gray-500")
    ui.separator().classes("mb-4")


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


def _status_row() -> tuple[ui.label, ui.spinner]:
    with ui.row().classes("items-center gap-2 h-6"):
        lbl = ui.label("").classes("text-xs")
        spin = ui.spinner(size="xs").classes("text-purple-400")
        spin.set_visibility(False)
    return lbl, spin


def _show_status(
    lbl: ui.label, spin: ui.spinner, err: str, ok_text: str | None = None
) -> None:
    _ = get_translator()
    if ok_text is None:
        ok_text = _("Connection successful")
    spin.set_visibility(False)
    if err:
        lbl.set_text(_("Error: {err}").format(err=err))
        lbl.classes(remove="text-green-400", add="text-red-400")
        ui.notify(err, type="negative")
    else:
        lbl.set_text(ok_text)
        lbl.classes(remove="text-red-400", add="text-green-400")
        ui.notify(ok_text, type="positive")


# ── Page ──────────────────────────────────────────────────────────────────────


@ui.page("/settings")
async def settings_page() -> None:
    if not require_auth():
        return
    page_layout()
    _ = get_translator()

    username: str = ng_app.storage.user.get("paperless_user", "")
    token: str = get_session_token()
    creds = load_credentials(username, token)
    llm_cfg: dict = creds.get("llm", {})
    imap_cfg: dict = creds.get("imap", {})
    cal_cfg: dict = creds.get("calendar", {})
    sender_cfg: dict = creds.get("sender_profile", {})

    # Normalise saved iCal URLs (new list format or legacy single key)
    _saved_ical_urls: list[str] = cal_cfg.get("ical_urls") or []
    if not _saved_ical_urls and cal_cfg.get("ical_url"):
        _saved_ical_urls = [cal_cfg["ical_url"]]
    while len(_saved_ical_urls) < 3:
        _saved_ical_urls.append("")

    saved_cal_mode = "ical" if (_saved_ical_urls[0] or cal_cfg.get("ical_url")) else ("caldav" if cal_cfg.get("url") else "ical")

    with ui.column().classes("w-full max-w-2xl mx-auto p-6 gap-3"):
        ui.label(_("Settings")).classes("text-xl font-bold text-gray-100 mb-1")

        # ── Sections ──────────────────────────────────────────────────────────
        # Fifteen cards in one column put everything equally far away. The
        # groups are created here, up front; `_card()` then parents each card
        # into its group. Since a NiceGUI element's parent is fixed when it is
        # created, the sections below keep their original build order and code —
        # only the `with` line changes.
        _groups: dict[str, ui.expansion] = {}

        def _group(key: str, icon: str, title: str, subtitle: str, opened: bool = False) -> None:
            _groups[key] = (
                ui.expansion(
                    title, caption=subtitle, icon=icon, group="settings", value=opened
                )
                .props('expand-separator header-class="settings-group-header"')
                .classes("w-full settings-group")
            )

        def _card(group: str) -> ui.card:
            with _groups[group]:
                return ui.card().classes(
                    "w-full bg-gray-800 border border-gray-700 p-5 gap-0 mt-3"
                )

        _group("general", "tune", _("General"), _("Language and appearance"), opened=True)
        _group("ai", "smart_toy", _("AI"), _("Models, providers and deep research"))
        _group("docs", "description", _("Documents"), _("Processing, write-back and tags"))
        _group(
            "memory",
            "psychology",
            _("Search & memory"),
            _("Thresholds, memory maintenance and vault"),
        )
        _group("voice", "mic", _("Voice"), _("Voice memos and dictation"))
        _group("connect", "hub", _("Connections"), _("Email, calendar and sender profile"))
        _group("data", "backup", _("Backup"), _("Import and export your settings"))

        # ── Sprache / Language ─────────────────────────────────────────────────
        with _card("general"):
            _section_header(
                "translate",
                _("Language"),
                _("Display language of the interface"),
            )
            language_setting()

        # ── Erscheinungsbild / Theme ───────────────────────────────────────────
        with _card("general"):
            _section_header(
                "dark_mode",
                _("Appearance"),
                _("Light or dark theme"),
            )
            theme_setting()

        # ── Rückschreiben nach Paperless / push text back ─────────────────────
        with _card("docs"):
            _section_header(
                "sync_alt",
                _("Paperless-ngx write-back"),
                _("What PaperlessBrain may write into your archive"),
            )
            push_text_setting()
            _hint(
                _(
                    "After each sync, documents whose AI-extracted text differs from the "
                    "text in Paperless-ngx are updated there. This improves the Paperless "
                    "full-text search. <b style='color:#f87171'>The previous OCR text is "
                    "overwritten and cannot be restored by Paperless-ngx.</b> Reprocessing "
                    "a document in Paperless-ngx replaces the text with its own OCR again."
                )
            )

        # ── Sprachmemos / voice memos ─────────────────────────────────────────
        with _card("voice"):
            voice_memo_setting(username, token)

        # ── KI-Modelle (dynamic registry) ─────────────────────────────────────
        with _card("ai"):
            from services.model_registry import get_models, save_models, new_model

            _BACKEND_LABEL = {
                "anthropic":         _("Anthropic-compatible"),
                "openai_compatible": _("OpenAI-compatible"),
            }
            _BACKEND_COLOR = {
                "anthropic":         "text-orange-400 bg-orange-950",
                "openai_compatible": "text-blue-400 bg-blue-950",
            }

            with ui.row().classes("w-full items-center justify-between mb-1"):
                with ui.row().classes("items-center gap-3"):
                    ui.icon("smart_toy", size="sm").classes("text-gray-400")
                    with ui.column().classes("gap-0"):
                        ui.label(_("AI models")).classes("text-base font-semibold text-gray-100")
                        ui.label(_("Providers and models — order = dropdown order")).classes("text-xs text-gray-500")

                def _open_add_dialog(edit_model: dict | None = None):
                    is_edit = edit_model is not None
                    models  = get_models(username, token)

                    with ui.dialog().props("persistent") as dlg:
                        with ui.card().classes("bg-gray-900").style("width:min(95vw,480px)"):
                            ui.label(_("Edit model") if is_edit else _("Add model")).classes(
                                "text-base font-semibold text-gray-100 mb-3"
                            )

                            f_name = _field(_("Display name"), _("e.g. Qwen3 local"))
                            f_name.set_value(edit_model.get("name", "") if is_edit else "")

                            f_backend = ui.select(
                                {"openai_compatible": _("OpenAI-compatible"), "anthropic": _("Anthropic-compatible")},
                                label=_("Backend"),
                                value=edit_model.get("backend", "openai_compatible") if is_edit else "openai_compatible",
                            ).props("outlined dark dense").classes("w-full mt-2")

                            f_model = _field(_("Model ID"), _("e.g. qwen3:32b or claude-sonnet-4-6"))
                            f_model.set_value(edit_model.get("model", "") if is_edit else "")

                            f_base_url = _field(_("Base URL"), "http://192.168.1.x:11434/v1")
                            f_base_url.set_value(edit_model.get("base_url", "") if is_edit else "")

                            _BASE_URL_PLACEHOLDERS = {
                                "anthropic":         _("https://api.anthropic.com (empty = default)"),
                                "openai_compatible": "http://192.168.1.x:11434/v1",
                            }

                            def _update_base_url_placeholder():
                                ph = _BASE_URL_PLACEHOLDERS.get(f_backend.value, "")
                                f_base_url.props(f'placeholder="{ph}"')

                            f_api_key = _field(_("API key"), _("sk-… (leave empty if not needed)"), password=True)
                            f_api_key.set_value(edit_model.get("api_key", "") if is_edit else "")

                            f_lane = ui.select(
                                {"api": _("API (several in parallel)"), "local": _("Local (one request at a time)")},
                                label=_("Concurrency lane"),
                                value=edit_model.get("lane", "api") if is_edit else "api",
                            ).props("outlined dark dense").classes("w-full mt-2")

                            f_temperature = (
                                ui.number(
                                    label=_("Temperature (0.0 precise – 1.5 creative)"),
                                    value=float(edit_model.get("temperature", 0.3)) if is_edit else 0.3,
                                    min=0.0, max=2.0, step=0.05, format="%.2f",
                                )
                                .props("outlined dark dense")
                                .classes("w-full mt-2")
                            )

                            f_max_output = (
                                ui.number(
                                    label=_("Max. output tokens (thinking + answer, 0 = default 16384)"),
                                    value=int(edit_model.get("max_output_tokens", 0)) if is_edit else 0,
                                    min=0, step=1024, format="%.0f",
                                )
                                .props("outlined dark dense")
                                .classes("w-full mt-2")
                            )

                            _think_val = edit_model.get("think") if is_edit else None
                            _think_init = "true" if _think_val is True else ("false" if _think_val is False else "auto")
                            f_think = ui.select(
                                {"auto": _("Auto (model default)"), "true": _("Thinking ON"), "false": _("Thinking OFF")},
                                label=_("Thinking mode"),
                                value=_think_init,
                            ).props("outlined dark dense").classes("w-full mt-2")
                            ui.label(
                                _(
                                    "\"Auto\" leaves the decision to the model — Claude then never "
                                    "thinks, and other models only sometimes. Set it explicitly to "
                                    "decide yourself."
                                )
                            ).classes("text-xs text-gray-500 mt-1")
                            f_thinking_budget = (
                                ui.number(
                                    label=_("Thinking budget in tokens (Anthropic backend, 0 = 4096)"),
                                    value=int(edit_model.get("thinking_budget", 0)) if is_edit else 0,
                                    min=0, step=1024, format="%.0f",
                                )
                                .props("outlined dark dense")
                                .classes("w-full mt-2")
                            )
                            ui.label(
                                _(
                                    "With thinking ON the Anthropic backend runs at temperature 1 — "
                                    "the API allows no other value."
                                )
                            ).classes("text-xs text-gray-500 mt-1")
                            ui.label(
                                _("⚠ Qwen3 thinking-loop risk at temp. < 0.5 — recommended: ≥ 0.6")
                            ).classes("text-xs text-yellow-500 mt-1")

                            _update_base_url_placeholder()
                            f_backend.on_value_change(lambda _: _update_base_url_placeholder())

                            def _save():
                                name = f_name.value.strip()
                                if not name:
                                    ui.notify(_("Name must not be empty."), type="warning")
                                    return
                                cfg = {
                                    "id":          edit_model["id"] if is_edit else new_model("", "", "")["id"],
                                    "name":        name,
                                    "backend":     f_backend.value,
                                    "model":       f_model.value.strip(),
                                    "base_url":    f_base_url.value.strip(),
                                    "api_key":     f_api_key.value.strip(),
                                    "lane":        f_lane.value,
                                    "temperature": float(f_temperature.value or 0.3),
                                    "max_output_tokens": int(f_max_output.value or 0),
                                    "think": True if f_think.value == "true" else (False if f_think.value == "false" else None),
                                    "thinking_budget": int(f_thinking_budget.value or 0),
                                    "enabled":          True,
                                }
                                fresh = get_models(username, token)
                                if is_edit:
                                    idx = next((i for i, m in enumerate(fresh) if m["id"] == edit_model["id"]), None)
                                    if idx is not None:
                                        fresh[idx] = cfg
                                else:
                                    fresh.append(cfg)
                                save_models(username, token, fresh)
                                dlg.close()
                                model_list.refresh()
                                ui.notify(_("Model saved."), type="positive")

                            with ui.row().classes("w-full justify-end gap-2 mt-4"):
                                ui.button(_("Cancel"), on_click=dlg.close).props("flat dark").classes("text-gray-400")
                                ui.button(_("Save"), icon="save", on_click=_save).props(
                                    "unelevated dark"
                                ).classes("bg-purple-700 text-white")

                    dlg.open()

                ui.button(_("Add"), icon="add", on_click=lambda: _open_add_dialog()).props(
                    "unelevated dark dense"
                ).classes("bg-purple-700 text-white")

            ui.separator().classes("mb-3")

            @ui.refreshable
            def model_list():
                models = get_models(username, token)
                if not models:
                    ui.label(_("No models configured yet.")).classes("text-xs text-gray-500 py-2")
                    return

                ui.add_head_html("""<style>
                @media(max-width:767px){
                  .model-item{flex-wrap:wrap!important;row-gap:2px!important;}
                  .model-item-top{width:100%;order:1;}
                  .model-item-bottom{width:100%;order:2;padding-left:4px;}
                }
                @media(min-width:768px){
                  .model-item-top{display:contents;}
                  .model-item-bottom{display:contents;}
                }
                /* A flex item's default min-width is its content, so a long
                   model id widens the row until the card overflows the phone.
                   The ellipsis on the labels only takes effect once every
                   ancestor is allowed to shrink below that. */
                .model-item, .model-item-top, .model-item-bottom { min-width:0; max-width:100%; }
                </style>""")
                with ui.column().classes("w-full gap-1"):
                    for i, m in enumerate(models):
                        badge_cls = _BACKEND_COLOR.get(m.get("backend", ""), "text-gray-400 bg-gray-800")
                        with ui.row().classes(
                            "w-full items-center gap-2 px-2 py-2 rounded-lg bg-gray-900 hover:bg-gray-850 model-item"
                        ):
                            # ── Row 1 on mobile: sort + badge + lane ──────────
                            with ui.element("div").classes("flex items-center gap-2 model-item-top"):
                                # Sort arrows
                                with ui.column().classes("gap-0"):
                                    def _move(idx=i, direction=-1):
                                        ms = get_models(username, token)
                                        j = idx + direction
                                        if 0 <= j < len(ms):
                                            ms[idx], ms[j] = ms[j], ms[idx]
                                            save_models(username, token, ms)
                                            model_list.refresh()
                                    ui.button(icon="keyboard_arrow_up",
                                              on_click=lambda _, idx=i: _move(idx, -1)).props(
                                        "flat dark dense"
                                    ).classes("text-gray-600 p-0").set_enabled(i > 0)
                                    ui.button(icon="keyboard_arrow_down",
                                              on_click=lambda _, idx=i: _move(idx, 1)).props(
                                        "flat dark dense"
                                    ).classes("text-gray-600 p-0").set_enabled(i < len(models) - 1)

                                # Backend badge
                                ui.label(_BACKEND_LABEL.get(m.get("backend", ""), m.get("backend", ""))).classes(
                                    f"text-xs font-mono px-2 py-0.5 rounded {badge_cls} flex-shrink-0"
                                )

                                # Lane chip
                                lane = m.get("lane", "api")
                                ui.label(_("local") if lane == "local" else "API").classes(
                                    "text-xs text-gray-600 flex-shrink-0"
                                )

                            # ── Row 2 on mobile: name + model id + buttons ───
                            with ui.element("div").classes("flex items-center gap-2 flex-1 min-w-0 model-item-bottom"):
                                # Name + model id
                                # `w-full` is what makes the ellipsis work at all:
                                # ui.column() is a flex column with
                                # `align-items: flex-start`, so a label inside it
                                # is sized to its own content. Without an explicit
                                # width there is nothing to truncate against, and a
                                # long model name walks straight out of the card.
                                with ui.column().classes("flex-1 gap-0 min-w-0"):
                                    ui.label(m.get("name", "")).classes(
                                        "w-full text-sm font-semibold text-gray-200"
                                    ).style("overflow:hidden;white-space:nowrap;text-overflow:ellipsis")
                                    ui.label(m.get("model", "")).classes(
                                        "w-full text-xs text-gray-500 font-mono"
                                    ).style("overflow:hidden;white-space:nowrap;text-overflow:ellipsis")

                                # Edit / delete (inside row-2 div)
                                ui.button(icon="edit",
                                          on_click=lambda _, mod=m: _open_add_dialog(mod)).props(
                                    "flat dark dense"
                                ).classes("text-gray-500")

                                def _delete(mid=m["id"]):
                                    ms = get_models(username, token)
                                    save_models(username, token, [x for x in ms if x["id"] != mid])
                                    model_list.refresh()
                                    ui.notify(_("Model deleted."), type="info")

                                ui.button(icon="delete", on_click=_delete).props(
                                    "flat dark dense"
                                ).classes("text-red-700")

            model_list()

            _hint(
                _(
                    "Models appear in dropdowns in the configured order.<br><b>OpenAI-compatible</b>: Ollama (<code>http://host:11434/v1</code>), MiniMax, Moonshot, …<br><b>Anthropic-compatible</b>: Anthropic API, MiniMax (<code>https://api.minimaxi.chat/v1</code>) and other providers with an Anthropic-compatible endpoint."
                )
            )

        # ── Verarbeitung neuer Dokumente ──────────────────────────────────────
        with _card("docs"):
            from werkbank import settings_store as _ws
            from services.model_registry import get_models as _get_models

            _section_header(
                "document_scanner",
                _("Processing new documents"),
                _("Vision model for OCR / page extraction (ChromaDB embeddings)"),
            )

            _ingest_models = _get_models(username, token)
            _model_opts = {m["name"]: m["name"] for m in _ingest_models if m.get("enabled", True)}

            _cur_ingest_server = _ws.get_ingest_server()
            _cur_ingest_model  = _ws.get_ingest_model()

            # Pre-select by stored registry name; fall back to matching the model
            # id for installs configured before the name was recorded. Two entries
            # can share a model id with different servers or keys, so the name is
            # the reliable one.
            _cur_ingest_name = _ws.get_ingest_model_name()
            _cur_sel: str | None = next(
                (m["name"] for m in _ingest_models if m["name"] == _cur_ingest_name),
                None,
            ) or next(
                (m["name"] for m in _ingest_models if m.get("model") == _cur_ingest_model),
                None,
            )

            _ingest_select = ui.select(
                _model_opts or {"": _("— No models configured yet —")},
                label=_("Vision model"),
                value=_cur_sel,
            ).props("outlined dark dense").classes("w-full")

            with ui.column().classes("gap-0 mt-1 mb-3"):
                _info_server = ui.label(_("Server: {server}").format(server=_cur_ingest_server)).classes(
                    "text-xs text-gray-500 font-mono"
                )
                _info_model = ui.label(_("Model ID: {model}").format(model=_cur_ingest_model)).classes(
                    "text-xs text-gray-500 font-mono"
                )

            def _on_ingest_model_change(e, _models=_ingest_models):
                sel = next((m for m in _models if m["name"] == e.value), None)
                if not sel:
                    return
                base = (sel.get("base_url") or "").rstrip("/")
                server = base.removesuffix("/v1") if base else _cur_ingest_server
                _info_server.set_text(_("Server: {server}").format(server=server))
                _info_model.set_text(_("Model ID: {model}").format(model=sel.get('model', '')))

            _ingest_select.on_value_change(_on_ingest_model_change)

            _hint(
                _(
                    "Any model <b>with image processing</b> from <i>AI models</i> — a local one "
                    "via Ollama (qwen2.5-vl, qwen3.6, llava …) or a cloud one (Claude, GPT, "
                    "MiniMax …). A model without vision will fail on the first page."
                )
            )

            from werkbank.settings_store import get_no_ingest_tag as _get_no_ingest_tag
            _no_ingest_field = _field(
                _("No-ingest tag"), settings.ignore_inbox_tag_at_sync
            )
            _no_ingest_field.set_value(_get_no_ingest_tag())
            _hint(
                _("Documents with this Paperless tag are <b>not</b> embedded into ChromaDB during sync.")
            )

            # Extraction profile + summary language. Both used to be .env-only and
            # invisible, so a missing line silently switched a German archive to
            # the generic English rule set with nothing in the UI to show it.
            from config.extraction_rules import (
                AVAILABLE_PROFILES as _PROFILES,
                get_active_profile as _get_active_profile,
            )
            from i18n import SUPPORTED_LANGUAGES as _LANGS
            from werkbank.settings_store import get_archive_language as _get_arch_lang

            _profile_select = ui.select(
                {p: p for p in _PROFILES},
                label=_("Extraction profile"),
                value=_get_active_profile(),
            ).props("outlined dark dense").classes("w-full mt-3")
            _hint(
                _(
                    "Which set of per-document-type extraction prompts to use. This follows the "
                    "<b>names of your document types in Paperless</b>, not the language of the "
                    "documents themselves — an English invoice filed as <i>Rechnung</i> still "
                    "matches the <code>de</code> rules. <code>de</code> covers ~46 German "
                    "legal/administrative types, <code>en</code> ~13 common international ones."
                )
            )

            _arch_lang_select = ui.select(
                dict(_LANGS),
                label=_("Language of generated summaries"),
                value=_get_arch_lang() if _get_arch_lang() in _LANGS else "en",
            ).props("outlined dark dense").classes("w-full mt-3")
            _hint(
                _(
                    "Summaries and image descriptions are written in this language, whatever the "
                    "document's own language is — that keeps every summary searchable with the "
                    "same query language. The extracted page text is always kept verbatim in the "
                    "original language."
                )
            )

            def _save_ingest_cfg():
                sel = next(
                    (m for m in _get_models(username, token) if m["name"] == _ingest_select.value),
                    None,
                )
                if not sel:
                    ui.notify(_("No model selected."), type="warning")
                    return
                base = (sel.get("base_url") or "").rstrip("/")
                server = base.removesuffix("/v1") if base else settings.ollama_server
                _ws.set_value(_ws.INGEST_SERVER, server)
                _ws.set_value(_ws.INGEST_MODEL, sel.get("model", ""))
                # The registry NAME as well: the transport and the API key live on
                # the registry entry, and the key is encrypted per user, so it
                # cannot be copied into this global store. build_vision_client()
                # re-resolves the entry from the signed-in user at ingest time.
                _ws.set_value(_ws.INGEST_MODEL_NAME, sel.get("name", ""))
                _ws.set_value(_ws.TAG_NO_INGEST, _no_ingest_field.value.strip())
                _ws.set_value(_ws.EXTRACTION_PROFILE, _profile_select.value or "")
                _ws.set_value(_ws.ARCHIVE_LANGUAGE, _arch_lang_select.value or "")
                ui.notify(_("Processing settings saved."), type="positive")
                # Only affects documents ingested from here on — existing sidecars
                # keep the rules and language they were extracted with.
                ui.notify(
                    _("Applies to newly ingested documents; existing ones keep their extraction."),
                    type="info",
                )

            ui.button(_("Save"), icon="save", on_click=_save_ingest_cfg).props(
                "unelevated dark dense"
            ).classes("bg-purple-700 text-white")

        # ── Suche & Gedächtnis-Schwellenwerte ─────────────────────────────────
        with _card("memory"):
            from werkbank import settings_store as _ws_search

            _section_header(
                "tune",
                _("Search & memory thresholds"),
                _("Number of search results, relevance threshold and window for memory hints"),
            )

            _max_results_field = ui.number(
                _("Max. search results"), value=_ws_search.get_search_max_results(),
                min=1, max=100, step=1, format="%d",
            ).props("outlined dark dense").classes("w-full mb-2")

            _hint_threshold_field = ui.number(
                _("Memory hint similarity threshold (0–1)"),
                value=_ws_search.get_brain_hint_threshold(),
                min=0.0, max=1.0, step=0.05, format="%.2f",
            ).props("outlined dark dense").classes("w-full mb-2")
            _hint(
                _(
                    "Cosine similarity: higher value = stricter filter. Distances are shown in the tool log as <code>[0.XX]</code> — a hint appears only when distance ≤ <code>1 − threshold</code>."
                )
            )

            _hint_window_field = ui.number(
                _("Memory hint window factor (≥ 1.0)"),
                value=_ws_search.get_brain_hint_window(),
                min=1.0, max=3.0, step=0.1, format="%.1f",
            ).props("outlined dark dense").classes("w-full mb-2")
            _hint(
                _(
                    "Maximum distance relative to the best match. 1.0 = best match only; 1.5 = up to 50 % short of the best."
                )
            )

            def _parse_float(val, default: float) -> float:
                try:
                    return float(str(val).replace(",", "."))
                except (ValueError, TypeError):
                    return default

            def _save_search_cfg():
                try:
                    _ws_search.set_value(_ws_search.SEARCH_MAX_RESULTS, str(max(1, int(_parse_float(_max_results_field.value, 20)))))
                    _ws_search.set_value(_ws_search.BRAIN_HINT_THRESHOLD, str(max(0.0, min(1.0, _parse_float(_hint_threshold_field.value, 0.70)))))
                    _ws_search.set_value(_ws_search.BRAIN_HINT_WINDOW, str(max(1.0, _parse_float(_hint_window_field.value, 1.3))))
                    ui.notify(_("Search settings saved."), type="positive")
                except Exception as e:
                    ui.notify(_("Invalid value: {err}").format(err=e), type="negative")

            ui.button(_("Save"), icon="save", on_click=_save_search_cfg).props(
                "unelevated dark dense"
            ).classes("bg-purple-700 text-white")

        # ── Gedächtnis-Pflege (Träumen) ───────────────────────────────────────
        with _card("memory"):
            _section_header(
                "bedtime",
                _("Memory maintenance (dreaming)"),
                _("Model for memory cleanup and for reviewing extracted deadlines"),
            )
            from services.model_registry import get_models as _get_models_dream
            from services.credential_store import load_credentials as _lc_dream, save_credentials as _sc_dream

            _dream_creds = _lc_dream(username, token) if username and token else {}
            _cur_dream_model = _dream_creds.get("dream_model", "")
            _dream_model_opts = {m["name"]: m["name"] for m in _get_models_dream(username, token) if m.get("enabled", True)}
            _dream_sel = ui.select(
                _dream_model_opts,
                label=_("Model for memory cleanup"),
                value=_cur_dream_model if _cur_dream_model in _dream_model_opts else next(iter(_dream_model_opts), None),
            ).props("outlined dark dense").classes("w-full mt-2")

            # This model has a second job that its name does not suggest: the
            # final step of every sync reviews extracted deadlines with it,
            # dropping content-free entries and cross-document duplicates. Left
            # unset, that step is skipped and only a line in the sync log says so.
            _hint(
                _(
                    "Used for two things: tidying up memory entries, and reviewing the "
                    "deadlines found during sync — junk and duplicates are dropped before "
                    "they reach the dashboard. Can be a local or a cloud model. "
                    "<b>Without a model here, deadline review is skipped.</b>"
                )
            )

            def _save_dream_cfg():
                c = _lc_dream(username, token)
                c["dream_model"] = _dream_sel.value
                _sc_dream(username, token, c)
                ui.notify(_("Saved."), type="positive")
                # Verdicts are cached per action, so a model change only affects
                # deadlines that have not been judged yet.
                ui.notify(
                    _("Applies to newly found deadlines; existing verdicts are kept."),
                    type="info",
                )

            ui.button(_("Save"), icon="save", on_click=_save_dream_cfg).props(
                "unelevated dark dense"
            ).classes("bg-purple-700 text-white mt-3")

        # ── IMAP ──────────────────────────────────────────────────────────────
        with _card("connect"):
            _section_header(
                "email",
                _("Email (IMAP)"),
                _("Read-only — no emails are sent or modified"),
            )

            imap_host = _field(_("IMAP server"), "imap.gmail.com")
            imap_host.set_value(imap_cfg.get("host", "imap.gmail.com"))

            with ui.row().classes("w-full gap-3"):
                imap_port = (
                    ui.number(
                        label=_("Port"),
                        value=imap_cfg.get("port", 993),
                        min=1,
                        max=65535,
                        step=1,
                    )
                    .props("outlined dark dense")
                    .classes("w-28")
                )
                imap_ssl = ui.checkbox(
                    "SSL/TLS", value=imap_cfg.get("use_ssl", True)
                ).classes("self-center text-sm text-gray-300")

            imap_user = _field(_("Username / email address"), "vorname@gmail.com")
            imap_user.set_value(imap_cfg.get("username", ""))

            imap_pass = _field(_("Password / app password"), password=True)
            imap_pass.set_value(imap_cfg.get("password", ""))

            _hint(
                _(
                    "<b style='color:#a78bfa'>Gmail:</b> IMAP must be enabled (Gmail → Settings → Forwarding and POP/IMAP). Password = Google <b>app password</b> (2FA must be active): Google Account → Security → App passwords."
                )
            )

            imap_status_lbl, imap_spin = _status_row()

            with ui.row().classes("gap-3 mt-2"):

                async def _test_imap() -> None:
                    from services.imap_service import test_connection as _t

                    imap_spin.set_visibility(True)
                    imap_status_lbl.set_text("")
                    err = await _t(
                        host=imap_host.value.strip(),
                        port=int(imap_port.value or 993),
                        username=imap_user.value.strip(),
                        password=imap_pass.value,
                        use_ssl=bool(imap_ssl.value),
                    )
                    _show_status(imap_status_lbl, imap_spin, err)

                ui.button(_("Test connection"), icon="wifi", on_click=_test_imap).props(
                    "flat dark dense"
                ).classes("text-gray-300 border border-gray-600")

                async def _save_imap() -> None:
                    nc = load_credentials(username, token)
                    nc["imap"] = {
                        "host": imap_host.value.strip(),
                        "port": int(imap_port.value or 993),
                        "username": imap_user.value.strip(),
                        "password": imap_pass.value,
                        "use_ssl": bool(imap_ssl.value),
                    }
                    await asyncio.to_thread(save_credentials, username, token, nc)
                    ui.notify(_("IMAP settings saved"), type="positive")

                ui.button(_("Save"), icon="save", on_click=_save_imap).props(
                    "unelevated dark dense"
                ).classes("bg-purple-700 text-white")

        # ── Calendar ──────────────────────────────────────────────────────────
        with _card("connect"):
            _section_header(
                "calendar_month",
                _("Calendar"),
                _("Read-only — no events are created or modified"),
            )

            # Mode selector
            cal_mode = ui.select(
                {"ical": _("Google Calendar (iCal URL)"), "caldav": _("CalDAV (Nextcloud, iCloud, …)")},
                value=saved_cal_mode,
                label=_("Connection type"),
            ).props("outlined dark dense").classes("w-full mb-4")

            # ── iCal URL panel ────────────────────────────────────────────────
            with ui.element("div") as ical_panel:
                ical_url1 = _field(_("Calendar 1 – iCal URL"), "https://calendar.google.com/calendar/ical/…/basic.ics")
                ical_url1.set_value(_saved_ical_urls[0])
                ical_url2 = _field(_("Calendar 2 – iCal URL (optional)"), "https://calendar.google.com/calendar/ical/…/basic.ics")
                ical_url2.set_value(_saved_ical_urls[1])
                ical_url3 = _field(_("Calendar 3 – iCal URL (optional)"), "https://calendar.google.com/calendar/ical/…/basic.ics")
                ical_url3.set_value(_saved_ical_urls[2])

                _hint(
                    _(
                        "<b style='color:#a78bfa'>Google Calendar:</b> Calendar settings &rarr; [select calendar] &rarr; <i>Secret address in iCal format</i> &rarr; copy URL.<br>The URL contains a secret token — it works without a password.<br>Up to 3 calendars are searched in parallel."
                    )
                )

            # ── CalDAV panel ──────────────────────────────────────────────────
            with ui.element("div") as caldav_panel:
                caldav_url = _field(
                    _("CalDAV URL"),
                    "https://nextcloud.example.com/remote.php/dav/calendars/user/personal/",
                )
                caldav_url.set_value(cal_cfg.get("url", ""))

                caldav_user = _field(_("Username"), "vorname@example.com")
                caldav_user.set_value(cal_cfg.get("username", ""))

                caldav_pass = _field(_("Password"), password=True)
                caldav_pass.set_value(cal_cfg.get("password", ""))

                _hint(
                    _(
                        "<b style='color:#a78bfa'>iCloud:</b> <code>https://caldav.icloud.com/</code>, app-specific password.<br><b style='color:#a78bfa'>Nextcloud:</b> copy the remote URL from the calendar app."
                    )
                )

            def _update_cal_panels() -> None:
                is_ical = cal_mode.value == "ical"
                ical_panel.set_visibility(is_ical)
                caldav_panel.set_visibility(not is_ical)

            _update_cal_panels()
            cal_mode.on_value_change(lambda _: _update_cal_panels())

            cal_status_lbl, cal_spin = _status_row()

            with ui.row().classes("gap-3 mt-2"):

                async def _test_cal() -> None:
                    cal_spin.set_visibility(True)
                    cal_status_lbl.set_text("")
                    if cal_mode.value == "ical":
                        from services.caldav_service import test_ical_url as _t
                        urls = [u for u in [ical_url1.value.strip(), ical_url2.value.strip(), ical_url3.value.strip()] if u]
                        if not urls:
                            _show_status(cal_status_lbl, cal_spin, _("No URL entered"))
                            return
                        errors = [e for e in await asyncio.gather(*[_t(u) for u in urls]) if e]
                        _show_status(cal_status_lbl, cal_spin, "; ".join(errors) if errors else "",
                                     ok_text=_("{n} calendars reachable").format(n=len(urls)))
                    else:
                        from services.caldav_service import test_caldav_connection as _t  # type: ignore[assignment]
                        err = await _t(
                            url=caldav_url.value.strip(),
                            username=caldav_user.value.strip(),
                            password=caldav_pass.value,
                        )
                        _show_status(cal_status_lbl, cal_spin, err)

                ui.button(_("Test connection"), icon="wifi", on_click=_test_cal).props(
                    "flat dark dense"
                ).classes("text-gray-300 border border-gray-600")

                async def _save_cal() -> None:
                    nc = load_credentials(username, token)
                    if cal_mode.value == "ical":
                        urls = [u for u in [ical_url1.value.strip(), ical_url2.value.strip(), ical_url3.value.strip()] if u]
                        nc["calendar"] = {"ical_urls": urls}
                    else:
                        nc["calendar"] = {
                            "url": caldav_url.value.strip(),
                            "username": caldav_user.value.strip(),
                            "password": caldav_pass.value,
                        }
                    await asyncio.to_thread(save_credentials, username, token, nc)
                    ui.notify(_("Calendar settings saved"), type="positive")

                ui.button(_("Save"), icon="save", on_click=_save_cal).props(
                    "unelevated dark dense"
                ).classes("bg-purple-700 text-white")

        # ── Absender-Profil ───────────────────────────────────────────────────
        with _card("connect"):
            _section_header(
                "person",
                _("Sender profile"),
                _("Used for letter generation (DOCX)"),
            )

            sp_name = _field(_("Full name"), "Max Mustermann")
            sp_name.set_value(sender_cfg.get("name", ""))

            sp_company = _field(_("Company / organization (optional)"), "Muster GmbH")
            sp_company.set_value(sender_cfg.get("company", ""))

            sp_street = _field(_("Street and number"), "Musterstraße 1")
            sp_street.set_value(sender_cfg.get("street", ""))

            with ui.row().classes("w-full gap-3"):
                sp_plz = _field(_("Postcode"), "80331").classes("w-28")
                sp_plz.set_value(sender_cfg.get("plz", ""))
                sp_city = _field(_("City"), "München").classes("flex-1")
                sp_city.set_value(sender_cfg.get("city", ""))

            sp_phone = _field(_("Phone (optional)"), "+49 89 …")
            sp_phone.set_value(sender_cfg.get("phone", ""))

            sp_email = _field(_("Email (optional)"), "name@example.com")
            sp_email.set_value(sender_cfg.get("email", ""))

            sp_closing = _field(_("Default closing"), "Mit freundlichen Grüßen")
            sp_closing.set_value(sender_cfg.get("closing", "Mit freundlichen Grüßen"))

            async def _save_sender() -> None:
                nc = load_credentials(username, token)
                nc["sender_profile"] = {
                    "name":    sp_name.value.strip(),
                    "company": sp_company.value.strip(),
                    "street":  sp_street.value.strip(),
                    "plz":     sp_plz.value.strip(),
                    "city":    sp_city.value.strip(),
                    "phone":   sp_phone.value.strip(),
                    "email":   sp_email.value.strip(),
                    "closing": sp_closing.value.strip() or "Mit freundlichen Grüßen",
                }
                await asyncio.to_thread(save_credentials, username, token, nc)
                ui.notify(_("Sender profile saved"), type="positive")

            ui.button(_("Save"), icon="save", on_click=_save_sender).props(
                "unelevated dark dense"
            ).classes("bg-purple-700 text-white mt-2")

        # ── PDF → Paperless: Standard-Tags ───────────────────────────────────
        with _card("docs"):
            from werkbank.settings_store import (
                get_tag_inbox as _get_tag_inbox,
                get_tag_ai_generated as _get_tag_ai,
                get_ingest_correspondent as _get_correspondent,
                get_ingest_doc_type as _get_doc_type,
            )
            from werkbank import settings_store as _ws2

            _section_header(
                "label",
                _("PDF → Paperless: default tags"),
                _("Tags, correspondent and document type for AI-generated documents"),
            )

            with ui.row().classes("w-full gap-3"):
                _pt_inbox = _field(_("Tag: Inbox"), "Posteingang")
                _pt_inbox.set_value(_get_tag_inbox())
                _pt_ai = _field(_("Tag: AI-generated"), "AI-generiert")
                _pt_ai.set_value(_get_tag_ai())

            with ui.row().classes("w-full gap-3"):
                _pt_correspondent = _field(_("Correspondent"), "PaperSage AI")
                _pt_correspondent.set_value(_get_correspondent())
                _pt_doctype = _field(_("Document type"), "Information")
                _pt_doctype.set_value(_get_doc_type())

            _hint(
                _(
                    "Tags must exist in Paperless. Correspondent and document type are created automatically if missing."
                )
            )

            def _save_pdf_tags():
                _ws2.set_value(_ws2.TAG_INBOX, _pt_inbox.value.strip())
                _ws2.set_value(_ws2.TAG_AI_GENERATED, _pt_ai.value.strip())
                _ws2.set_value(_ws2.INGEST_CORRESPONDENT, _pt_correspondent.value.strip())
                _ws2.set_value(_ws2.INGEST_DOC_TYPE, _pt_doctype.value.strip())
                ui.notify(_("Tags saved."), type="positive")

            ui.button(_("Save"), icon="save", on_click=_save_pdf_tags).props(
                "unelevated dark dense"
            ).classes("bg-purple-700 text-white")

        # ── KI-Tiefenrecherche ───────────────────────────────────────────────────────
        with _card("ai"):
            _section_header(
                "auto_awesome",
                _("AI deep research"),
                _("System prompts for Planner, Splitter, Critic, Synthesizer"),
            )

            from werkbank import settings_store as _ws
            from werkbank.roles import planner as _pl, splitter as _sp, critic as _cr, synthesizer as _sy
            from werkbank import compaction as _co

            # ── Token-Limits ──────────────────────────────────────────────
            ui.label(_("Token limits per role")).classes("text-xs text-gray-500 uppercase tracking-wide mt-2 mb-1")
            _token_defs = [
                (_("Planner"),       _ws.TOKENS_PLANNER),
                (_("Splitter"),      _ws.TOKENS_SPLITTER),
                (_("Critic"),        _ws.TOKENS_CRITIC),
                (_("Synthesizer"),   _ws.TOKENS_SYNTHESIZER),
                (_("Compaction"), _ws.TOKENS_COMPACTION),
                (_("Worker"),        _ws.TOKENS_WORKER),
            ]
            _token_inputs: dict[str, ui.number] = {}
            with ui.grid(columns=3).classes("w-full gap-2 mb-3"):
                for _tlabel, _tkey in _token_defs:
                    _tval = _ws.get_tokens(_tkey)
                    _tn = ui.number(
                        label=_tlabel, value=_tval, min=1000, step=1000, format="%.0f"
                    ).props("outlined dark dense").classes("w-full")
                    _token_inputs[_tkey] = _tn

            ui.separator().classes("my-2")

            _prompt_defs = [
                (_("Planner"),      _ws.PROMPT_PLANNER,     _pl.DEFAULT_SYSTEM_PROMPT),
                (_("Splitter"),     _ws.PROMPT_SPLITTER,    _sp.DEFAULT_SYSTEM_PROMPT),
                (_("Critic"),       _ws.PROMPT_CRITIC,      _cr.DEFAULT_SYSTEM_PROMPT),
                (_("Synthesizer"),  _ws.PROMPT_SYNTHESIZER, _sy.DEFAULT_SYSTEM_PROMPT),
                (_("Compaction"),_ws.PROMPT_COMPACTION,  _co.DEFAULT_SYSTEM_PROMPT),
            ]

            _prompt_inputs: dict[str, ui.textarea] = {}
            for label, key, default in _prompt_defs:
                with ui.expansion(label).classes("w-full text-gray-300").props("dark"):
                    stored = _ws.get(key)
                    ta = ui.textarea(
                        label=_("System prompt: {role}").format(role=label),
                        value=stored or default,
                    ).classes("w-full").style(
                        "min-height:140px; font-family:monospace; font-size:11px;"
                        " background:var(--c-bg); color:var(--c-text-2);"
                    ).props("dark outlined")
                    _prompt_inputs[key] = ta

                    def _reset(k=key, d=default, t=ta):
                        t.set_value(d)
                        _ws.set_value(k, "")
                        ui.notify(_("Prompt reset."), type="info")

                    ui.button(_("Reset to default"), icon="refresh", on_click=_reset).props(
                        "flat dark dense"
                    ).classes("text-gray-500 mt-1")

            def _save_werkbank():
                for key, tn in _token_inputs.items():
                    _ws.set_value(key, str(int(tn.value or 16000)))
                for key, ta in _prompt_inputs.items():
                    val = ta.value.strip()
                    default_val = next(d for _, k, d in _prompt_defs if k == key)
                    _ws.set_value(key, val if val != default_val else "")
                ui.notify(_("Deep research settings saved."), type="positive")

            with ui.row().classes("gap-2 mt-3"):
                def _reset_all_prompts():
                    for key, ta in _prompt_inputs.items():
                        default_val = next(d for _, k, d in _prompt_defs if k == key)
                        ta.set_value(default_val)
                        _ws.set_value(key, "")
                    ui.notify(_("All prompts reset."), type="info")

                ui.button(_("Reset all"), icon="refresh", on_click=_reset_all_prompts).props(
                    "flat dark dense"
                ).classes("text-gray-500")
                ui.button(_("Save"), icon="save", on_click=_save_werkbank).props(
                    "unelevated dark dense"
                ).classes("bg-purple-700 text-white")

        # ── Import / Export ───────────────────────────────────────────────────
        with _card("data"):
            from services import settings_transfer as _st

            _section_header(
                "import_export",
                _("Import / export settings"),
                _("Move your personal settings to another installation"),
            )

            _ta_style = (
                "min-height:150px; font-family:monospace; font-size:11px;"
                " background:var(--c-bg); color:var(--c-text-2);"
            )

            # ── Export ────────────────────────────────────────────────────────
            _export_secrets_cb = ui.checkbox(
                _("Include passwords and API keys"), value=False
            ).classes("text-sm text-gray-300")
            _hint(
                _(
                    "Without this option the export contains no secrets; on import the "
                    "existing passwords and keys are kept. <b style='color:#f87171'>With it, the "
                    "string contains your API keys, IMAP/CalDAV passwords and iCal URLs in "
                    "plain text</b> — treat it like a password."
                )
            )

            _export_out = (
                ui.textarea(label=_("Export string"))
                .props("dark outlined readonly")
                .classes("w-full")
                .style(_ta_style)
            )
            _export_out.set_visibility(False)

            def _do_export() -> None:
                text = _st.build_export(
                    username,
                    token,
                    language=ng_app.storage.user.get("language", DEFAULT_LANG),
                    theme=ng_app.storage.user.get("theme", "dark"),
                    push_text_to_paperless=bool(
                        ng_app.storage.user.get("push_text_to_paperless", False)
                    ),
                    voice_memos_enabled=bool(
                        ng_app.storage.user.get("voice_memos_enabled", True)
                    ),
                    include_secrets=bool(_export_secrets_cb.value),
                )
                _export_out.set_value(text)
                _export_out.set_visibility(True)

            def _copy_export() -> None:
                if not _export_out.value:
                    return
                ui.clipboard.write(_export_out.value)
                ui.notify(_("Copied to clipboard."), type="positive")

            with ui.row().classes("gap-3 mt-2"):
                ui.button(_("Create export"), icon="download", on_click=_do_export).props(
                    "unelevated dark dense"
                ).classes("bg-purple-700 text-white")
                ui.button(_("Copy"), icon="content_copy", on_click=_copy_export).props(
                    "flat dark dense"
                ).classes("text-gray-300 border border-gray-600")

            _hint(
                _(
                    "The copy button needs HTTPS or localhost. Otherwise select the text in the field and copy it manually."
                )
            )

            ui.separator().classes("my-4")

            # ── Import ────────────────────────────────────────────────────────
            _import_in = (
                ui.textarea(label=_("Paste export string here"))
                .props("dark outlined")
                .classes("w-full")
                .style(_ta_style)
            )

            async def _do_import() -> None:
                try:
                    payload = _st.parse_export(_import_in.value)
                except _st.SettingsImportError as e:
                    ui.notify(_("Import failed: {err}").format(err=e), type="negative")
                    return

                items = _st.describe(payload, list(SUPPORTED_LANGUAGES))
                if not items:
                    ui.notify(_("The string contains no importable settings."), type="warning")
                    return

                with ui.dialog() as _dlg, ui.card().classes("bg-gray-800 border border-gray-700"):
                    ui.label(_("Apply these settings?")).classes(
                        "text-base font-semibold text-gray-100"
                    )
                    with ui.column().classes("gap-0 my-2"):
                        for _it in items:
                            ui.label(f"• {_it}").classes("text-sm text-gray-300")
                    if not payload.get("_secrets"):
                        ui.label(
                            _("The export contains no secrets — existing passwords and API keys are kept.")
                        ).classes("text-xs text-gray-500 mt-1")
                    with ui.row().classes("gap-2 mt-3 justify-end w-full"):
                        ui.button(_("Cancel"), on_click=lambda: _dlg.submit(False)).props(
                            "flat dark dense"
                        ).classes("text-gray-400")
                        ui.button(_("Apply"), icon="check", on_click=lambda: _dlg.submit(True)).props(
                            "unelevated dark dense"
                        ).classes("bg-purple-700 text-white")

                if not await _dlg:
                    return

                try:
                    prefs = await asyncio.to_thread(
                        _st.apply_import, username, token, payload, list(SUPPORTED_LANGUAGES)
                    )
                except Exception as e:  # noqa: BLE001 — surfaced to the user
                    ui.notify(_("Import failed: {err}").format(err=e), type="negative")
                    return

                for k, v in prefs.items():
                    ng_app.storage.user[k] = v
                ui.notify(_("Settings imported."), type="positive")
                ui.navigate.reload()

            ui.button(_("Import"), icon="upload", on_click=_do_import).props(
                "unelevated dark dense"
            ).classes("bg-purple-700 text-white mt-2")

            _hint(
                _(
                    "Import overwrites the sections contained in the string. Global settings "
                    "(processing, tags, search, deep research) are <b>not</b> part of the export — "
                    "they apply to all users."
                )
            )

        # ── Vault / Obsidian ──────────────────────────────────────────────────
        with _card("memory"):
            from vault.paths import vault_path as _vault_path, brain_path as _brain_path

            _section_header(
                "folder_open",
                _("Vault / Obsidian synchronization"),
                _("Where PaperlessBrain expects memory files and your notes"),
            )

            _vp = str(_vault_path(username)) if username else str(settings.vault_root / "<username>")
            _bp = str(_brain_path(username)) if username else str(settings.vault_root / "<username>" / settings.brain_subfolder)

            with ui.column().classes("gap-1 mb-3"):
                # `break-all`: a path has no spaces, so without it the label is
                # one unbreakable word that pushes the card off a phone screen.
                ui.label(_("Vault directory (Obsidian vault root)")).classes("text-xs text-gray-500 uppercase tracking-wide mt-1")
                ui.label(_vp).classes("w-full break-all text-sm font-mono text-gray-300 bg-gray-900 rounded px-3 py-1.5 select-all")

                ui.label(_("Memory subfolder (Brain)")).classes("text-xs text-gray-500 uppercase tracking-wide mt-2")
                ui.label(_bp).classes("w-full break-all text-sm font-mono text-gray-300 bg-gray-900 rounded px-3 py-1.5 select-all")

            _hint(
                _(
                    "<b>Obsidian / Remotely Save:</b> point synchronization at the vault directory above.<br>Exclude <code>.git/</code> client-side in Remotely Save (Settings → Remotely Save → exclude list: <code>.git</code>) so the server-side Git repo is not propagated to devices."
                )
            )

        # ── Info footer ───────────────────────────────────────────────────────
        with ui.row().classes("items-start gap-2"):
            ui.icon("lock", size="xs").classes("text-gray-700 flex-shrink-0 mt-0.5")
            ui.label(
                _(
                    "Credentials are stored encrypted and are accessible only to your Paperless user account. The key is your current Paperless token — if you regenerate it in Paperless, you must re-enter the settings."
                )
            ).classes("text-xs text-gray-600 leading-relaxed")
