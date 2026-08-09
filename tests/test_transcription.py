"""Transcription client: request shape and error mapping.

Every failure has to reach the user as a sentence they can act on. A generic
"something went wrong" for a wrong API key or a service that is simply not
running turns a two-minute fix into a support question.
"""

import asyncio

import httpx
import pytest

from config.settings import settings
from services import transcription as T


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "whisper_url", "http://whisper.invalid/v1", raising=False)
    monkeypatch.setattr(settings, "whisper_api_key", "", raising=False)
    monkeypatch.setattr(settings, "whisper_model", "whisper-1", raising=False)
    monkeypatch.setattr(settings, "whisper_language", "", raising=False)


def _mock_transport(monkeypatch, handler):
    """Route every request through `handler` instead of the network."""
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(T.httpx, "AsyncClient", factory)


# ── Configuration gate ────────────────────────────────────────────────────────


def test_unconfigured_is_reported_not_attempted(monkeypatch):
    monkeypatch.setattr(settings, "whisper_url", "", raising=False)
    assert not T.is_configured()
    with pytest.raises(T.TranscriptionError):
        _run(T.transcribe(b"x", "a.webm", "audio/webm"))


# ── Request shape ─────────────────────────────────────────────────────────────


def test_posts_multipart_to_the_transcriptions_endpoint(monkeypatch, configured):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "Klempner kommt Dienstag."})

    _mock_transport(monkeypatch, handler)
    assert _run(T.transcribe(b"audio-bytes", "memo.webm", "audio/webm")) == "Klempner kommt Dienstag."
    assert seen["url"] == "http://whisper.invalid/v1/audio/transcriptions"
    assert b"audio-bytes" in seen["body"]
    assert b"whisper-1" in seen["body"]
    # No key configured → no header at all, rather than an empty Bearer
    assert seen["auth"] is None


def test_api_key_and_language_are_sent_when_set(monkeypatch, configured):
    monkeypatch.setattr(settings, "whisper_api_key", "sk-abc", raising=False)
    monkeypatch.setattr(settings, "whisper_language", "de", raising=False)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "ok ok ok"})

    _mock_transport(monkeypatch, handler)
    _run(T.transcribe(b"x", "memo.webm", "audio/webm"))
    assert seen["auth"] == "Bearer sk-abc"
    assert b"de" in seen["body"]


def test_a_trailing_slash_on_the_url_does_not_double_up(monkeypatch, configured):
    monkeypatch.setattr(settings, "whisper_url", "http://whisper.invalid/v1/", raising=False)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"text": "ok ok ok"})

    _mock_transport(monkeypatch, handler)
    _run(T.transcribe(b"x", "memo.webm", "audio/webm"))
    assert seen["url"] == "http://whisper.invalid/v1/audio/transcriptions"


# ── Error mapping ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status,fragment", [
    (401, "API key"),
    (403, "API key"),
    (413, "too large"),
    (500, "error"),
])
def test_http_errors_become_actionable_messages(monkeypatch, configured, status, fragment):
    _mock_transport(monkeypatch, lambda r: httpx.Response(status, text="nope"))
    with pytest.raises(T.TranscriptionError) as exc:
        _run(T.transcribe(b"x", "memo.webm", "audio/webm"))
    assert fragment in str(exc.value)


def test_an_unreachable_service_says_so(monkeypatch, configured):
    def handler(request):
        raise httpx.ConnectError("refused")

    _mock_transport(monkeypatch, handler)
    with pytest.raises(T.TranscriptionError) as exc:
        _run(T.transcribe(b"x", "memo.webm", "audio/webm"))
    assert "unreachable" in str(exc.value)


def test_a_timeout_says_so(monkeypatch, configured):
    def handler(request):
        raise httpx.ReadTimeout("slow")

    _mock_transport(monkeypatch, handler)
    with pytest.raises(T.TranscriptionError) as exc:
        _run(T.transcribe(b"x", "memo.webm", "audio/webm"))
    assert "timed out" in str(exc.value)


