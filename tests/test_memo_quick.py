"""Fire-and-forget memos: the background half of the memo path.

The dialog closes the moment the recording stops, so nobody is watching when
this runs. Two things therefore have to hold: a failure must still reach the
user, and the recording must never be written to disk — not on any path.
"""

import asyncio
import sys
import types

import pytest

from app_ui import memo_routes as R


def _run(coro):
    return asyncio.run(coro)


def _process(**kw):
    params = {
        "filename": "memo.webm",
        "content_type": "audio/webm",
        "username": "alice",
        "token": "tok",
        "conversation": False,
        "language": "de",
    }
    params.update(kw)
    data = params.pop("data", b"audio-bytes")
    return _run(R._process_quick_memo(data, **params))


@pytest.fixture(autouse=True)
def clean_notices():
    R._notices.clear()
    yield
    R._notices.clear()


@pytest.fixture
def writer(monkeypatch):
    """Stub `services.clients.vault_memo_writer` — importing the real module
    would spin up Chroma and the embedding model."""
    calls: list[tuple] = []

    class FakeWriter:
        async def create_memo(self, text, username, topic=""):
            calls.append((text, username, topic))
            return "id-1", "Voice memos/2026-08-12 1030 Brakes.md"

    module = types.ModuleType("services.clients")
    module.vault_memo_writer = FakeWriter()
    monkeypatch.setitem(sys.modules, "services.clients", module)
    return calls


@pytest.fixture
def transcribes(monkeypatch):
    async def fake(data, **kw):
        return {"topic": "Brakes", "text": "Change the brake pads.", "transcript": "raw"}

    monkeypatch.setattr(R, "transcribe_payload", fake)


# ── Notice queue ─────────────────────────────────────────────────────────────


def test_pop_notices_drains_the_queue():
    R._push_notice("alice", "ok", "note.md")
    assert R.pop_notices("alice") == [{"kind": "ok", "message": "note.md"}]
    assert R.pop_notices("alice") == []


def test_notices_are_per_user():
    R._push_notice("alice", "ok", "a")
    R._push_notice("bob", "ok", "b")
    assert R.pop_notices("bob") == [{"kind": "ok", "message": "b"}]
    assert len(R.pop_notices("alice")) == 1


# ── Happy path ───────────────────────────────────────────────────────────────


def test_success_files_the_memo_and_reports_the_filename(writer, transcribes):
    _process()
    assert writer == [("Change the brake pads.", "alice", "Brakes")]
    assert R.pop_notices("alice") == [
        {"kind": "ok", "message": "2026-08-12 1030 Brakes.md"}
    ]


# ── Failures ─────────────────────────────────────────────────────────────────


def _no_audio_written(root) -> bool:
    """No recording anywhere under the app path, whatever the failure was."""
    return not list(root.rglob("*.webm"))


def test_silence_warns_and_files_nothing(monkeypatch, writer):
    """The guard doing its job — there is nothing in that recording to save."""
    async def fake(data, **kw):
        raise R.MemoInputError(422, R.NOTHING_RECOGNISED)

    monkeypatch.setattr(R, "transcribe_payload", fake)

    _process()
    notices = R.pop_notices("alice")
    assert notices[0]["kind"] == "warning"
    assert notices[0]["message"] == R.NOTHING_RECOGNISED
    assert not writer


def test_transcription_failure_never_writes_the_audio(monkeypatch, writer, tmp_path):
    async def fake(data, **kw):
        raise RuntimeError("whisper is down")

    monkeypatch.setattr(R, "transcribe_payload", fake)
    monkeypatch.setattr(R.settings, "app_path", tmp_path)

    _process(data=b"the-recording")
    notice = R.pop_notices("alice")[0]
    assert notice["kind"] == "error"
    # The failure is reported; the recording is not kept and no path leaks into
    # the message the user reads.
    assert _no_audio_written(tmp_path)
    assert "the-recording" not in notice["message"]
    assert "memo_failed" not in notice["message"]


def test_save_failure_never_writes_the_audio(monkeypatch, transcribes, tmp_path):
    class BrokenWriter:
        async def create_memo(self, *a, **kw):
            raise OSError("vault is read-only")

    module = types.ModuleType("services.clients")
    module.vault_memo_writer = BrokenWriter()
    monkeypatch.setitem(sys.modules, "services.clients", module)
    monkeypatch.setattr(R.settings, "app_path", tmp_path)

    _process(data=b"the-recording")
    notice = R.pop_notices("alice")[0]
    assert notice["kind"] == "error"
    assert _no_audio_written(tmp_path)
    assert "memo_failed" not in notice["message"]


# ── Serialization ────────────────────────────────────────────────────────────


def test_quick_memos_run_one_at_a_time(monkeypatch, writer):
    """Local inference is one GPU — parallel memos would queue up inside it."""
    overlap = {"now": 0, "max": 0}

    async def fake(data, **kw):
        overlap["now"] += 1
        overlap["max"] = max(overlap["max"], overlap["now"])
        await asyncio.sleep(0.01)
        overlap["now"] -= 1
        return {"topic": "T", "text": "text", "transcript": "raw"}

    monkeypatch.setattr(R, "transcribe_payload", fake)

    async def run_three():
        # A fresh semaphore per event loop: the module-level one binds to the
        # loop that first awaits it, and each asyncio.run() makes a new one.
        monkeypatch.setattr(R, "_quick_lane", asyncio.Semaphore(1))
        await asyncio.gather(*[
            R._process_quick_memo(
                b"a", filename="m.webm", content_type="audio/webm",
                username="alice", token="t", conversation=False, language="de",
            )
            for _ in range(3)
        ])

    _run(run_three())
    assert overlap["max"] == 1
    assert len(R.pop_notices("alice")) == 3
