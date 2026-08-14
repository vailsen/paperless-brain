"""Voice-memo quick capture: hold to speak, confirm, file into the vault.

Deliberately not a chat tool. Pressing the button *is* the routing decision, so
nothing has to disambiguate a memo from `remember_fact` or `create_deadline` —
see `docs/voice-memos-tasks.md`.

The dialog also works with no microphone at all: type the memo and save. That
keeps the whole path usable when `getUserMedia` is unavailable (it needs a
secure context — HTTPS or localhost).
"""

import json

from nicegui import app as ng_app
from nicegui import ui

from app_ui.memo_routes import QUICK_PATH, REWRITE_PATH, TRANSCRIBE_PATH
from config.settings import settings
from i18n import get_translator
from services import transcription
from services.clients import vault_memo_writer
from services.session_auth import get_session_token

_ENABLED_KEY = "voice_memos_enabled"
_MIC_ENGINE_KEY = "chat_mic_engine"
_DICTATION_LANG_KEY = "dictation_language"


def memo_configured() -> bool:
    """True when a transcription service is set up. Server-level, not per user."""
    return transcription.is_configured()


def memo_enabled() -> bool:
    """True when the feature should be visible for the current user.

    Configuring the service is itself the opt-in — a user who set WHISPER_URL
    wants voice memos. The per-user toggle exists to turn them back off.
    """
    if not memo_configured():
        return False
    return bool(ng_app.storage.user.get(_ENABLED_KEY, True))


def set_memo_enabled(value: bool) -> None:
    ng_app.storage.user[_ENABLED_KEY] = bool(value)


def chat_mic_engine() -> str:
    """Which engine the chat mic uses: ``whisper`` or ``browser``.

    Whisper transcribes better but only answers after the round trip; the Web
    Speech API streams words while you talk and costs nothing. Which trade-off
    is right is a preference, not something the server can decide — hence the
    setting. Defaults to Whisper, which is what a configured service implies.
    """
    value = ng_app.storage.user.get(_MIC_ENGINE_KEY, "whisper")
    return "browser" if value == "browser" else "whisper"


def set_chat_mic_engine(value: str) -> None:
    ng_app.storage.user[_MIC_ENGINE_KEY] = "browser" if value == "browser" else "whisper"


# The language you speak is not the language you read the app in — an English
# interface with German dictation is the normal case here, so it cannot be
# derived from the UI language. ISO-639-1 for the transcription service, mapped
# to BCP-47 for the Web Speech API, which insists on a region.
DICTATION_LANGUAGES: dict[str, str] = {
    "de": "Deutsch",
    "en": "English",
    "fr": "Français",
    "es": "Español",
    "it": "Italiano",
    "nl": "Nederlands",
    "pl": "Polski",
    "pt": "Português",
}

_BCP47 = {
    "de": "de-DE",
    "en": "en-US",
    "fr": "fr-FR",
    "es": "es-ES",
    "it": "it-IT",
    "nl": "nl-NL",
    "pl": "pl-PL",
    "pt": "pt-PT",
}


def dictation_language() -> str:
    """ISO-639-1 code to dictate in, or ``""`` for "let the service decide".

    Order: the user's own choice, then the server's ``WHISPER_LANGUAGE``, then
    the UI language. The server default comes before the UI language on purpose
    — whoever set `WHISPER_LANGUAGE=de` stated which language gets spoken here,
    and that held for the transcription service long before the browser engine
    became selectable.
    """
    chosen = str(ng_app.storage.user.get(_DICTATION_LANG_KEY, "") or "").strip()
    if chosen:
        return chosen
    server = (settings.whisper_language or "").strip().lower()
    if server and server != "auto":
        return server
    from i18n import DEFAULT_LANG

    return str(ng_app.storage.user.get("language", DEFAULT_LANG) or DEFAULT_LANG)


def set_dictation_language(value: str | None) -> None:
    ng_app.storage.user[_DICTATION_LANG_KEY] = (value or "").strip()


def speech_recognition_lang() -> str:
    """BCP-47 tag for the Web Speech API. Never empty — it has no 'auto'."""
    code = dictation_language() or "en"
    return _BCP47.get(code, code if "-" in code else f"{code}-{code.upper()}")


