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

from app_ui.memo_routes import TRANSCRIBE_PATH
from config.settings import settings
from i18n import get_translator
from services import transcription
from services.clients import vault_memo_writer
from services.session_auth import get_session_token

_ENABLED_KEY = "voice_memos_enabled"


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


def memo_button() -> None:
    """Header entry point. Renders nothing when the feature is off."""
    if not memo_enabled():
        return
    _ = get_translator()
    dialog = build_memo_dialog()
    ui.button(icon="mic", color=None, on_click=dialog.open).props(
        "flat dark dense"
    ).classes("nav-btn memo-nav-btn").tooltip(_("Voice memo"))


def build_memo_dialog():
    """Build the capture dialog. Returns the ui.dialog so a caller can open it."""
    _ = get_translator()

    state: dict = {"transcript": ""}

    with ui.dialog() as dialog, ui.card().classes("w-full max-w-xl gap-4 memo-card").style(
        "background:var(--c-surface); border:1px solid var(--c-border);"
    ):
        with ui.row().classes("w-full items-center justify-between gap-2"):
            ui.label(_("Voice memo")).classes("text-lg font-semibold").style(
                "color:var(--c-text);"
            )
            # Drives three things at once: speaker labels from the transcription
            # service, which rewrite prompt runs, and the length/size caps.
            mode_toggle = (
                ui.toggle(
                    {"memo": _("Memo"), "conversation": _("Conversation")},
                    value="memo",
                )
                .props("dense unelevated no-caps color=purple")
                .classes("memo-mode-toggle")
            )

        # ── Recording control ────────────────────────────────────────────────
        with ui.column().classes("w-full items-center gap-2"):
            rec_btn = (
                ui.button(icon="mic")
                .props("round unelevated size=lg")
                .classes("memo-record-btn")
            )
            status = ui.label(_("Hold to record, swipe up to lock")).classes(
                "text-sm text-center"
            ).style("color:var(--c-text-muted);")

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
        from app_ui.memo_routes import MemoInputError, transcribe_payload

        upload.reset()  # otherwise the picker keeps the old file and won't re-fire
        _set_status(_("Transcribing …"))
        try:
            payload = await transcribe_payload(
                await e.file.read(),
                filename=e.file.name or "memo.m4a",
                content_type=e.file.content_type or "application/octet-stream",
                rewrite=True,
                username=ng_app.storage.user.get("paperless_user", ""),
                token=get_session_token(),
                conversation=(mode_toggle.value == "conversation"),
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

    def _reset_and_close() -> None:
        state["transcript"] = ""
        topic_input.value = ""
        text_area.value = ""
        upload.reset()
        _reset_status()
        dialog.close()

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
            ui.notify(err, type="negative")
            return

        _apply_payload(payload)

    handler.on("memoResult", _on_result)
    listener_id = list(handler._event_listeners.keys())[0]

    labels = {
        "hold": _("Hold to record, swipe up to lock"),
        "recording": _("Recording — release to stop, swipe up to lock"),
        "locked": _("Recording locked — tap to stop"),
        "working": _("Transcribing …"),
        "denied": _("Microphone access was denied."),
        "insecure": _(
            "Recording needs a secure connection (HTTPS). "
            "Type the memo instead, or reach the app over HTTPS or localhost."
        ),
        "failed": _("Transcription failed."),
        "tooLong": _("The recording is too long."),
    }

    ui.add_head_html(f"""<script>
(function initMemoRecorder() {{
    var LABELS = {json.dumps(labels)};
    var MEMO_MAX_MS = {settings.memo_max_seconds * 1000};
    var CONV_MAX_MS = {settings.conversation_max_seconds * 1000};
    function memoMode() {{ return window.__memoMode === 'conversation' ? 'conversation' : 'memo'; }}
    var ENDPOINT = {json.dumps(TRANSCRIBE_PATH)};
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
        var btn = document.querySelector('.memo-record-btn');
        if (!btn) {{ setTimeout(waitForBtn, 200); return; }}
        if (btn.dataset.memoInit) return;
        btn.dataset.memoInit = '1';

        // getUserMedia only exists in a secure context (HTTPS or localhost).
        // Say so up front rather than failing on the first press.
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {{
            btn.disabled = true;
            btn.style.opacity = '0.4';
            setStatus(btn, LABELS.insecure);
            return;
        }}

        var recorder = null, chunks = [], stream = null, timer = null, busy = false;
        // Swipe-to-lock: drag up past LOCK_DIST px while holding and the
        // recording keeps running after the finger lifts, WhatsApp-style. The
        // next tap stops it.
        var LOCK_DIST = 48;
        var locked = false, startY = 0, starting = false, releasedEarly = false, gesture = false;

        function stop() {{
            if (timer) {{ clearTimeout(timer); timer = null; }}
            locked = false;
            btn.classList.remove('memo-locked');
            setIcon(btn, 'mic');
            if (starting) releasedEarly = true;   // stop as soon as it has started
            if (recorder && recorder.state === 'recording') recorder.stop();
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
            btn.classList.add('memo-locked');
            setIcon(btn, 'stop');
            setStatus(btn, LABELS.locked);
        }}

        async function start(e) {{
            e.preventDefault();
            // While locked the button is a stop button — the tap that stops it
            // must not open a second recording.
            if (locked) {{ stop(); return; }}
            if (busy || (recorder && recorder.state === 'recording')) return;
            startY = e.clientY;
            gesture = true;
            // Without capture the pointer leaves the button a few pixels into
            // the swipe and the move events stop arriving, so locking could
            // never trigger.
            try {{ btn.setPointerCapture(e.pointerId); }} catch (err) {{}}
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
                btn.classList.remove('memo-recording');
                stream.getTracks().forEach(function(t) {{ t.stop(); }});
                if (!chunks.length) {{ setStatus(btn, LABELS.hold); return; }}
                busy = true;
                setStatus(btn, LABELS.working);
                var blob = new Blob(chunks, {{type: recorder.mimeType || 'audio/webm'}});
                var fd = new FormData();
                fd.append('audio', blob, 'memo.webm');
                // Every branch below must set the status again: this script owns
                // the label (it writes textContent straight into the DOM), so
                // Python cannot clear "Transcribing …" on its way back.
                try {{
                    var resp = await fetch(ENDPOINT + '?rewrite=true&mode=' + memoMode(), {{
                        method: 'POST', body: fd, credentials: 'same-origin'
                    }});
                    var data = await resp.json().catch(function() {{ return {{}}; }});
                    if (!resp.ok) {{
                        var msg = data.error || LABELS.failed;
                        setStatus(btn, msg);
                        emit({{error: msg}});
                    }} else {{
                        setStatus(btn, LABELS.hold);
                        emit(data);
                    }}
                }} catch (err) {{
                    setStatus(btn, LABELS.failed);
                    emit({{error: LABELS.failed}});
                }}
                busy = false;
            }};
            recorder.start();
            btn.classList.add('memo-recording');
            setStatus(btn, LABELS.recording);
            // Hard stop so a stuck press cannot record forever. Read at start,
            // not at module load: the mode can change between recordings.
            var maxMs = memoMode() === 'conversation' ? CONV_MAX_MS : MEMO_MAX_MS;
            timer = setTimeout(function() {{ setStatus(btn, LABELS.tooLong); stop(); }}, maxMs);
            // Released while the permission prompt was up, without locking.
            if (releasedEarly && !locked) stop();
            else if (locked) setStatus(btn, LABELS.locked);
        }}

        btn.addEventListener('pointerdown', start);
        btn.addEventListener('pointermove', drag);
        btn.addEventListener('pointerup', release);
        btn.addEventListener('pointercancel', release);
        // No 'pointerleave': the pointer is captured for the whole gesture, so
        // leaving the button is the swipe, not the end of the recording.
        // Holding a button normally starts a text selection / context menu
        btn.addEventListener('contextmenu', function(e) {{ e.preventDefault(); }});
    }}
    waitForBtn();
    document.addEventListener('click', function() {{ setTimeout(waitForBtn, 50); }});
}})();
</script>""")

    status.classes("memo-status")
    return dialog
