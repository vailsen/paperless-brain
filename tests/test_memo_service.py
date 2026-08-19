"""Dictation rewrite and the silence guard.

The guard is the load-bearing part. Whisper answers non-speech with a confident
stock phrase rather than an empty string, so without it an accidental button
press files a memo that looks entirely genuine.
"""

import asyncio

import pytest

from services import memo_service as M


def _run(coro):
    return asyncio.run(coro)


# ── Silence guard ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "",
    "   \n  ",
    "Vielen Dank.",
    "vielen dank!",
    "Vielen Dank fürs Zuschauen.",
    "Thank you.",
    "you",
    ".",
    "kurz",           # under the minimum length
])
def test_non_speech_output_is_recognised_as_silence(text):
    assert M.looks_like_silence(text)


@pytest.mark.parametrize("text", [
    "Klempner kommt am Dienstag um zehn.",
    "Steuerbescheid 2024 ist noch nicht da.",
    "Vielen Dank an Herrn Müller für die Unterlagen.",   # contains the phrase, is not it
])
def test_real_memos_pass_the_guard(text):
    assert not M.looks_like_silence(text)


# ── Fallback behaviour ────────────────────────────────────────────────────────


def test_no_model_returns_the_raw_transcript(monkeypatch):
    topic, text = _run(M.rewrite_dictation(
        "Klempner kommt Dienstag.", model="", user_id="alice", token="t"
    ))
    assert text == "Klempner kommt Dienstag."
    assert topic


def test_a_failed_rewrite_never_loses_the_users_words(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("model unreachable")

    monkeypatch.setattr("werkbank.llm_lane.complete_structured", boom)
    topic, text = _run(M.rewrite_dictation(
        "Gebäudeversicherung Nummer 118 prüfen.",
        model="qwen", user_id="alice", token="t",
    ))
    assert text == "Gebäudeversicherung Nummer 118 prüfen."
    assert topic == "Gebäudeversicherung Nummer 118 prüfen"


def test_an_empty_rewrite_body_falls_back_to_the_transcript(monkeypatch):
    async def empty(*a, **k):
        return {"topic": "Versicherung", "text": "   "}

    monkeypatch.setattr("werkbank.llm_lane.complete_structured", empty)
    topic, text = _run(M.rewrite_dictation(
        "Originaltext bleibt.", model="qwen", user_id="alice", token="t"
    ))
    assert text == "Originaltext bleibt."
    assert topic == "Versicherung"


def test_a_successful_rewrite_is_used(monkeypatch):
    async def ok(system, messages, **k):
        assert messages[0]["content"] == "Klempner kommt Dienstag."
        return {"topic": "Klempner Termin", "text": "- Klempner kommt am Dienstag"}

    monkeypatch.setattr("werkbank.llm_lane.complete_structured", ok)
    topic, text = _run(M.rewrite_dictation(
        "Klempner kommt Dienstag.", model="qwen", user_id="alice", token="t"
    ))
    assert topic == "Klempner Termin"
    assert text == "- Klempner kommt am Dienstag"


def test_a_missing_topic_is_derived_from_the_text(monkeypatch):
    async def no_topic(*a, **k):
        return {"text": "Gebäudereinigung muss neu beauftragt werden."}

    monkeypatch.setattr("werkbank.llm_lane.complete_structured", no_topic)
    topic, _ = _run(M.rewrite_dictation(
        "irgendwas", model="qwen", user_id="alice", token="t"
    ))
    assert topic == "Gebäudereinigung muss neu beauftragt werden"


# ── Prompt contract ───────────────────────────────────────────────────────────


def test_the_prompt_states_the_non_negotiables():
    """These four are the whole contract — a reworded prompt that drops one
    changes what gets filed."""
    p = M.REWRITE_SYSTEM.lower()
    assert "never drop" in p or "keep every piece" in p     # no content loss
    assert "do not pad" in p                                 # no padding
    assert "table" in p and "bullet" in p                    # structure
    assert "no new claims" in p                              # no invention


# ── A second recording continues the memo ────────────────────────────────────
#
# The review dialog lets the user record again. Without the memo so far, the
# model tidies the new fragment alone and the result is stapled to the end: a
# second heading for the same subject, a second table with the same columns,
# facts separated from the ones they belong with.


def _capture_rewrite(monkeypatch, reply=("Topic", "merged memo")):
    """Record what rewrite_dictation hands the model."""
    seen = {}

    async def fake_complete(system, messages, **kw):
        seen["system"] = system
        seen["user"] = messages[0]["content"]
        return {"topic": reply[0], "text": reply[1]}

    import werkbank.llm_lane as lane
    monkeypatch.setattr(lane, "complete_structured", fake_complete)
    return seen


def test_a_first_recording_sends_only_the_transcript(monkeypatch):
    seen = _capture_rewrite(monkeypatch)
    _run(M.rewrite_dictation(
        "Milch kaufen", model="m", user_id="alice", token="t",
    ))
    assert seen["user"] == "Milch kaufen"
    assert "MEMO SO FAR" not in seen["user"]
    assert M.CONTINUATION_RULE not in seen["system"]


def test_a_second_recording_sends_the_memo_so_far(monkeypatch):
    seen = _capture_rewrite(monkeypatch)
    topic, text = _run(M.rewrite_dictation(
        "und Brot", model="m", user_id="alice", token="t",
        previous="# Einkauf\n\n- Milch",
    ))
    assert "MEMO SO FAR:" in seen["user"]
    assert "# Einkauf" in seen["user"]
    assert "NEW DICTATION TO ADD:\nund Brot" in seen["user"]
    # The rule that tells the model to return the whole thing merged.
    assert M.CONTINUATION_RULE in seen["system"]
    assert text == "merged memo"


def test_a_failed_second_rewrite_keeps_the_earlier_half(monkeypatch):
    """Falling back to the new fragment alone would delete reviewed text."""
    async def boom(*a, **kw):
        raise RuntimeError("model down")

    import werkbank.llm_lane as lane
    monkeypatch.setattr(lane, "complete_structured", boom)
    _topic, text = _run(M.rewrite_dictation(
        "und Brot", model="m", user_id="alice", token="t", previous="- Milch",
    ))
    assert "- Milch" in text and "und Brot" in text


def test_no_model_still_keeps_both_halves(monkeypatch):
    _topic, text = _run(M.rewrite_dictation(
        "und Brot", model="", user_id="alice", token="t", previous="- Milch",
    ))
    assert "- Milch" in text and "und Brot" in text


def test_punctuation_dictation_is_covered_by_the_prompts():
    """Spoken "Komma"/"comma" must become a mark, in both prompt variants."""
    for prompt in (M.REWRITE_SYSTEM, M.CONVERSATION_SYSTEM):
        assert "Komma" in prompt and "comma" in prompt
        assert "Doppelpunkt" in prompt
