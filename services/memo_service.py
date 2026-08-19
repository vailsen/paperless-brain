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

# Byte thresholds on the recording itself, checked before the upload.
#
# MediaRecorder writes webm/opus at roughly 4-6 KB per second plus about a
# kilobyte of container header, so a real one-word memo still lands well above
# MIN_AUDIO_BYTES. What falls below it is a mis-tap that stopped the recorder
# almost as soon as it started — there is no speech in there to find, and some
# transcription services answer such a stub with a 500 rather than an empty
# transcript, which would otherwise surface as a scary service error.
MIN_AUDIO_BYTES = 2048

# A few seconds of audio. A transcription failure on this little material is
# reported to the user as "nothing was recognised" rather than as a service
# outage: the two are indistinguishable from where they sit, and the useful
# advice ("say it again, properly this time") is the same. Longer recordings
# keep the real error, because there an outage is worth knowing about.
SHORT_AUDIO_BYTES = 24 * 1024


def is_too_short_for_speech(data: bytes) -> bool:
    """True when a recording is too small to contain any speech at all."""
    return len(data) < MIN_AUDIO_BYTES


# Whisper's other failure mode on silence: instead of a short stock phrase it
# regurgitates a slab of memorised training data — broadcast subtitle rules,
# licence boilerplate, a podcast outro — hundreds of words long, fluent, and
# completely invented. `_HALLUCINATIONS` cannot catch those: they are long,
# they are never the same twice, and they read like a genuine document.
#
# What gives them away is arithmetic. Speech has a maximum rate, so a recording
# only holds so many characters no matter what comes back. Two deliberately
# slack constants keep this from ever rejecting a real memo:
#
# * The bitrate assumed for the audio is far below what any browser actually
#   writes (browsers use 24-64 kbps; this assumes 12), so the duration estimate
#   errs long, which errs towards accepting.
# * The rate ceiling is above even fast speech (~20 chars/s), so the character
#   budget errs high for that duration too.
#
# A real 30-second memo comes out around four times under the budget. The
# hallucination that prompted this guard was 1200 characters from under two
# seconds of audio — about ten times over it.
ASSUMED_BYTES_PER_SECOND = 1500
MAX_CHARS_PER_SECOND = 25


def looks_like_hallucination(transcript: str, audio_bytes: int) -> bool:
    """True when the transcript is far longer than the audio could have held.

    Only catches the impossible, never the merely talkative — see the budget
    above. Long recordings get a correspondingly large budget, so this is a
    guard on mis-taps and dead air, not a general-purpose plausibility check.
    """
    if audio_bytes <= 0:
        return False
    budget = (audio_bytes / ASSUMED_BYTES_PER_SECOND) * MAX_CHARS_PER_SECOND
    return len(transcript.strip()) > budget


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
5. Dictated punctuation becomes punctuation. When the transcript spells out a \
mark — "Komma", "Punkt", "Doppelpunkt", "Semikolon", "Fragezeichen", \
"Ausrufezeichen", "Bindestrich", "Gedankenstrich", "Klammer auf/zu", \
"Anführungszeichen", "neue Zeile", "neuer Absatz", "Aufzählungszeichen", and \
the English equivalents (comma, period/full stop, colon, semicolon, question \
mark, exclamation mark, dash, open/close bracket, quote, new line, new \
paragraph, bullet point) — write the mark itself instead of the word, and drop \
the word. Only when it is meant as punctuation: "ein Punkt auf der Liste" and \
"the comma is wrong here" are content and stay as they are.
6. Write in the language the dictation is in.

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
6. Dictated punctuation becomes punctuation: a spoken "Komma", "Punkt", \
"Doppelpunkt", "Fragezeichen", "neue Zeile" (or comma, period, colon, question \
mark, new line …) is written as the mark and the word is dropped — unless it is \
plainly meant as content.
7. Write in the language the conversation is in.

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


CONTINUATION_RULE = """\

This memo already exists and the user has just dictated more. The memo so far \
is given first, the new dictation second. Return the COMPLETE memo — the \
earlier part plus the new material, merged into one coherent whole. Fold the \
new content into the structure that is already there: continue the existing \
list or table instead of starting a second one, and put related facts next to \
the ones they belong with. The earlier part is already tidied and may contain \
the user's own edits — keep its wording and never drop anything from it."""


async def rewrite_dictation(
    transcript: str,
    *,
    model: str,
    user_id: str,
    token: str,
    conversation: bool = False,
    previous: str = "",
) -> tuple[str, str]:
    """Return (topic, text). Never raises.

    A failed rewrite must not cost the user their words, so every error path
    falls back to the raw transcript with a topic taken from its first words.

    ``conversation`` swaps in a prompt that preserves speaker turns. The memo
    prompt actively works against a dialog — it restructures into bullets and
    tables, which destroys who-said-what.

    ``previous`` is the memo as it already stands when the user records a
    second time. Without it the model tidies the new fragment in isolation and
    the result is stapled onto the end: a second "Einkauf" heading, a second
    table with the same columns, facts separated from the ones they belong to.
    With it the model returns the whole memo, so the caller REPLACES rather
    than appends.
    """
    raw = transcript.strip()
    if not model:
        return _fallback_topic(raw), (f"{previous}\n\n{raw}".strip() if previous else raw)

    from werkbank.llm_lane import complete_structured

    previous = (previous or "").strip()
    system = CONVERSATION_SYSTEM if conversation else REWRITE_SYSTEM
    if previous:
        system += CONTINUATION_RULE
        user_message = (
            f"MEMO SO FAR:\n{previous}\n\nNEW DICTATION TO ADD:\n{raw}"
        )
    else:
        user_message = raw

    # Every failure path falls back to the memo so far PLUS the raw words:
    # dropping the earlier half would delete text the user has already reviewed,
    # which is worse than an untidy memo.
    fallback = f"{previous}\n\n{raw}".strip() if previous else raw

    try:
        result = await complete_structured(
            system,
            [{"role": "user", "content": user_message}],
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
        return _fallback_topic(raw), fallback

    text = (result.get("text") or "").strip()
    topic = (result.get("topic") or "").strip()
    if not text:
        # A structurally valid response with an empty body is still a failure.
        return topic or _fallback_topic(raw), fallback
    return topic or _fallback_topic(text), text