def memo_button() -> None:
    """Header entry point. Renders nothing when the feature is off."""
    if not memo_enabled():
        return
    _ = get_translator()
    dialog = build_memo_dialog()

    def _open() -> None:
        # The dialog outlives any single memo, so it is cleared on the way in as
        # well as on the way out — see the `hide` handler in build_memo_dialog().
        dialog.reset_memo()
        dialog.open()

    ui.button(icon="mic", color=None, on_click=_open).props(
        "flat dark dense"
    ).classes("nav-btn memo-nav-btn").tooltip(_("Voice memo"))
    _quick_memo_notices()


def _quick_memo_notices() -> None:
    """Show the outcome of background memos, wherever the user ended up.

    A quick memo deliberately outlives the page that started it, so its result
    cannot be delivered by the request that started it either. The server queues
    a notice per user and every connected page drains it.
    """
    _ = get_translator()

    def _drain() -> None:
        from app_ui.memo_routes import pop_notices

        username = ng_app.storage.user.get("paperless_user", "")
        if not username:
            return
        for notice in pop_notices(username):
            kind = notice.get("kind")
            if kind == "ok":
                ui.notify(
                    _("Memo saved as {name}").format(name=notice.get("message", "")),
                    type="positive",
                )
            else:
                ui.notify(
                    notice.get("message", ""),
                    type="negative" if kind == "error" else "warning",
                    timeout=0 if kind == "error" else None,
                    close_button=True,
                )

    ui.timer(4.0, _drain)


