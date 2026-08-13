"""Upload endpoint for dictated audio.

One route serves both callers. The chat mic wants the **raw** transcript — the
user is composing their own message and a rewrite would put words in their
mouth. The memo dialog additionally runs the Phase 2 rewrite and gets a topic
back. See `docs/voice-memos-tasks.md`.

The route is authenticated: it accepts a file and spends real CPU on it, so an
anonymous caller must not be able to reach the transcription service through it.

`transcribe_payload` holds the actual work so the in-process caller (the memo
dialog's "Upload audio file" button, which already has a session) shares one
code path with the HTTP route — including the size cap and the silence guard,
which are the two things that must never be skipped.
"""

import asyncio
import logging

from fastapi import UploadFile
from fastapi.responses import JSONResponse
from nicegui import app as ng_app

from config.settings import settings
from services import memo_service, transcription
from services.credential_store import load_credentials
from services.session_auth import get_session_token

_log = logging.getLogger(__name__)

TRANSCRIBE_PATH = "/api/memo/transcribe"
REWRITE_PATH = "/api/memo/rewrite"
QUICK_PATH = "/api/memo/quick"


class MemoInputError(Exception):
    """Rejected input, with the message the user should see.

    Carries the HTTP status so the route can answer with it; the in-process
    caller ignores the number and shows the text.
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


# 422 is the "there was nothing in that recording" status throughout this
# module. It is the one failure that is the user's to fix and the only one that
# is not worth a stack trace in the log.
NOTHING_RECOGNISED = "Nothing was recognised in the recording."


def _failure_for(data: bytes, message: str) -> tuple[int, str]:
    """Status and message for a transcription that did not come back.

    On a few seconds of audio the difference between "the service is down" and
    "you recorded nothing" is invisible to the user and the remedy is the same,
    so the short case gets the plainer message. A failure on a long recording
    keeps the real error — that one is worth knowing about.
    """
    if len(data) < memo_service.SHORT_AUDIO_BYTES:
        return 422, NOTHING_RECOGNISED
    return 502, message


def _memo_model(username: str, token: str) -> str:
    """Model used to tidy up a dictation and name it.

    A dedicated setting, because the rewrite wants a different trade-off than
    chat: it is a short, cheap, strictly-formatted job, so a small local model
    is usually the right choice even for a user who chats with Claude. Falls
    back to the chat model so the feature still works before anyone visits
    settings.
    """
    try:
        creds = load_credentials(username, token) if username and token else {}
    except Exception:  # a broken credential file must not kill the transcript
        _log.warning("could not read credentials for %s", username, exc_info=True)
        creds = {}
    return (creds.get("memo_model") or "").strip() or ng_app.storage.user.get(
        "chat_model_name", ""
    )


def upload_limit_mb(conversation: bool) -> int:
    """Size cap in MB. A conversation is a recording of a meeting, not a memo."""
    return (
        settings.conversation_max_upload_mb
        if conversation
        else settings.memo_max_upload_mb
    )


async def transcribe_payload(
    data: bytes,
    *,
    filename: str,
    content_type: str,
    rewrite: bool,
    username: str,
    token: str,
    conversation: bool = False,
    language: str | None = None,
) -> dict:
    """Audio bytes → ``{"text": …}``, or ``{"topic", "text", "transcript"}``.

    ``conversation`` asks the service for speaker-labelled turns and runs the
    dialog-preserving rewrite instead of the memo one.

    Raises ``MemoInputError`` for anything the user can act on.
    """
    if not username:
        raise MemoInputError(401, "Not signed in.")

    if not transcription.is_configured():
        raise MemoInputError(503, "No transcription service configured.")

    if not data:
        raise MemoInputError(400, "The recording is empty.")
    limit_mb = upload_limit_mb(conversation)
    if len(data) > limit_mb * 1024 * 1024:
        raise MemoInputError(413, f"The recording exceeds {limit_mb} MB.")
    # A mis-tap never reaches the transcription service: there is nothing in it
    # to transcribe, and some services answer a stub recording with a 500 that
    # would reach the user as an alarming error about a service that is fine.
    if memo_service.is_too_short_for_speech(data):
        raise MemoInputError(422, NOTHING_RECOGNISED)

    try:
        text = await transcription.transcribe(
            data,
            filename=filename,
            content_type=content_type,
            diarize=conversation,
            language=language,
        )
    except transcription.TranscriptionError as exc:
        raise MemoInputError(*_failure_for(data, str(exc))) from exc
    except Exception as exc:  # never leak a stack trace to the browser
        _log.exception("transcription failed")
        raise MemoInputError(*_failure_for(data, "Transcription failed.")) from exc

    # Whisper answers non-speech with a confident stock phrase rather than an
    # empty string, so this cannot be an `if not text` check.
    if memo_service.looks_like_silence(text):
        raise MemoInputError(422, NOTHING_RECOGNISED)
    # The other, worse failure: pages of fluent invented text from a recording
    # that held nothing. Logged in full, because unlike a stock phrase this one
    # is worth being able to inspect after the fact.
    if memo_service.looks_like_hallucination(text, len(data)):
        _log.warning(
            "discarding a %d-char transcript from %d bytes of audio for %s: %r",
            len(text), len(data), username, text[:300],
        )
        raise MemoInputError(422, NOTHING_RECOGNISED)

    if not rewrite:
        return {"text": text}

    return await rewrite_payload(
        text, username=username, token=token, conversation=conversation
    )


async def rewrite_payload(
    text: str,
    *,
    username: str,
    token: str,
    conversation: bool = False,
) -> dict:
    """Transcript → ``{"topic", "text", "transcript"}``.

    Split out from the transcription so the UI can tell the two phases apart:
    waiting on Whisper and waiting on the model are different waits, and a
    status line that says "Transcribing …" through both of them is a lie.

    Raises ``MemoInputError`` for anything the user can act on.
    """
    if not username:
        raise MemoInputError(401, "Not signed in.")
    raw = (text or "").strip()
    if not raw:
        raise MemoInputError(400, "There is nothing to tidy up.")

    model = _memo_model(username, token)
    if not model:
        # Without a model the rewrite degrades to the raw transcript and a topic
        # cut from its first words — visibly worse, and nothing in the UI says why.
        _log.warning("no memo model configured for %s — filing the raw transcript", username)
    topic, cleaned = await memo_service.rewrite_dictation(
        raw, model=model, user_id=username, token=token, conversation=conversation
    )
    return {"topic": topic, "text": cleaned, "transcript": raw}


@ng_app.post(TRANSCRIBE_PATH)
async def transcribe_audio(
    audio: UploadFile,
    rewrite: bool = False,
    mode: str = "memo",
) -> JSONResponse:
    """Transcribe an uploaded recording. Optionally clean it up into a memo."""
    from app_ui.memo_dialog import dictation_language

    try:
        username = ng_app.storage.user.get("paperless_user", "")
        token = get_session_token()
        language = dictation_language()
    except Exception:  # no session context at all
        username, token, language = "", "", None

    try:
        payload = await transcribe_payload(
            await audio.read(),
            filename=audio.filename or "memo.webm",
            content_type=audio.content_type or "application/octet-stream",
            rewrite=rewrite,
            username=username,
            token=token,
            conversation=(mode == "conversation"),
            language=language,
        )
    except MemoInputError as exc:
        return _error(exc.status, exc.message)
    return JSONResponse(payload)


# ── Quick memo: fire and forget ───────────────────────────────────────────────
#
# The dialog closes the moment the recording stops and the rest happens here.
# Three consequences shape this code:
#
# * The work must NOT be tied to the client. A task started from a page dies
#   when the user navigates away or the PWA is backgrounded — which is precisely
#   when a fire-and-forget memo is used. So it runs as an app-level task and
#   keeps everything it needs (bytes, user, token) as plain arguments.
# * It must be serialized. Local inference is one GPU; three quick memos in a
#   row would otherwise queue up inside Ollama and time out.
# * A failure must be visible, and the audio is never written to disk. Parking
#   the recording was meant to make a failure recoverable, but it silently
#   accumulated the user's voice in a folder nobody looks at, and the path in
#   the notice was noise to the only person who ever read it. The audio is
#   dropped and the notice says what went wrong.

_quick_lane = asyncio.Semaphore(1)
_quick_tasks: set[asyncio.Task] = set()
_notices: dict[str, list[dict]] = {}


def _push_notice(username: str, kind: str, message: str) -> None:
    """Queue a message for the user's next connected page."""
    _notices.setdefault(username, []).append({"kind": kind, "message": message})


