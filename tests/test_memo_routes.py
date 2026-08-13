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
    # Comfortably past both byte thresholds, so a test that is about something
    # else is not caught by the short-recording guard.
    data = params.pop("data", b"x" * (64 * 1024))
    return _run(R.transcribe_payload(data, **params))


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(transcription, "is_configured", lambda: True)


@pytest.fixture
def transcribes(monkeypatch, configured):
    """Return whatever the test puts in `box`, recording the call arguments."""
    box = {"text": "This is a real dictated memo with enough words.", "seen": {}}

    async def fake(data, *, filename, content_type, diarize=False, language=None):
        box["seen"] = {
            "data": data,
            "filename": filename,
            "content_type": content_type,
            "diarize": diarize,
            "language": language,
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
    async def boom(data, *, filename, content_type, diarize=False, language=None):
        raise transcription.TranscriptionError("Whisper endpoint refused the API key.")

    monkeypatch.setattr(transcription, "transcribe", boom)
    with pytest.raises(R.MemoInputError) as exc:
        _call()
    assert exc.value.status == 502
    assert "API key" in exc.value.message


# ── Short recordings ──────────────────────────────────────────────────────────
#
# A mis-tap is the common case for a button held for a fraction of a second,
# and it must read as "nothing was understood" rather than as a broken service.


def test_a_mis_tap_never_reaches_the_service(monkeypatch, configured):
    """Too small to hold speech — so there is nothing to ask the service about."""
    called = []

    async def fake(data, **kw):
        called.append(data)
        return "text"

    monkeypatch.setattr(transcription, "transcribe", fake)
    with pytest.raises(R.MemoInputError) as exc:
        _call(data=b"x" * 500)
    assert exc.value.status == 422
    assert exc.value.message == R.NOTHING_RECOGNISED
    assert not called


def test_a_service_error_on_a_short_clip_reads_as_nothing_recognised(
    monkeypatch, configured
):
    """The 500 some services answer a near-empty clip with is not an outage."""
    async def boom(data, *, filename, content_type, diarize=False, language=None):
        raise transcription.TranscriptionError(
            "The transcription service returned an error (500)."
        )

    monkeypatch.setattr(transcription, "transcribe", boom)
    with pytest.raises(R.MemoInputError) as exc:
        _call(data=b"x" * (8 * 1024))
    assert exc.value.status == 422
    assert exc.value.message == R.NOTHING_RECOGNISED
    # The service's own wording must not survive — that is the scary part.
    assert "500" not in exc.value.message


def test_a_wall_of_invented_text_from_a_short_clip_is_discarded(transcribes):
    """The real thing: Whisper returning memorised broadcast boilerplate.

    Verbatim from a test recording of roughly two seconds of silence. It is
    fluent, structured and entirely invented — nothing in the stock-phrase list
    can catch it, only the arithmetic can.
    """
    transcribes["text"] = (
        "Referenznummer der zugrunde liegenden Richtlinie: UH-RL-2018-04. "
        "Ansprechpartner für Rückfragen ist die Untertitelredaktion BR, Kontakt "
        "über den üblichen Verteiler.\n\n"
        "1. Format: VTT-Datei, UTF-8-Kodierung.\n"
        "2. Maximale Zeilenlänge: 42 Zeichen pro Zeile.\n"
        "3. Maximale Dauer pro Cue: 6 Sekunden.\n"
        "4. Mindestdauer pro Cue: 1 Sekunde.\n"
        "5. Gap zwischen Cues: mindestens 4 Frames, ca. 160 ms bei 25 fps.\n"
        "6. Position: Standard ist Bottom-Center.\n"
        "7. Schriftsprache: durchgehend deutsche Rechtschreibung.\n"
        "8. Sprecherwechsel im Off-Text mit zwei Gedankenstrichen kennzeichnen.\n"
    )
    with pytest.raises(R.MemoInputError) as exc:
        _call(data=b"x" * (8 * 1024))
    assert exc.value.status == 422
    assert exc.value.message == R.NOTHING_RECOGNISED


def test_a_normal_memo_is_not_mistaken_for_a_hallucination(transcribes):
    """The guard must have real headroom or it eats genuine memos."""
    # ~30s of speech: about 450 characters, from ~120 KB of webm at 32 kbps.
    transcribes["text"] = "Ich muss morgen die Bremsbeläge wechseln lassen. " * 9
    out = _call(rewrite=False, data=b"x" * (120 * 1024))
    assert out["text"] == transcribes["text"]


def test_a_long_dictation_is_kept(transcribes):
    """A five-minute memo produces a lot of text, and all of it is real."""
    transcribes["text"] = "Das ist ein ganz normaler diktierter Satz. " * 100
    out = _call(rewrite=False, data=b"x" * (1200 * 1024))
    assert out["text"] == transcribes["text"]


def test_a_service_error_on_a_real_recording_keeps_the_real_error(
    monkeypatch, configured
):
    """An outage during a long memo is worth knowing about."""
    async def boom(data, *, filename, content_type, diarize=False, language=None):
        raise transcription.TranscriptionError("The transcription service timed out.")

    monkeypatch.setattr(transcription, "transcribe", boom)
    with pytest.raises(R.MemoInputError) as exc:
        _call(data=b"x" * (200 * 1024))
    assert exc.value.status == 502
    assert "timed out" in exc.value.message


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


# ── Rewrite as its own phase ──────────────────────────────────────────────────
# The recorder transcribes first, flips the status line, then asks for the
# rewrite. That only works if the second phase stands on its own — with the same
# auth check, and without the audio it no longer has.


def test_rewrite_phase_rejects_anonymous_caller():
    with pytest.raises(R.MemoInputError) as exc:
        _run(R.rewrite_payload("a transcript", username="", token="tok"))
    assert exc.value.status == 401


def test_rewrite_phase_rejects_empty_transcript():
    with pytest.raises(R.MemoInputError) as exc:
        _run(R.rewrite_payload("   ", username="alice", token="tok"))
    assert exc.value.status == 400


def test_rewrite_phase_returns_topic_text_and_transcript(monkeypatch):
    monkeypatch.setattr(R, "_memo_model", lambda u, t: "some-model")

    async def fake_rewrite(text, *, model, user_id, token, conversation=False):
        return "Car insurance", "- cleaned up"

    monkeypatch.setattr(R.memo_service, "rewrite_dictation", fake_rewrite)

    out = _run(R.rewrite_payload("raw words", username="alice", token="tok"))
    assert out == {
        "topic": "Car insurance",
        "text": "- cleaned up",
        "transcript": "raw words",
    }