def build_memo_dialog():
    """Build the capture dialog. Returns the ui.dialog so a caller can open it."""
    _ = get_translator()

    state: dict = {"transcript": ""}

    # Mode switch: one filled segment for the active mode, the other only
    # outlined. Colour marks the active selection and nothing else, so two
    # saturated halves would say "both are on".
    ui.add_head_html("""<style>
.memo-mode-toggle {
  border: 1px solid var(--c-border); border-radius: 6px; overflow: hidden;
  /* Quasar paints the active segment with `.bg-primary { background:
     var(--q-primary) !important }`. Rebinding the variable on the container
     beats that without an !important arms race — and follows the theme. */
  --q-primary: var(--c-accent);
}
.memo-mode-toggle .q-btn {
  background: transparent !important; color: var(--c-text-muted) !important;
  font-size: 0.72rem; min-height: 26px; padding: 0 10px;
}
.memo-mode-toggle .q-btn:hover { color: var(--c-text-2) !important; }
/* Quasar leaves toggle-color at "primary", so the active segment is the one
   carrying .bg-primary — it picks up the rebound variable above. */
.memo-mode-toggle .q-btn.bg-primary { color: #fff !important; }
</style>""")

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-xl gap-4 memo-card").style(
        "background:var(--c-surface); border:1px solid var(--c-border);"
    ):
        with ui.row().classes("w-full items-center justify-between gap-2"):
            ui.label(_("Voice memo")).classes("text-lg font-semibold").style(
                "color:var(--c-text);"
            )
            # Drives three things at once: speaker labels from the transcription
            # service, which rewrite prompt runs, and the length/size caps.
            # No `color=` prop on purpose: on a QBtnToggle that paints the
            # *unselected* segments (in Quasar's own Material purple, which is
            # not the brand purple), so both halves end up filled with two
            # clashing colours. Styling lives in CSS below, off theme tokens.
            mode_toggle = (
                ui.toggle(
                    {"memo": _("Memo"), "conversation": _("Conversation")},
                    value="memo",
                )
                .props("dense unelevated no-caps")
                .classes("memo-mode-toggle")
            )

        # ── Recording control ────────────────────────────────────────────────
        with ui.column().classes("w-full items-center gap-2"):
            # Two buttons of equal weight rather than a mode switch: the choice
            # between reviewing a memo and firing it off belongs to the single
            # recording being made, and a sticky toggle is a hidden state the
            # user finds out about only afterwards.
            with ui.row().classes("items-center justify-center gap-6"):
                with ui.column().classes("items-center gap-1"):
                    rec_btn = (
                        ui.button(icon="mic")
                        .props("round unelevated size=lg")
                        .classes("memo-record-btn")
                    )
                    ui.label(_("With review")).classes("text-xs").style(
                        "color:var(--c-text-muted);"
                    )
                with ui.column().classes("items-center gap-1"):
                    (
                        ui.button(icon="bolt")
                        .props("round unelevated size=lg")
                        .classes("memo-record-btn memo-quick-btn")
                    )
                    ui.label(_("Quick memo")).classes("text-xs").style(
                        "color:var(--c-text-muted);"
                    )
                # Shown only while recording — JS owns its visibility, because
                # only JS knows whether the recorder is running.
                (
                    ui.button(icon="close")
                    .props("round flat size=md")
                    .classes("memo-cancel-btn")
                    .style("display:none;color:var(--c-text-2);")
                    .tooltip(_("Discard recording"))
                )

            status = ui.label(_("Hold to record, swipe up to lock")).classes(
                "text-sm text-center"
            ).style("color:var(--c-text-muted);")
            ui.label(
                _("With review: check the text before saving. Quick memo: filed automatically.")
            ).classes("text-xs text-center").style("color:var(--c-text-muted);")

            # Same destination, different source: an already-recorded file goes
            # through the identical transcribe → rewrite → review path.
            upload = (
                ui.upload(
                    label=_("Upload audio file"),
                    on_upload=lambda e: _on_upload(e),
                    auto_upload=True,
                    # The larger of the two caps: the client cannot re-evaluate
                    # this on mode change, and the server enforces the real
                    # per-mode limit anyway.
                    max_file_size=settings.conversation_max_upload_mb * 1024 * 1024,
                    on_rejected=lambda: ui.notify(
                        _("The file exceeds {n} MB.").format(
                            n=settings.conversation_max_upload_mb
                        ),
                        type="warning",
                    ),
                )
                .props(
                    'accept="audio/*,.wav,.m4a,.mp3,.ogg,.flac,.webm,.mp4,.aac" '
                    "flat dense dark hide-upload-btn"
                )
                .classes("w-full memo-upload")
            )

        topic_input = ui.input(label=_("Topic")).props("dark dense outlined").classes("w-full")

        # The transcript is the only part allowed to consume leftover height.
        # Without this wrapper an autogrow textarea holding a long conversation
        # grows the card past the viewport and pushes Save/Discard off-screen.
        with ui.column().classes("w-full gap-0 memo-text-scroll"):
            text_area = (
                ui.textarea(label=_("Memo"))
                .props("dark outlined autogrow")
                .classes("w-full")
                .style("min-height:160px;")
            )

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button(_("Discard"), on_click=lambda: _reset_and_close()).props(
                "flat dark dense"
            )
            save_btn = ui.button(_("Save")).props("unelevated dense color=purple")

    def _reset_status() -> None:
        """Reset the status line through the DOM, not through `status.text`.

        The recorder script writes the label directly via `textContent`, so
        Python's copy of the prop never changed and assigning the same value
        back produces no update — the line would stay on "Transcribing …"
        forever. JS owns this label; Python only resets it.
        """
        ui.run_javascript(
            "document.querySelectorAll('.memo-status').forEach(function(el) { el.textContent = "
            f"{json.dumps(_('Hold to record, swipe up to lock'))}; }});"
        )

    def _publish_mode() -> None:
        """Hand the mode to the recorder script.

        It runs outside NiceGUI's component tree, so reading the toggle from the
        DOM would mean scraping Quasar's markup. A global set from Python on
        every change is the stable contract.
        """
        ui.run_javascript(f"window.__memoMode = {json.dumps(mode_toggle.value)};")

    mode_toggle.on_value_change(lambda: _publish_mode())

    def _set_status(text: str) -> None:
        """Same reason as _reset_status: the label is owned by the DOM."""
        ui.run_javascript(
            "document.querySelectorAll('.memo-status').forEach(function(el) { el.textContent = "
            f"{json.dumps(text)}; }});"
        )

    def _apply_payload(payload: dict) -> None:
        """Fold a transcription result into the form. Shared by the recorder and
        the file upload — both produce the same {topic, text, transcript}."""
        state["transcript"] = payload.get("transcript", "")
        incoming = payload.get("text", "")
        # Append rather than replace: a second recording continues the memo
        # instead of destroying what is already there.
        existing = (text_area.value or "").strip()
        text_area.value = f"{existing}\n\n{incoming}".strip() if existing else incoming
        if not (topic_input.value or "").strip():
            topic_input.value = payload.get("topic", "")

    async def _on_upload(e) -> None:
        """Transcribe an already-recorded file. Goes through the same helper the
        HTTP route uses, so the size cap and the silence guard still apply."""
        from app_ui.memo_routes import MemoInputError, rewrite_payload, transcribe_payload

        upload.reset()  # otherwise the picker keeps the old file and won't re-fire
        username = ng_app.storage.user.get("paperless_user", "")
        token = get_session_token()
        conversation = mode_toggle.value == "conversation"
        _set_status(_("Transcribing …"))
        try:
            # Same two phases as the recorder, for the same reason: the wait on
            # the model is the longer one and deserves to say so.
            payload = await transcribe_payload(
                await e.file.read(),
                filename=e.file.name or "memo.m4a",
                content_type=e.file.content_type or "application/octet-stream",
                rewrite=False,
                username=username,
                token=token,
                conversation=conversation,
                language=dictation_language(),
            )
            _set_status(_("AI is tidying it up …"))
            payload = await rewrite_payload(
                payload["text"],
                username=username,
                token=token,
                conversation=conversation,
            )
        except MemoInputError as exc:
            _set_status(exc.message)
            ui.notify(exc.message, type="negative")
            return
        except Exception as exc:  # never leave the dialog stuck on "Transcribing …"
            _set_status(_("Transcription failed."))
            ui.notify(_("Transcription failed: {err}").format(err=exc), type="negative")
            return
        _reset_status()
        _apply_payload(payload)

    def _reset() -> None:
        state["transcript"] = ""
        topic_input.value = ""
        text_area.value = ""
        # The explicit update() is what actually empties the fields. Assigning a
        # value that equals Python's copy is a no-op — nothing is sent — and by
        # the time the dialog is reopened Python already holds "" from the reset
        # that ran on close, while the browser still shows the old text. Same
        # divergence as the status line (see _reset_status), opposite direction:
        # there the DOM was ahead of Python, here Python is ahead of the DOM.
        # update() re-sends the props regardless of change detection.
        topic_input.update()
        text_area.update()
        upload.reset()
        _reset_status()

    def _reset_and_close() -> None:
        _reset()
        dialog.close()

    # Belt and braces against the state leak: `Discard` is not the only way out
    # of the dialog — Escape and a click on the overlay close it too, and those
    # never reached _reset_and_close(), so the next open still showed the old
    # transcript. The dialog's value tracks open/closed, so this catches every
    # way of closing it, including the ones Quasar handles by itself.
    dialog.on_value_change(lambda e: _reset() if not e.value else None)

    async def _save() -> None:
        text = (text_area.value or "").strip()
        if not text:
            ui.notify(_("The memo is empty."), type="warning")
            return
        username = ng_app.storage.user.get("paperless_user", "")
        if not username:
            ui.notify(_("Not signed in."), type="negative")
            return
        save_btn.disable()
        try:
            # Not `_, rel = ...` — that would rebind the translator to a string
            # and make the very next `_(...)` call a TypeError.
            _memo_id, rel = await vault_memo_writer.create_memo(
                text, username, topic=(topic_input.value or "").strip()
            )
        except Exception as exc:  # surfaced, never swallowed — the memo is the user's words
            ui.notify(_("The memo could not be saved: {err}").format(err=exc), type="negative")
            return
        finally:
            save_btn.enable()
        ui.notify(_("Memo saved as {name}").format(name=rel.split("/")[-1]), type="positive")
        _reset_and_close()

    # Handed to on_click directly, never wrapped in `ensure_future`: that would
    # detach the coroutine from the client slot stack, and every `ui.notify`
    # after the first `await` would raise "the current slot cannot be determined".
    save_btn.on_click(_save)

    # ── JS → Python bridge for the finished recording ────────────────────────
    handler = ui.element("div").style("display:none;")

    def _on_result(e) -> None:
        args = e.args
        if isinstance(args, (list, tuple)):
            args = args[0] if args else None
        try:
            payload = json.loads(args) if isinstance(args, str) else (args or {})
        except (TypeError, ValueError):
            payload = {}

        # The status line is left to the recorder script — see _reset_status().
        if err := payload.get("error"):
            # "Nothing was recognised" is the guard working, not a breakage —
            # a red error toast for a mis-tap reads as though something broke.
            ui.notify(err, type="warning" if payload.get("soft") else "negative")
            return

        # Cancelled mid-recording: no transcription ran, nothing to fold in.
        if payload.get("cancelled"):
            _reset_and_close()
            return

        # Quick memo: the server has the audio and the dialog's job is done.
        # Closing here rather than in JS keeps the reset and the close together.
        if payload.get("quick"):
            ui.notify(_("Memo is being filed in the background …"), type="info")
            _reset_and_close()
            return

        _apply_payload(payload)

    handler.on("memoResult", _on_result)
    listener_id = list(handler._event_listeners.keys())[0]

    labels = {
        "hold": _("Hold to record, swipe up to lock"),
        "recording": _("Recording — release to stop, swipe up to lock"),
        "locked": _("Recording locked — tap to stop"),
        "working": _("Transcribing …"),
        "polishing": _("AI is tidying it up …"),
        "denied": _("Microphone access was denied."),
        "insecure": _(
            "Recording needs a secure connection (HTTPS). "
            "Type the memo instead, or reach the app over HTTPS or localhost."
        ),
        "failed": _("Transcription failed."),
        "tooLong": _("The recording is too long."),
        "sending": _("Filing in the background …"),
        "cancelled": _("Recording discarded."),
    }

    ui.add_head_html(f"""<script>
(function initMemoRecorder() {{
    var LABELS = {json.dumps(labels)};
    var MEMO_MAX_MS = {settings.memo_max_seconds * 1000};
    var CONV_MAX_MS = {settings.conversation_max_seconds * 1000};
    function memoMode() {{ return window.__memoMode === 'conversation' ? 'conversation' : 'memo'; }}
    var ENDPOINT = {json.dumps(TRANSCRIBE_PATH)};
    var REWRITE_ENDPOINT = {json.dumps(REWRITE_PATH)};
    var QUICK_ENDPOINT = {json.dumps(QUICK_PATH)};
    var ELEMENT_ID = {handler.id};
    var LISTENER_ID = {json.dumps(listener_id)};

    function emit(payload) {{
        if (!window.socket || !window.did_handshake) {{
            setTimeout(function() {{ emit(payload); }}, 100);
            return;
        }}
        window.socket.emit('event', {{
            id: ELEMENT_ID,
            client_id: window.clientId,
            listener_id: LISTENER_ID,
            args: [JSON.stringify(payload)]
        }});
    }}

    function setStatus(btn, text) {{
        var card = btn.closest('.q-card');
        var el = card ? card.querySelector('.memo-status') : null;
        if (el) el.textContent = text;
    }}

    // Quasar renders the icon as a material-icons ligature, so the glyph is the
    // element's text. Swapping it is how a locked recording says "tap to stop".
    function setIcon(btn, name) {{
        var el = btn.querySelector('.q-icon');
        if (el) el.textContent = name;
    }}

    function waitForBtn() {{
        var btn = document.querySelector('.memo-record-btn:not(.memo-quick-btn)');
        var quickBtn = document.querySelector('.memo-quick-btn');
        var cancelBtn = document.querySelector('.memo-cancel-btn');
        if (!btn) {{ setTimeout(waitForBtn, 200); return; }}
        if (btn.dataset.memoInit) return;
        btn.dataset.memoInit = '1';

        // getUserMedia only exists in a secure context (HTTPS or localhost).
        // Say so up front rather than failing on the first press.
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {{
            [btn, quickBtn].forEach(function(b) {{
                if (!b) return;
                b.disabled = true;
                b.style.opacity = '0.4';
            }});
            setStatus(btn, LABELS.insecure);
            return;
        }}

        var recorder = null, chunks = [], stream = null, timer = null, busy = false;
        // Which of the two buttons started this recording, and whether the user
        // pulled the plug on it. `cancelled` is read in onstop — that is the one
        // place that decides between uploading the audio and dropping it.
        // `activeBtn` is the button under the finger: every bit of recording
        // feedback (pressed state, lock, the stop glyph) has to land on THAT
        // button. Addressing `btn` directly meant a locked quick memo lit up the
        // mic button instead, so the stop icon appeared on a button that was not
        // recording anything.
        var quick = false, cancelled = false, activeBtn = btn;

        function showCancel(on) {{
            if (cancelBtn) cancelBtn.style.display = on ? '' : 'none';
        }}
        // Swipe-to-lock: drag up past LOCK_DIST px while holding and the
        // recording keeps running after the finger lifts, WhatsApp-style. The
        // next tap stops it.
        var LOCK_DIST = 48;
        var locked = false, startY = 0, starting = false, releasedEarly = false, gesture = false;

        // Both buttons are restored, not just the active one: a recording can
        // end from a timeout or an error, and a stale stop glyph on the other
        // button would be a button that lies about what it does.
        function resetButtons() {{
            [[btn, 'mic'], [quickBtn, 'bolt']].forEach(function(pair) {{
                if (!pair[0]) return;
                pair[0].classList.remove('memo-locked', 'memo-recording');
                setIcon(pair[0], pair[1]);
            }});
        }}

        function stop() {{
            if (timer) {{ clearTimeout(timer); timer = null; }}
            locked = false;
            resetButtons();
            showCancel(false);
            if (starting) releasedEarly = true;   // stop as soon as it has started
            if (recorder && recorder.state === 'recording') recorder.stop();
        }}

        // Abort: stop the recorder, throw the audio away, transcribe nothing.
        // The flag is set before stop() so onstop sees it — MediaRecorder fires
        // it asynchronously and there is no other way to tell the two apart.
        function cancel() {{
            if (!recorder && !starting) return;
            cancelled = true;
            chunks = [];
            stop();
            setStatus(btn, LABELS.cancelled);
            emit({{cancelled: true}});
        }}

        function release() {{
            gesture = false;
            // A locked recording ignores the release that armed it, and every
            // release after that.
            if (!locked) stop();
        }}

        function drag(e) {{
            // Keyed on the gesture, not on recorder.state: the very first press
            // waits behind the permission prompt, and a swipe during that window
            // must still arm the lock.
            if (locked || !gesture) return;
            if (startY - e.clientY < LOCK_DIST) return;
            locked = true;
            // The button under the finger, not `btn`: locking a quick memo used
            // to put the stop glyph on the mic button, which then looked like
            // the control for a recording it had not started.
            activeBtn.classList.add('memo-locked');
            setIcon(activeBtn, 'stop');
            setStatus(activeBtn, LABELS.locked);
        }}

        async function start(e, isQuick) {{
            e.preventDefault();
            // While locked the button is a stop button — the tap that stops it
            // must not open a second recording.
            if (locked) {{ stop(); return; }}
            if (busy || (recorder && recorder.state === 'recording')) return;
            quick = !!isQuick;
            cancelled = false;
            startY = e.clientY;
            gesture = true;
            // Without capture the pointer leaves the button a few pixels into
            // the swipe and the move events stop arriving, so locking could
            // never trigger.
            activeBtn = isQuick && quickBtn ? quickBtn : btn;
            try {{ activeBtn.setPointerCapture(e.pointerId); }} catch (err) {{}}
            // getUserMedia is async and may sit behind a permission prompt. A
            // release during that window has nothing to stop yet, so remember it.
            starting = true;
            releasedEarly = false;
            try {{
                stream = await navigator.mediaDevices.getUserMedia({{audio: true}});
            }} catch (err) {{
                starting = false;
                gesture = false;
                setStatus(btn, LABELS.denied);
                return;
            }}
            starting = false;
            chunks = [];
            recorder = new MediaRecorder(stream);
            recorder.ondataavailable = function(ev) {{
                if (ev.data && ev.data.size) chunks.push(ev.data);
            }};
            recorder.onstop = async function() {{
                resetButtons();
                showCancel(false);
                stream.getTracks().forEach(function(t) {{ t.stop(); }});
                // Cancelled: the audio never leaves the browser.
                if (cancelled) {{ chunks = []; cancelled = false; return; }}
                if (!chunks.length) {{ setStatus(btn, LABELS.hold); return; }}
                busy = true;
                setStatus(btn, LABELS.working);
                var blob = new Blob(chunks, {{type: recorder.mimeType || 'audio/webm'}});
                var fd = new FormData();
                fd.append('audio', blob, 'memo.webm');

                // Quick memo: hand the bytes over and stop caring. The server
                // answers 202 before it starts transcribing, so the dialog can
                // close while the work runs on — which is the whole point, and
                // why the upload cannot live on the page's lifetime.
                if (quick) {{
                    setStatus(btn, LABELS.sending);
                    try {{
                        var qResp = await fetch(QUICK_ENDPOINT + '?mode=' + memoMode(), {{
                            method: 'POST', body: fd, credentials: 'same-origin'
                        }});
                        var qData = await qResp.json().catch(function() {{ return {{}}; }});
                        setStatus(btn, LABELS.hold);
                        // 422 means the recording held nothing, which is a
                        // mis-tap and not a failure — flagged so Python can
                        // warn rather than raise a red error.
                        emit(qResp.ok
                            ? {{quick: true}}
                            : {{error: qData.error || LABELS.failed, soft: qResp.status === 422}});
                    }} catch (err) {{
                        setStatus(btn, LABELS.failed);
                        emit({{error: LABELS.failed}});
                    }}
                    busy = false;
                    return;
                }}
                // Every branch below must set the status again: this script owns
                // the label (it writes textContent straight into the DOM), so
                // Python cannot clear "Transcribing …" on its way back.
                try {{
                    // Two phases, two waits: Whisper first, then the model that
                    // tidies the transcript up. One request for both would leave
                    // the status line stuck on "Transcribing …" for the whole
                    // thing, which is the longer half of it.
                    var resp = await fetch(ENDPOINT + '?rewrite=false&mode=' + memoMode(), {{
                        method: 'POST', body: fd, credentials: 'same-origin'
                    }});
                    var data = await resp.json().catch(function() {{ return {{}}; }});
                    if (!resp.ok) {{
                        var msg = data.error || LABELS.failed;
                        setStatus(btn, msg);
                        emit({{error: msg, soft: resp.status === 422}});
                        busy = false;
                        return;
                    }}
                    setStatus(btn, LABELS.polishing);
                    var rResp = await fetch(REWRITE_ENDPOINT, {{
                        method: 'POST',
                        credentials: 'same-origin',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify({{text: data.text, mode: memoMode()}})
                    }});
                    var rData = await rResp.json().catch(function() {{ return {{}}; }});
                    setStatus(btn, LABELS.hold);
                    // A failed rewrite must not cost the user their words: fall
                    // back to the raw transcript rather than throwing it away.
                    emit(rResp.ok ? rData : {{text: data.text, transcript: data.text}});
                }} catch (err) {{
                    setStatus(btn, LABELS.failed);
                    emit({{error: LABELS.failed}});
                }}
                busy = false;
            }};
            recorder.start();
            activeBtn.classList.add('memo-recording');
            showCancel(true);
            setStatus(btn, LABELS.recording);
            // Hard stop so a stuck press cannot record forever. Read at start,
            // not at module load: the mode can change between recordings.
            var maxMs = memoMode() === 'conversation' ? CONV_MAX_MS : MEMO_MAX_MS;
            timer = setTimeout(function() {{ setStatus(btn, LABELS.tooLong); stop(); }}, maxMs);
            // Released while the permission prompt was up, without locking.
            if (releasedEarly && !locked) stop();
            else if (locked) setStatus(btn, LABELS.locked);
        }}

        // Both buttons drive the same recorder — only the flag differs, so a
        // recording cannot be started on one and finished on the other.
        [[btn, false], [quickBtn, true]].forEach(function(pair) {{
            var el = pair[0], isQuick = pair[1];
            if (!el) return;
            el.addEventListener('pointerdown', function(e) {{ start(e, isQuick); }});
            el.addEventListener('pointermove', drag);
            el.addEventListener('pointerup', release);
            el.addEventListener('pointercancel', release);
            // No 'pointerleave': the pointer is captured for the whole gesture,
            // so leaving the button is the swipe, not the end of the recording.
            // Holding a button normally starts a text selection / context menu
            el.addEventListener('contextmenu', function(e) {{ e.preventDefault(); }});
        }});

        if (cancelBtn) cancelBtn.addEventListener('click', function(e) {{
            e.preventDefault();
            e.stopPropagation();
            cancel();
        }});

        // Escape closes the dialog on its own, which would leave the recorder
        // running with nowhere to report back to. Abort it first.
        document.addEventListener('keydown', function(e) {{
            if (e.key !== 'Escape') return;
            if (recorder && recorder.state === 'recording') cancel();
        }});
    }}
    waitForBtn();
    document.addEventListener('click', function() {{ setTimeout(waitForBtn, 50); }});
}})();
</script>""")

    status.classes("memo-status")
    # Exposed so the header button can clear the form before showing it. The
    # dialog is built once per page load and reused for every memo after that.
    dialog.reset_memo = _reset
    return dialog