def pop_notices(username: str) -> list[dict]:
    """Take everything queued for `username`. Drained by the header poller."""
    return _notices.pop(username, [])


async def _process_quick_memo(
    data: bytes,
    *,
    filename: str,
    content_type: str,
    username: str,
    token: str,
    conversation: bool,
    language: str | None,
) -> None:
    async with _quick_lane:
        try:
            payload = await transcribe_payload(
                data,
                filename=filename,
                content_type=content_type,
                rewrite=True,
                username=username,
                token=token,
                conversation=conversation,
                language=language,
            )
        except MemoInputError as exc:
            # A 422 is the guard doing its job, not a lost memo — there was
            # nothing in that recording worth telling the user about twice.
            kind = "warning" if exc.status == 422 else "error"
            _push_notice(username, kind, exc.message)
            return
        except Exception:
            _log.exception("quick memo failed for %s", username)
            _push_notice(
                username, "error", "The memo could not be transcribed."
            )
            return

        from services.clients import vault_memo_writer

        try:
            _memo_id, rel = await vault_memo_writer.create_memo(
                payload["text"], username, topic=payload.get("topic", "")
            )
        except Exception:
            _log.exception("quick memo could not be filed for %s", username)
            _push_notice(username, "error", "The memo could not be saved.")
            return
        _push_notice(username, "ok", rel.split("/")[-1])


