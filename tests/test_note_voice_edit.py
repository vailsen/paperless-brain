"""Applying a spoken instruction to an open note.

The memo path can fall back to the raw transcript when the model fails, because
a memo is new text either way. Here the transcript is an *instruction* — writing
it into the note would put "delete the second bullet" into the user's own notes,
and an empty answer would wipe them. So every degraded outcome must end in an
error and an untouched note, and that is what these pin.
"""

import asyncio

import pytest

from app_ui import memo_routes as R
from app_ui.note_voice_dialog import _diff_html
from services import note_edit_service as N


def _run(coro):
    return asyncio.run(coro)


def _structured(monkeypatch, result):
    """Stand in for the model. Returns `result`, or raises it if it is an error."""
    seen: dict = {}

    async def fake(system, messages, **kw):
        seen["system"] = system
        seen["messages"] = messages
        seen.update(kw)
        if isinstance(result, Exception):
            raise result
        return result

    import werkbank.llm_lane as lane

    monkeypatch.setattr(lane, "complete_structured", fake)
    return seen


# ── The service ───────────────────────────────────────────────────────────────


def test_the_note_and_the_instruction_both_reach_the_model(monkeypatch):
    seen = _structured(monkeypatch, {"text": "- milk\n- bread", "summary": "added bread"})
    text, summary, changed = _run(N.apply_voice_edit(
        "- milk", "add bread", model="m", user_id="alice", token="t"
    ))
    assert text == "- milk\n- bread"
    assert summary == "added bread"
    assert changed is True
    sent = seen["messages"][0]["content"]
    assert "- milk" in sent
    assert "add bread" in sent


def test_an_empty_dictation_is_refused_before_the_model(monkeypatch):
    seen = _structured(monkeypatch, {"text": "wiped", "summary": ""})
    with pytest.raises(N.NoteEditError):
        _run(N.apply_voice_edit("- milk", "   ", model="m", user_id="a", token="t"))
    assert not seen  # the model was never called


def test_no_model_is_an_error_not_a_silent_pass_through():
    with pytest.raises(N.NoteEditError):
        _run(N.apply_voice_edit("- milk", "add bread", model="", user_id="a", token="t"))


def test_a_model_failure_leaves_the_note_alone(monkeypatch):
    _structured(monkeypatch, RuntimeError("model unreachable"))
    with pytest.raises(N.NoteEditError):
        _run(N.apply_voice_edit("- milk", "add bread", model="m", user_id="a", token="t"))


def test_an_empty_answer_never_wipes_a_note(monkeypatch):
    """A structurally valid response with an empty body is the one failure that
    must not be applied — it would delete everything the note held."""
    _structured(monkeypatch, {"text": "   ", "summary": "cleared"})
    with pytest.raises(N.NoteEditError):
        _run(N.apply_voice_edit("- milk", "tidy up", model="m", user_id="a", token="t"))


def test_an_empty_answer_for_an_empty_note_is_fine(monkeypatch):
    """Nothing to lose, so refusing here would only be noise."""
    _structured(monkeypatch, {"text": "", "summary": "nothing to do"})
    text, _summary, changed = _run(N.apply_voice_edit(
        "", "never mind", model="m", user_id="a", token="t"
    ))
    assert text == ""
    assert changed is False


# ── The route ─────────────────────────────────────────────────────────────────


def test_the_route_requires_a_session():
    with pytest.raises(R.MemoInputError) as exc:
        _run(R.note_edit_payload("- milk", "add bread", username="", token="t"))
    assert exc.value.status == 401


def test_a_failed_revision_is_a_502_not_a_422(monkeypatch):
    """422 means "there was nothing in that recording" throughout this module.
    A model that failed on a perfectly good recording is a different failure and
    must not be reported as the user's mis-tap."""
    monkeypatch.setattr(R, "_memo_model", lambda u, t: "m")
    _structured(monkeypatch, RuntimeError("boom"))
    with pytest.raises(R.MemoInputError) as exc:
        _run(R.note_edit_payload("- milk", "add bread", username="alice", token="t"))
    assert exc.value.status == 502


