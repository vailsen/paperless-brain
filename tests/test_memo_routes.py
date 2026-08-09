"""The shared transcribe helper behind both memo entry points.

The recorder and the "Upload audio file" button go through `transcribe_payload`.
Anything skipped here is skipped for both, so the guards that matter — auth, the
size cap and the silence check — are pinned by tests rather than by the caller
remembering to apply them.
"""

import asyncio

import pytest

from app_ui import memo_routes as R
from config.settings import settings
from services import transcription


def _run(coro):
    return asyncio.run(coro)


def _call(**kw):
    params = {
        "filename": "memo.m4a",
        "content_type": "audio/mp4",
        "rewrite": False,
        "username": "alice",
        "token": "tok",
    }
    params.update(kw)
    data = params.pop("data", b"audio-bytes")
    return _run(R.transcribe_payload(data, **params))


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(transcription, "is_configured", lambda: True)


@pytest.fixture
def transcribes(monkeypatch, configured):
    """Return whatever the test puts in `box`, recording the call arguments."""
    box = {"text": "This is a real dictated memo with enough words.", "seen": {}}

    async def fake(data, *, filename, content_type, diarize=False):
        box["seen"] = {
            "data": data,
            "filename": filename,
            "content_type": content_type,
            "diarize": diarize,
        }
        return box["text"]

    monkeypatch.setattr(transcription, "transcribe", fake)
    return box


def test_rejects_anonymous_caller(configured):
    with pytest.raises(R.MemoInputError) as exc:
        _call(username="")
    assert exc.value.status == 401


def test_rejects_when_no_service_configured(monkeypatch):
    monkeypatch.setattr(transcription, "is_configured", lambda: False)
    with pytest.raises(R.MemoInputError) as exc:
        _call()
    assert exc.value.status == 503


def test_rejects_empty_upload(configured):
    with pytest.raises(R.MemoInputError) as exc:
        _call(data=b"")
    assert exc.value.status == 400


def test_enforces_the_size_cap(monkeypatch, configured):
    monkeypatch.setattr(settings, "memo_max_upload_mb", 1, raising=False)
    with pytest.raises(R.MemoInputError) as exc:
        _call(data=b"x" * (1024 * 1024 + 1))
    assert exc.value.status == 413
    assert "1 MB" in exc.value.message


def test_silence_is_rejected_not_filed(transcribes):
    # Whisper's stock hallucination for a silent clip — the whole reason an
    # `if not text` check is not enough.
    transcribes["text"] = "Vielen Dank."
    with pytest.raises(R.MemoInputError) as exc:
        _call()
    assert exc.value.status == 422


def test_raw_transcript_without_rewrite(transcribes):
    out = _call(rewrite=False)
    assert out == {"text": transcribes["text"]}
    # Filename and content type must reach the service: it picks the decoder
    # from them, so an m4a announced as webm can fail to decode.
    assert transcribes["seen"]["filename"] == "memo.m4a"
    assert transcribes["seen"]["content_type"] == "audio/mp4"


def test_rewrite_returns_topic_text_and_raw_transcript(monkeypatch, transcribes):
    monkeypatch.setattr(R, "_memo_model", lambda u, t: "some-model")

    async def fake_rewrite(text, *, model, user_id, token, conversation=False):
        return "Car insurance", "- cleaned up\n- as markdown"

    monkeypatch.setattr(R.memo_service, "rewrite_dictation", fake_rewrite)

    out = _call(rewrite=True)
    assert out["topic"] == "Car insurance"
    assert out["text"] == "- cleaned up\n- as markdown"
    # The raw words survive alongside the rewrite, so nothing the user said is
    # only available in a model's paraphrase.
    assert out["transcript"] == transcribes["text"]


def test_transcription_failure_becomes_actionable_message(monkeypatch, configured):
    async def boom(data, *, filename, content_type, diarize=False):
        raise transcription.TranscriptionError("Whisper endpoint refused the API key.")

    monkeypatch.setattr(transcription, "transcribe", boom)
    with pytest.raises(R.MemoInputError) as exc:
        _call()
    assert exc.value.status == 502
    assert "API key" in exc.value.message


# ── Conversation mode ─────────────────────────────────────────────────────────


def test_conversation_mode_asks_for_diarization(transcribes):
    _call(conversation=True)
    assert transcribes["seen"]["diarize"] is True


def test_memo_mode_does_not_ask_for_diarization(transcribes):
    _call()
    assert transcribes["seen"]["diarize"] is False


def test_conversation_mode_uses_the_larger_size_cap(monkeypatch, transcribes):
    monkeypatch.setattr(settings, "memo_max_upload_mb", 1, raising=False)
    monkeypatch.setattr(settings, "conversation_max_upload_mb", 50, raising=False)
    big = b"x" * (2 * 1024 * 1024)

    # A meeting recording that a memo would reject.
    _call(data=big, conversation=True)

    with pytest.raises(R.MemoInputError) as exc:
        _call(data=big, conversation=False)
    assert exc.value.status == 413
    assert "1 MB" in exc.value.message


def test_conversation_mode_selects_the_dialog_prompt(monkeypatch, transcribes):
    monkeypatch.setattr(R, "_memo_model", lambda u, t: "some-model")
    seen = {}

    async def fake_rewrite(text, *, model, user_id, token, conversation=False):
        seen["conversation"] = conversation
        return "Kitchen quote", text

    monkeypatch.setattr(R.memo_service, "rewrite_dictation", fake_rewrite)

    _call(rewrite=True, conversation=True)
    assert seen["conversation"] is True
    _call(rewrite=True, conversation=False)
    assert seen["conversation"] is False