@ng_app.post(QUICK_PATH)
async def quick_memo(audio: UploadFile, mode: str = "memo") -> JSONResponse:
    """Accept a recording and answer immediately; file it in the background."""
    from app_ui.memo_dialog import dictation_language

    try:
        username = ng_app.storage.user.get("paperless_user", "")
        token = get_session_token()
        language = dictation_language()
    except Exception:
        username, token, language = "", "", None

    if not username:
        return _error(401, "Not signed in.")
    if not transcription.is_configured():
        return _error(503, "No transcription service configured.")

    data = await audio.read()
    if not data:
        return _error(400, "The recording is empty.")
    conversation = mode == "conversation"
    limit_mb = upload_limit_mb(conversation)
    # Checked here rather than in the task: a size rejection is the one failure
    # the caller is still around to be told about.
    if len(data) > limit_mb * 1024 * 1024:
        return _error(413, f"The recording exceeds {limit_mb} MB.")
    # Answered here rather than as a background notice: a mis-tap is caught
    # before the dialog closes, so the user is told straight away instead of
    # getting a warning toast seconds later about a memo they never made.
    if memo_service.is_too_short_for_speech(data):
        return _error(422, NOTHING_RECOGNISED)

    task = asyncio.create_task(
        _process_quick_memo(
            data,
            filename=audio.filename or "memo.webm",
            content_type=audio.content_type or "application/octet-stream",
            username=username,
            token=token,
            conversation=conversation,
            language=language,
        )
    )
    # asyncio only keeps a weak reference to running tasks — without this the
    # memo can be garbage-collected mid-transcription.
    _quick_tasks.add(task)
    task.add_done_callback(_quick_tasks.discard)
    return JSONResponse({"queued": True}, status_code=202)


@ng_app.post(REWRITE_PATH)
async def rewrite_transcript(body: dict) -> JSONResponse:
    """Second phase of the memo path: tidy an existing transcript into a memo.

    Its own route so the recorder can flip the status line the moment the
    transcript is back and the model takes over.
    """
    try:
        username = ng_app.storage.user.get("paperless_user", "")
        token = get_session_token()
    except Exception:  # no session context at all
        username, token = "", ""

    try:
        payload = await rewrite_payload(
            str(body.get("text") or ""),
            username=username,
            token=token,
            conversation=(body.get("mode") == "conversation"),
        )
    except MemoInputError as exc:
        return _error(exc.status, exc.message)
    return JSONResponse(payload)
