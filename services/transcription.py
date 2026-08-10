"""Speech-to-text via any OpenAI-compatible /v1/audio/transcriptions endpoint.

Not shipped with the app — the user points `WHISPER_URL` at their own service
(Speaches, hwdsl2/whisper-server, whisper.cpp --inference-path, Groq, OpenAI …).
Same bring-your-own-endpoint principle as the chat model registry: no hardcoded
provider, no bundled model, no new runtime dependency.
"""

import logging

import httpx

from config.settings import settings

_log = logging.getLogger(__name__)

# Transcription is CPU-bound on a self-hosted box. Measured reference: ~6s for
# 30s of speech on an 8-core CPU with large-v3-turbo, so this is generous
# headroom for a long memo rather than a tight bound.
_TIMEOUT = httpx.Timeout(connect=5.0, read=180.0, write=30.0, pool=5.0)


class TranscriptionError(Exception):
    """Raised with a message intended to be shown to the user."""


def is_configured() -> bool:
    return bool(settings.whisper_url.strip())


def _speaker_label(raw: str, order: list[str]) -> str:
    """`SPEAKER_00` → `Speaker 1`, numbered by first appearance.

    The diarizer's own numbering reflects clustering order, not who spoke first,
    so a transcript could open with "Speaker 3". Renumbering by appearance is
    what makes the result readable.
    """
    if raw not in order:
        order.append(raw)
    return f"Speaker {order.index(raw) + 1}"


def _turns_from_segments(segments: list[dict]) -> str:
    """Merge diarized segments into speaker turns.

    Whisper emits a segment every few seconds, so one person talking for a
    minute is a dozen segments with the same speaker. Emitting a label per
    segment would bury the conversation in repeated headers; consecutive
    segments by the same speaker become one turn.
    """
    order: list[str] = []
    turns: list[tuple[str, list[str]]] = []
    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        speaker = _speaker_label(str(seg.get("speaker") or "SPEAKER_00"), order)
        if turns and turns[-1][0] == speaker:
            turns[-1][1].append(text)
        else:
            turns.append((speaker, [text]))
    # A single speaker means diarization found nothing to separate — labelling
    # every line "Speaker 1" would be noise, so fall back to plain text.
    if len(order) < 2:
        return " ".join(" ".join(parts) for _s, parts in turns).strip()
    return "\n\n".join(f"**{s}:** {' '.join(parts)}" for s, parts in turns).strip()


async def transcribe(
    audio: bytes,
    filename: str,
    content_type: str,
    diarize: bool = False,
    language: str | None = None,
) -> str:
    """POST the recording, return the transcript text.

    The audio is forwarded exactly as the browser recorded it (usually
    webm/opus). Every service listed above runs the upload through ffmpeg, so
    no client-side transcoding is needed.

    ``language`` (ISO-639-1) overrides the server-wide ``WHISPER_LANGUAGE`` for
    this one request — the language a user speaks is a per-user fact, while the
    env var is only the installation's default. Pass ``"auto"`` to force
    detection even when a default is configured.

    With ``diarize`` the transcript comes back as ``**Speaker 1:** …`` turns.
    That needs ``response_format=verbose_json``: speaker labels live on the
    segments, never in the flat ``text`` field. Services without diarization
    still answer verbose_json, just without a ``speaker`` key — which collapses
    to the same plain text as before.
    """
    if not is_configured():
        raise TranscriptionError("No transcription service configured.")

    url = f"{settings.whisper_url.rstrip('/')}/audio/transcriptions"
    headers = {}
    if settings.whisper_api_key:
        headers["Authorization"] = f"Bearer {settings.whisper_api_key}"

    data = {"model": settings.whisper_model or "whisper-1"}
    lang = (language or "").strip() or settings.whisper_language
    # "auto" is not a language the API knows — omitting the field is how you ask
    # Whisper to detect one.
    if lang and lang.strip().lower() != "auto":
        data["language"] = lang
    if diarize:
        data["response_format"] = "verbose_json"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                url,
                headers=headers,
                data=data,
                files={"file": (filename, audio, content_type)},
            )
    except httpx.TimeoutException as exc:
        raise TranscriptionError("The transcription service timed out.") from exc
    except httpx.HTTPError as exc:
        _log.warning("transcription request to %s failed: %s", url, exc)
        raise TranscriptionError("The transcription service is unreachable.") from exc

    if resp.status_code == 401 or resp.status_code == 403:
        raise TranscriptionError("The transcription service rejected the API key.")
    if resp.status_code == 413:
        raise TranscriptionError("The recording is too large for the transcription service.")
    if resp.status_code >= 400:
        _log.warning("transcription failed: HTTP %s — %s", resp.status_code, resp.text[:400])
        raise TranscriptionError(
            f"The transcription service returned an error ({resp.status_code})."
        )

    try:
        body = resp.json()
    except ValueError as exc:
        raise TranscriptionError("The transcription service returned an unreadable response.") from exc

    if diarize:
        segments = body.get("segments") or []
        # No segments at all means the service ignored verbose_json. Falling back
        # to `text` keeps a working transcript instead of an empty memo — the
        # user loses the speaker labels, not their words.
        if segments:
            return _turns_from_segments(segments)
        _log.info("no segments in verbose_json response — falling back to plain text")
    return (body.get("text") or "").strip()
