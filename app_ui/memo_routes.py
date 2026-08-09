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

    try:
        text = await transcription.transcribe(
            data,
            filename=filename,
            content_type=content_type,
            diarize=conversation,
        )
    except transcription.TranscriptionError as exc:
        raise MemoInputError(502, str(exc)) from exc
    except Exception as exc:  # never leak a stack trace to the browser
        _log.exception("transcription failed")
        raise MemoInputError(502, "Transcription failed.") from exc

    # Whisper answers non-speech with a confident stock phrase rather than an
    # empty string, so this cannot be an `if not text` check.
    if memo_service.looks_like_silence(text):
        raise MemoInputError(422, "Nothing was recognised in the recording.")

    if not rewrite:
        return {"text": text}

    model = _memo_model(username, token)
    if not model:
        # Without a model the rewrite degrades to the raw transcript and a topic
        # cut from its first words — visibly worse, and nothing in the UI says why.
        _log.warning("no memo model configured for %s — filing the raw transcript", username)
    topic, cleaned = await memo_service.rewrite_dictation(
        text, model=model, user_id=username, token=token, conversation=conversation
    )
    return {"topic": topic, "text": cleaned, "transcript": text}


@ng_app.post(TRANSCRIBE_PATH)
async def transcribe_audio(
    audio: UploadFile,
    rewrite: bool = False,
    mode: str = "memo",
) -> JSONResponse:
    """Transcribe an uploaded recording. Optionally clean it up into a memo."""
    try:
        username = ng_app.storage.user.get("paperless_user", "")
        token = get_session_token()
    except Exception:  # no session context at all
        username, token = "", ""

    try:
        payload = await transcribe_payload(
            await audio.read(),
            filename=audio.filename or "memo.webm",
            content_type=audio.content_type or "application/octet-stream",
            rewrite=rewrite,
            username=username,
            token=token,
            conversation=(mode == "conversation"),
        )
    except MemoInputError as exc:
        return _error(exc.status, exc.message)
    return JSONResponse(payload)
