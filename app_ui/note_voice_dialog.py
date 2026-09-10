"""Revise an open note by speaking to it.

Hold the button, say what should change, and the model returns the whole note
revised. The result is a *proposal*: it lands in an editable field next to a
diff, and nothing is written until the user accepts it.

Three things are deliberate:

- **The dictation is not content.** The memo path assumes everything spoken is
  material to file; here "delete the second bullet" is an instruction and
  "remember to call the insurance" is content, and telling the two apart is the
  model's job — see `services/note_edit_service.py`.
- **The body only.** The note's frontmatter never reaches the model and never
  comes back from it. `pbrain_id` and the properties have exactly one editor,
  and it is the properties panel.
- **Its own recorder script.** The gesture is the same as the memo button's
  (hold to talk, swipe up to lock, tap to stop) but the memo recorder is wired
  to the memo dialog's two buttons, its mode toggle and its endpoints. This is
  the third recorder in the app, after the memo dialog and the chat mic; they
  share the transcription endpoint, not the JS.
"""

import difflib
import json

from nicegui import ui

from app_ui.memo_dialog import memo_enabled
from app_ui.memo_routes import NOTE_EDIT_PATH, TRANSCRIBE_PATH
from config.settings import settings
from i18n import get_translator

_CSS = """<style>
.nvedit-diff {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.75rem; line-height: 1.5;
  max-height: 220px; overflow: auto;
  border: 1px solid var(--c-border); border-radius: 6px;
  background: var(--c-bg); padding: 6px 8px;
  /* A diff line is one unbreakable run of text far more often than prose is;
     without this the whole dialog scrolls sideways on a phone. */
  white-space: pre-wrap; overflow-wrap: anywhere;
}
.nvedit-diff .d-add { color: var(--c-text); }
.nvedit-diff .d-del { color: var(--c-warn); text-decoration: line-through; }
.nvedit-diff .d-ctx { color: var(--c-text-muted); }
.nvedit-diff .d-hdr { color: var(--c-text-muted); opacity: 0.7; }
/* The record button itself is styled in `app_ui/theme.py`, next to the memo
   button's rules: it is the same control and must not drift from it. Purple at
   rest would also spend the one accent meaning on a button that is neither the
   user's input nor an active selection. */
/* The proposal field is the only part allowed to consume leftover height —
   an autogrow textarea holding a long note otherwise pushes Accept off-screen. */
.nvedit-text-scroll { flex: 1; min-height: 0; overflow: auto; }
</style>"""


def note_voice_enabled() -> bool:
    """True when the feature should be offered at all.

    Same gate as the memo button: a configured transcription service plus the
    per-user switch. There is no second setting — a user who turned dictation
    off does not want a microphone on the note page either.
    """
    return memo_enabled()


def _diff_html(before: str, after: str, escape) -> str:
    """A unified diff of two note bodies, as HTML.

    Line-level and deliberately plain. What the user has to decide is "did it
    do what I said, and did it quietly eat anything" — for that, seeing which
    lines went and which arrived is enough, and a word-level highlight inside a
    reflowed markdown line is mostly noise.
    """
    before_lines = before.splitlines()
    after_lines = after.splitlines()
    if before_lines == after_lines:
        return ""
    out: list[str] = []
    diff = difflib.unified_diff(before_lines, after_lines, lineterm="", n=2)
    for line in diff:
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("@@"):
            # "@@ -1,8 +1,7 @@" answers a question nobody reading a note asked.
            # What the header is actually for is showing that a stretch was
            # skipped, so between hunks it becomes an ellipsis and the first one
            # — which separates nothing — is dropped.
            if out:
                out.append('<div class="d-hdr">⋯</div>')
            continue
        if line.startswith("+"):
            cls = "d-add"
        elif line.startswith("-"):
            cls = "d-del"
        else:
            cls = "d-ctx"
        # An empty line still has to occupy one, or the diff loses its shape.
        out.append(f'<div class="{cls}">{escape(line) or "&nbsp;"}</div>')
    return "".join(out)