def test_the_route_uses_the_memo_model(monkeypatch):
    """One dictation model setting, not two: the user picked it for exactly this
    kind of short, strictly-formatted job."""
    monkeypatch.setattr(R, "_memo_model", lambda u, t: "the-memo-model")
    seen = _structured(monkeypatch, {"text": "ok", "summary": "s"})
    _run(R.note_edit_payload("- milk", "add bread", username="alice", token="t"))
    assert seen["model"] == "the-memo-model"


# ── The diff shown before accepting ───────────────────────────────────────────


def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def test_an_unchanged_note_produces_no_diff():
    assert _diff_html("- milk\n- bread", "- milk\n- bread", _esc) == ""


def test_a_removed_line_is_marked_as_removed():
    html = _diff_html("- milk\n- bread", "- milk", _esc)
    assert 'class="d-del"' in html
    assert "bread" in html


def test_diff_content_is_escaped():
    """The diff is rendered as HTML and the note is the user's own text."""
    html = _diff_html("", "<script>alert(1)</script>", _esc)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


# ── The model that says it edited and did not ────────────────────────────────
#
# The failure that prompted this: "lösche den letzten Absatz" came back with a
# confident summary and a byte-identical note. NiceGUI sends no update for an
# unchanged value, so the user saw a confirmation over an untouched draft and
# had no way to tell. `changed` is therefore measured here, never read off the
# model's own summary.


def _echoing(monkeypatch, replies):
    """A model that returns `replies` in order, recording what it was asked."""
    calls: list = []

    async def fake(system, messages, **kw):
        calls.append(messages)
        return replies[min(len(calls) - 1, len(replies) - 1)]

    import werkbank.llm_lane as lane

    monkeypatch.setattr(lane, "complete_structured", fake)
    return calls


def test_an_echoed_note_is_retried_once(monkeypatch):
    calls = _echoing(monkeypatch, [
        {"text": "- milk", "summary": "bread added"},        # unchanged, lying
        {"text": "- milk\n- bread", "summary": "bread added"},
    ])
    text, _summary, changed = _run(N.apply_voice_edit(
        "- milk", "add bread", model="m", user_id="a", token="t"
    ))
    assert changed is True
    assert text == "- milk\n- bread"
    assert len(calls) == 2, "the echo must be retried"
    # The retry says what went wrong instead of re-sending the same prompt.
    assert any("unchanged" in m["content"] for m in calls[1])


def test_a_second_echo_is_reported_as_unchanged_not_as_success(monkeypatch):
    """Two identical answers means the instruction did not land. The caller gets
    the truth, so the UI can say so instead of showing the model's summary."""
    _echoing(monkeypatch, [{"text": "- milk", "summary": "bread added"}])
    text, summary, changed = _run(N.apply_voice_edit(
        "- milk", "add bread", model="m", user_id="a", token="t"
    ))
    assert changed is False
    assert text == "- milk"
    assert summary  # still returned; the caller decides whether to believe it


def test_a_first_time_success_costs_only_one_call(monkeypatch):
    calls = _echoing(monkeypatch, [{"text": "- milk\n- bread", "summary": "ok"}])
    _run(N.apply_voice_edit("- milk", "add bread", model="m", user_id="a", token="t"))
    assert len(calls) == 1


def test_whitespace_only_differences_do_not_count_as_a_change(monkeypatch):
    """The comparison is against the stripped note the model was given, so a
    trailing newline is not an edit."""
    _echoing(monkeypatch, [{"text": "  - milk  ", "summary": "tidied"}])
    _text, _summary, changed = _run(N.apply_voice_edit(
        "- milk", "add bread", model="m", user_id="a", token="t"
    ))
    assert changed is False


def test_the_route_reports_changed(monkeypatch):
    monkeypatch.setattr(R, "_memo_model", lambda u, t: "m")
    _echoing(monkeypatch, [{"text": "- milk", "summary": "bread added"}])
    payload = _run(R.note_edit_payload("- milk", "add bread", username="a", token="t"))
    assert payload["changed"] is False
    payload = None
    _echoing(monkeypatch, [{"text": "- milk\n- bread", "summary": "ok"}])
    payload = _run(R.note_edit_payload("- milk", "add bread", username="a", token="t"))
    assert payload["changed"] is True