def test_a_non_json_response_does_not_raise_a_raw_parse_error(monkeypatch, configured):
    _mock_transport(monkeypatch, lambda r: httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(T.TranscriptionError) as exc:
        _run(T.transcribe(b"x", "memo.webm", "audio/webm"))
    assert "unreadable" in str(exc.value)


# ── Diarization: speaker turns ────────────────────────────────────────────────


def _seg(start, end, text, speaker=None):
    seg = {"start": start, "end": end, "text": text}
    if speaker is not None:
        seg["speaker"] = speaker
    return seg


def _diarized(monkeypatch, segments, text="flat fallback"):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(200, json={"text": text, "segments": segments})

    _mock_transport(monkeypatch, handler)
    out = _run(T.transcribe(b"a", "c.m4a", "audio/mp4", diarize=True))
    return out, seen


def test_diarize_requests_verbose_json(monkeypatch, configured):
    # Speaker labels live on the segments; the flat `text` field never has them,
    # so asking for the default json format would silently lose diarization.
    _out, seen = _diarized(monkeypatch, [_seg(0, 1, "Hallo.", "SPEAKER_00")])
    assert b"verbose_json" in seen["body"]


def test_plain_mode_does_not_request_verbose_json(monkeypatch, configured):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        return httpx.Response(200, json={"text": "hi"})

    _mock_transport(monkeypatch, handler)
    assert _run(T.transcribe(b"a", "m.webm", "audio/webm")) == "hi"
    assert b"verbose_json" not in seen["body"]


def test_consecutive_segments_of_one_speaker_become_one_turn(monkeypatch, configured):
    out, _ = _diarized(monkeypatch, [
        _seg(0, 2, "Das Angebot liegt bei 14.200.", "SPEAKER_00"),
        _seg(2, 4, "Inklusive Arbeitsplatte.", "SPEAKER_00"),
        _seg(4, 6, "Und der Termin?", "SPEAKER_01"),
        _seg(6, 8, "Ende März.", "SPEAKER_00"),
    ])
    assert out == (
        "**Speaker 1:** Das Angebot liegt bei 14.200. Inklusive Arbeitsplatte.\n\n"
        "**Speaker 2:** Und der Termin?\n\n"
        "**Speaker 1:** Ende März."
    )


def test_speakers_are_numbered_by_first_appearance(monkeypatch, configured):
    # The diarizer numbers by clustering order, so the first voice can arrive as
    # SPEAKER_02. A transcript that opens with "Speaker 3" reads like a bug.
    out, _ = _diarized(monkeypatch, [
        _seg(0, 1, "Erste Stimme.", "SPEAKER_02"),
        _seg(1, 2, "Zweite Stimme.", "SPEAKER_00"),
    ])
    assert out == "**Speaker 1:** Erste Stimme.\n\n**Speaker 2:** Zweite Stimme."


def test_single_speaker_gets_no_labels(monkeypatch, configured):
    # Labelling every line "Speaker 1" is noise, not information.
    out, _ = _diarized(monkeypatch, [
        _seg(0, 2, "Nur ich rede hier.", "SPEAKER_00"),
        _seg(2, 4, "Immer noch ich.", "SPEAKER_00"),
    ])
    assert out == "Nur ich rede hier. Immer noch ich."


def test_segments_without_speaker_keys_still_produce_text(monkeypatch, configured):
    # A service that honours verbose_json but has no diarizer.
    out, _ = _diarized(monkeypatch, [_seg(0, 2, "Kein Diarizer hier.")])
    assert out == "Kein Diarizer hier."


def test_missing_segments_fall_back_to_the_flat_text(monkeypatch, configured):
    # The service ignored verbose_json. Losing the labels beats losing the memo.
    out, _ = _diarized(monkeypatch, [], text="Der ganze Text.")
    assert out == "Der ganze Text."


def test_blank_segments_are_dropped(monkeypatch, configured):
    out, _ = _diarized(monkeypatch, [
        _seg(0, 1, "   ", "SPEAKER_00"),
        _seg(1, 2, "Echter Inhalt.", "SPEAKER_01"),
        _seg(2, 3, "", "SPEAKER_01"),
    ])
    # Only one speaker actually said anything → no labels.
    assert out == "Echter Inhalt."