def create_note_voice_dialog(get_note, apply_note):
    """Build the dialog once and return ``open_voice_edit()``.

    ``get_note()`` returns the note body as it currently stands; ``apply_note``
    is an async callable that takes the accepted text and owns everything that
    happens after — writing, marking dirty, saving.
    """
    _ = get_translator()
    state: dict = {"before": "", "proposal": False}

    ui.add_head_html(_CSS)

    def _escape(text: str) -> str:
        return (
            text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )

    # `persistent`: same reason as the memo dialog, and more so — this one can
    # hold a revision the model has already produced, which a backdrop tap
    # would throw away unrecoverably. The X and Discard are the ways out.
    with ui.dialog().props("full-width persistent").classes("nvedit-dialog") as dialog:
        with ui.card().classes("w-full max-w-2xl gap-3").style(
            "background:var(--c-surface); border:1px solid var(--c-border);"
            "max-height:88vh; display:flex; flex-direction:column;"
        ):
            with ui.row().classes("w-full items-center justify-between gap-2"):
                ui.label(_("Edit note by voice")).classes(
                    "text-lg font-semibold"
                ).style("color:var(--c-text);")
                ui.button(icon="close", on_click=lambda: dialog.close()).props(
                    "flat dense round"
                ).classes("card-action-btn")

            with ui.column().classes("w-full items-center gap-2"):
                with ui.row().classes("items-center justify-center gap-4"):
                    # `color=None` matters: with Quasar's default the button
                    # carries `bg-primary`, and the theme's recording background
                    # is a 12%-alpha amber — it would tint the purple instead of
                    # replacing it, so a live microphone would still read as the
                    # accent colour.
                    (
                        ui.button(icon="mic", color=None)
                        .props("round unelevated size=lg")
                        .classes("nvedit-record-btn")
                    )
                    # JS owns this button's visibility: only JS knows whether
                    # the recorder is running.
                    (
                        ui.button(icon="close", color=None)
                        .props("round flat size=md")
                        .classes("nvedit-cancel-btn")
                        .style("display:none;color:var(--c-text-2);")
                        .tooltip(_("Discard recording"))
                    )
                status = ui.label(
                    _("Hold to speak, swipe up to lock")
                ).classes("text-sm text-center nvedit-status").style(
                    "color:var(--c-text-muted);"
                )
                ui.label(
                    _("Say what should change, or simply dictate what to add.")
                ).classes("text-xs text-center").style("color:var(--c-text-muted);")

            summary_label = ui.label("").classes("text-sm w-full").style(
                "color:var(--c-text-2);"
            )
            summary_label.set_visibility(False)

            diff_box = ui.html("").classes("nvedit-diff w-full")
            diff_box.set_visibility(False)

            with ui.column().classes("w-full gap-0 nvedit-text-scroll"):
                text_area = (
                    ui.textarea(label=_("Note"))
                    .props("dark outlined autogrow")
                    # nvedit-text: the recorder reads the current text out of
                    # this field, so a second instruction acts on the proposal
                    # rather than on the note as it was three edits ago.
                    .classes("w-full nvedit-text")
                    .style("min-height:180px;")
                )

            with ui.row().classes("w-full justify-end gap-2 items-center"):
                revert_btn = ui.button(
                    _("Revert"), on_click=lambda: _revert()
                ).props("flat dense")
                revert_btn.set_visibility(False)
                ui.button(_("Discard"), on_click=lambda: dialog.close()).props(
                    "flat dense"
                )
                accept_btn = ui.button(_("Accept")).props(
                    "unelevated dense color=purple"
                )

    def _set_status(text: str) -> None:
        """Written through the DOM, like the memo dialog's: the recorder script
        owns this label via `textContent`, so Python's copy of the prop never
        changed and assigning the same value back would produce no update."""
        ui.run_javascript(
            "document.querySelectorAll('.nvedit-status').forEach(function(el) {"
            f" el.textContent = {json.dumps(text)}; }});"
        )

    def _reset_status() -> None:
        _set_status(_("Hold to speak, swipe up to lock"))

    def _show_proposal(text: str, summary: str) -> None:
        state["proposal"] = True
        text_area.value = text
        text_area.update()
        summary_label.text = summary or _("The note was revised.")
        summary_label.set_visibility(True)
        html = _diff_html(state["before"], text, _escape)
        diff_box.content = html or (
            f'<div class="d-ctx">{_escape(_("Nothing changed."))}</div>'
        )
        diff_box.set_visibility(True)
        revert_btn.set_visibility(True)

    def _revert() -> None:
        """Back to the note as it was when the dialog opened. The proposal is
        the model's guess; getting out of it must not cost a page reload."""
        state["proposal"] = False
        text_area.value = state["before"]
        text_area.update()
        summary_label.set_visibility(False)
        diff_box.set_visibility(False)
        revert_btn.set_visibility(False)
        _reset_status()

    def _reset() -> None:
        state["before"] = ""
        state["proposal"] = False
        text_area.value = ""
        text_area.update()
        summary_label.set_visibility(False)
        diff_box.set_visibility(False)
        revert_btn.set_visibility(False)
        _reset_status()

    async def _accept() -> None:
        text = text_area.value or ""
        if text.strip() == state["before"].strip():
            ui.notify(_("Nothing changed."), type="info")
            dialog.close()
            return
        await apply_note(text)
        ui.notify(_("Note updated."), type="positive")
        dialog.close()

    # Handed to on_click directly, never wrapped in a task: a detached coroutine
    # leaves the client slot stack, and the save it triggers may need to render
    # a conflict banner — which would then raise instead of showing.
    accept_btn.on_click(_accept)
    # Not every way out goes through Discard, and `persistent` only removes two
    # of them — without this the next open could still show the old proposal.
    dialog.on_value_change(lambda e: _reset() if not e.value else None)

    # ── JS → Python bridge ───────────────────────────────────────────────────
    handler = ui.element("div").style("display:none;")

    def _on_result(e) -> None:
        args = e.args
        if isinstance(args, (list, tuple)):
            args = args[0] if args else None
        try:
            payload = json.loads(args) if isinstance(args, str) else (args or {})
        except (TypeError, ValueError):
            payload = {}

        if err := payload.get("error"):
            # "Nothing was recognised" is the guard doing its job, not a
            # breakage — a red toast for a mis-tap reads as though it broke.
            ui.notify(err, type="warning" if payload.get("soft") else "negative")
            return
        if payload.get("cancelled"):
            _reset_status()
            return
        if not payload.get("changed", True):
            # Measured server-side, not read off the summary: the model returned
            # the note it was given. Showing its summary here would put a
            # confirmation over an empty diff.
            _set_status(_("The note is unchanged — the instruction was not "
                          "carried out. Try saying it more plainly."))
            return
        _show_proposal(payload.get("text", ""), payload.get("summary", ""))

    handler.on("noteVoiceResult", _on_result)
    listener_id = list(handler._event_listeners.keys())[0]

    labels = {
        "hold": _("Hold to speak, swipe up to lock"),
        "recording": _("Recording — release to stop, swipe up to lock"),
        "locked": _("Recording locked — tap to stop"),
        "working": _("Transcribing …"),
        "thinking": _("AI is revising the note …"),
        "denied": _("Microphone access was denied."),
        "insecure": _(
            "Recording needs a secure connection (HTTPS). "
            "Edit the note by hand, or reach the app over HTTPS or localhost."
        ),
        "failed": _("The note could not be revised."),
        "tooLong": _("The recording is too long."),
        "cancelled": _("Recording discarded."),
    }

    ui.add_head_html(f"""<script>
(function initNoteVoiceRecorder() {{
    var LABELS = {json.dumps(labels)};
    var MAX_MS = {settings.memo_max_seconds * 1000};
    var ENDPOINT = {json.dumps(TRANSCRIBE_PATH)};
    var EDIT_ENDPOINT = {json.dumps(NOTE_EDIT_PATH)};
    var ELEMENT_ID = {handler.id};
    var LISTENER_ID = {json.dumps(listener_id)};

    // Read straight from the DOM: the field's Python value only catches up
    // after a round trip, and the user may have edited it by hand since.
    function noteNow() {{
        var el = document.querySelector('.nvedit-text textarea');
        return el ? (el.value || '') : '';
    }}

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

    function setStatus(text) {{
        document.querySelectorAll('.nvedit-status').forEach(function(el) {{
            el.textContent = text;
        }});
    }}

    // Quasar renders the icon as a material-icons ligature, so the glyph is the
    // element's text. Swapping it is how a locked recording says "tap to stop".
    function setIcon(btn, name) {{
        var el = btn.querySelector('.q-icon');
        if (el) el.textContent = name;
    }}

    function waitForBtn() {{
        var btn = document.querySelector('.nvedit-record-btn');
        var cancelBtn = document.querySelector('.nvedit-cancel-btn');
        if (!btn) {{ setTimeout(waitForBtn, 200); return; }}
        if (btn.dataset.nveditInit) return;
        btn.dataset.nveditInit = '1';

        // getUserMedia only exists in a secure context (HTTPS or localhost).
        // Say so up front rather than failing on the first press.
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {{
            btn.disabled = true;
            btn.style.opacity = '0.4';
            setStatus(LABELS.insecure);
            return;
        }}

        var recorder = null, chunks = [], stream = null, timer = null, busy = false;
        var cancelled = false;
        // Swipe-to-lock: drag up past LOCK_DIST px while holding and the
        // recording keeps running after the finger lifts. The next tap stops it.
        var LOCK_DIST = 48;
        var locked = false, startY = 0, starting = false, releasedEarly = false, gesture = false;

        function showCancel(on) {{
            if (cancelBtn) cancelBtn.style.display = on ? '' : 'none';
        }}

        function resetButton() {{
            btn.classList.remove('nvedit-locked', 'nvedit-recording');
            setIcon(btn, 'mic');
        }}

        function stop() {{
            if (timer) {{ clearTimeout(timer); timer = null; }}
            locked = false;
            resetButton();
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
            setStatus(LABELS.cancelled);
            emit({{cancelled: true}});
        }}

        function release() {{
            gesture = false;
            if (!locked) stop();
        }}

        function drag(e) {{
            // Keyed on the gesture, not on recorder.state: the very first press
            // waits behind the permission prompt, and a swipe during that
            // window must still arm the lock.
            if (locked || !gesture) return;
            if (startY - e.clientY < LOCK_DIST) return;
            locked = true;
            btn.classList.add('nvedit-locked');
            setIcon(btn, 'stop');
            setStatus(LABELS.locked);
        }}

        async function start(e) {{
            e.preventDefault();
            // While locked the button is a stop button — the tap that stops it
            // must not open a second recording.
            if (locked) {{ stop(); return; }}
            if (busy || (recorder && recorder.state === 'recording')) return;
            cancelled = false;
            startY = e.clientY;
            gesture = true;
            // Without capture the pointer leaves the button a few pixels into
            // the swipe and the move events stop arriving, so locking could
            // never trigger.
            try {{ btn.setPointerCapture(e.pointerId); }} catch (err) {{}}
            starting = true;
            releasedEarly = false;
            try {{
                stream = await navigator.mediaDevices.getUserMedia({{audio: true}});
            }} catch (err) {{
                starting = false;
                gesture = false;
                setStatus(LABELS.denied);
                return;
            }}
            starting = false;
            chunks = [];
            // The note as it stands the moment the recording starts. Captured
            // here rather than in onstop so an autosave landing mid-sentence
            // cannot swap the base out from under the instruction.
            var noteBefore = noteNow();
            recorder = new MediaRecorder(stream);
            recorder.ondataavailable = function(ev) {{
                if (ev.data && ev.data.size) chunks.push(ev.data);
            }};
            recorder.onstop = async function() {{
                resetButton();
                showCancel(false);
                stream.getTracks().forEach(function(t) {{ t.stop(); }});
                // Cancelled: the audio never leaves the browser.
                if (cancelled) {{ chunks = []; cancelled = false; return; }}
                if (!chunks.length) {{ setStatus(LABELS.hold); return; }}
                busy = true;
                setStatus(LABELS.working);
                var blob = new Blob(chunks, {{type: recorder.mimeType || 'audio/webm'}});
                var fd = new FormData();
                fd.append('audio', blob, 'instruction.webm');
                // Every branch below must set the status again: this script owns
                // the label, so Python cannot clear "Transcribing …" for it.
                try {{
                    // Two phases, two waits. `rewrite=false`: the memo rewrite
                    // would tidy the instruction into a memo, and the raw words
                    // are what the edit prompt has to reason about.
                    var resp = await fetch(ENDPOINT + '?rewrite=false', {{
                        method: 'POST', body: fd, credentials: 'same-origin'
                    }});
                    var data = await resp.json().catch(function() {{ return {{}}; }});
                    if (!resp.ok) {{
                        var msg = data.error || LABELS.failed;
                        setStatus(msg);
                        emit({{error: msg, soft: resp.status === 422}});
                        busy = false;
                        return;
                    }}
                    setStatus(LABELS.thinking);
                    var eResp = await fetch(EDIT_ENDPOINT, {{
                        method: 'POST',
                        credentials: 'same-origin',
                        headers: {{'Content-Type': 'application/json'}},
                        body: JSON.stringify({{
                            note: noteBefore,
                            instruction: data.text
                        }})
                    }});
                    var eData = await eResp.json().catch(function() {{ return {{}}; }});
                    if (!eResp.ok) {{
                        var eMsg = eData.error || LABELS.failed;
                        setStatus(eMsg);
                        emit({{error: eMsg}});
                        busy = false;
                        return;
                    }}
                    setStatus(LABELS.hold);
                    emit(eData);
                }} catch (err) {{
                    setStatus(LABELS.failed);
                    emit({{error: LABELS.failed}});
                }}
                busy = false;
            }};
            recorder.start();
            btn.classList.add('nvedit-recording');
            showCancel(true);
            setStatus(LABELS.recording);
            timer = setTimeout(function() {{ setStatus(LABELS.tooLong); stop(); }}, MAX_MS);
            // Released while the permission prompt was up, without locking.
            if (releasedEarly && !locked) stop();
            else if (locked) setStatus(LABELS.locked);
        }}

        btn.addEventListener('pointerdown', start);
        btn.addEventListener('pointermove', drag);
        btn.addEventListener('pointerup', release);
        btn.addEventListener('pointercancel', release);
        // No 'pointerleave': the pointer is captured for the whole gesture, so
        // leaving the button is the swipe, not the end of the recording.
        btn.addEventListener('contextmenu', function(e) {{ e.preventDefault(); }});

        if (cancelBtn) cancelBtn.addEventListener('click', function(e) {{
            e.preventDefault();
            e.stopPropagation();
            cancel();
        }});

        // The dialog is `persistent`, so Escape no longer closes it — but it
        // still reads as "stop what you are doing", and a recording that keeps
        // running after it would be a button lying about its state.
        document.addEventListener('keydown', function(e) {{
            if (e.key !== 'Escape') return;
            if (recorder && recorder.state === 'recording') cancel();
        }});
    }}
    waitForBtn();
    document.addEventListener('click', function() {{ setTimeout(waitForBtn, 50); }});
}})();
</script>""")

    def open_voice_edit() -> None:
        _reset()
        state["before"] = get_note() or ""
        text_area.value = state["before"]
        text_area.update()
        dialog.open()
        # After open(), so the label exists in the DOM to be written to.
        _reset_status()

    return open_voice_edit
