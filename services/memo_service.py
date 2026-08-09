"""Turn a raw dictation into a filed memo.

Two steps, both deliberately dumb: clean up the transcript, then propose a
topic for the filename. No tool calls, no agentic loop, no decisions — the
user already made the decision by pressing the memo button. See
`docs/voice-memos-tasks.md` for why memo capture is not a chat tool.
"""

import logging
import re

_log = logging.getLogger(__name__)

# Whisper does not return an empty string for non-speech input — it invents a
# plausible phrase. These are the stock hallucinations it produces on silence,
# tones and room noise; a memo built from one of them would look completely
# genuine. Compared case-insensitively against the whole transcript.
_HALLUCINATIONS = {
    "vielen dank.",
    "vielen dank!",
    "vielen dank für's zuschauen!",
    "vielen dank für's zuschauen.",
    "vielen dank fürs zuschauen!",
    "vielen dank fürs zuschauen.",
    "untertitel von stephanie geiges",
    "untertitelung des zdf, 2020",
    "untertitel im auftrag des zdf, 2021",
    "thank you.",
    "thanks for watching!",
    "thank you for watching.",
    "you",
    "bye.",
    ".",
}

# Below this, a "transcript" is noise rather than a memo.
MIN_TRANSCRIPT_CHARS = 12


REWRITE_SYSTEM = """\
You clean up dictated personal memos. You are not an assistant and you never \
answer, advise on, or comment on the content — you only reformat it.

Rules, in order of importance:

1. Keep every piece of content. Facts, figures, dates, amounts, reference \
numbers, names — all of it survives verbatim in meaning. Never drop, merge or \
summarise away a detail.
2. Do not pad. Add no introduction, no closing, no commentary, no invented \
context. The result must not be longer than the content justifies.
3. Structure it well. If the dictation enumerates things, use a bullet list. If \
it describes a sequence of steps, use a numbered list. If it repeats the same \
kind of item with values (positions with numbers, entries with amounts, \
dates with events), use a Markdown table. If it is simply prose, leave it as \
prose in short paragraphs. Use headings only if the memo genuinely covers \
several distinct subjects.
4. Improve the wording. Spoken language is clumsy — fix broken grammar, remove \
filler ("äh", "also", "sozusagen"), fix obvious speech-recognition slips \
(wrong casing of terms, a spelled-out "Nummer" that means "Nr."). But say the \
same thing: no new claims, no interpretation, no conclusions of your own.
5. Write in the language the dictation is in.

Then give the memo a topic: 3-5 words naming the subject, no date, no verb. It \
becomes part of a filename, so keep it plain."""


CONVERSATION_SYSTEM = """\
You clean up transcripts of recorded conversations. You are not a participant \
and you never answer, advise on, or comment on what was said — you only \
reformat it.

Rules, in order of importance:

1. Keep the turn structure. Every turn stays attributed to the speaker it came \
from. Never merge two speakers into one turn, never invent a turn, never \
reorder them. Keep the `**Speaker N:**` labels exactly as they appear — do not \
rename, renumber, or guess at real names.
2. Keep every piece of content. Facts, figures, dates, amounts, reference \
numbers, names — all of it survives verbatim in meaning within the turn it was \
said in.
3. Fix only the transcription. Remove filler ("äh", "also"), repair broken \
grammar and obvious speech-recognition slips, drop false starts where the \
speaker immediately corrects themselves. Say the same thing: no new claims, no \
interpretation, no summary of your own.
4. Merge consecutive turns by the same speaker into one turn. Speaker labels \
that alternate every sentence are a transcription artefact, not the \
conversation.
5. If the recording is clearly one person talking (a single speaker \
throughout), drop the labels and treat it as prose.
6. Write in the language the conversation is in.

Then give the conversation a topic: 3-5 words naming the subject, no date, no \
verb. It becomes part of a filename, so keep it plain."""


def looks_like_silence(transcript: str) -> bool:
    """True when a transcript is Whisper's output for "nothing was said".

    Checking for an empty string is not enough: an accidental button press
    produces a confident-looking sentence, and without this guard it would be
    rewritten and filed as a real memo.
    """
    cleaned = transcript.strip()
    if len(cleaned) < MIN_TRANSCRIPT_CHARS:
        return True
    return cleaned.lower() in _HALLUCINATIONS


def _fallback_topic(text: str, max_words: int = 5) -> str:
    """Topic from the opening words — used when the rewrite call fails."""
    words = re.sub(r"[^\w\s-]", " ", text, flags=re.UNICODE).split()
    return " ".join(words[:max_words]) or "Memo"


REWRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {
            "type": "string",
            "description": "3-5 words naming the subject. No date, no verb.",
        },
        "text": {
            "type": "string",
            "description": "The cleaned-up memo in Markdown.",
        },
    },
    "required": ["topic", "text"],
}


async def rewrite_dictation(
    transcript: str,
    *,
    model: str,
    user_id: str,
    token: str,
    conversation: bool = False,
) -> tuple[str, str]:
    """Return (topic, text). Never raises.

    A failed rewrite must not cost the user their words, so every error path
    falls back to the raw transcript with a topic taken from its first words.

    ``conversation`` swaps in a prompt that preserves speaker turns. The memo
    prompt actively works against a dialog — it restructures into bullets and
    tables, which destroys who-said-what.
    """
    raw = transcript.strip()
    if not model:
        return _fallback_topic(raw), raw

    from werkbank.llm_lane import complete_structured

    try:
        result = await complete_structured(
            CONVERSATION_SYSTEM if conversation else REWRITE_SYSTEM,
            [{"role": "user", "content": raw}],
            model=model,
            user_id=user_id,
            token=token,
            json_schema=REWRITE_SCHEMA,
            tool_name="memo",
            max_tokens=4000,
            temperature=0.2,
        )
    except Exception as exc:
        _log.warning("memo rewrite failed, keeping raw transcript: %s", exc)
        return _fallback_topic(raw), raw

    text = (result.get("text") or "").strip()
    topic = (result.get("topic") or "").strip()
    if not text:
        # A structurally valid response with an empty body is still a failure.
        return topic or _fallback_topic(raw), raw
    return topic or _fallback_topic(text), text
