"""Turn a spoken instruction into a revised note.

The sibling of `memo_service.rewrite_dictation`, and deliberately a separate
prompt: a memo rewrite is told "this is the content, tidy it", while here the
dictation is a mix of two different things and the model's first job is to tell
them apart — "delete the second bullet" is an instruction about the note,
"remember to call the insurance" is content to put into it.

Two rules carry the feature:

- **The model returns the whole note, and everything it was not asked to change
  comes back verbatim.** A patch format would be smaller, but it would also mean
  the model deciding where a hunk applies, and a misplaced hunk in the user's
  own notes is not recoverable from the UI.
- **A failure never writes.** `memo_service` can fall back to the raw transcript
  because a memo is new text either way; here the raw transcript is an
  *instruction*, and stapling "delete the second bullet" into the note would be
  worse than doing nothing. So this raises and the note is left alone.
"""

import logging

_log = logging.getLogger(__name__)

EDIT_SYSTEM = """\
You revise a personal note from a spoken instruction. You are not an assistant \
and you never answer, advise on, or comment on the content of the note — you \
only produce the revised note.

You are given the note as it stands, and a dictation. The dictation mixes two \
kinds of utterance and your first job is to tell them apart:

* **Instructions about the note** — "delete the second bullet", "change the \
date to Friday", "put that under the shopping heading", "rewrite this as a \
table", "the amount is wrong, it is 240 euros", "scratch the last sentence". \
Carry them out.
* **New content** — anything that is not talking about the note is material to \
add to it. Fold it into the structure that is already there: continue the \
existing list or table instead of starting a second one, and put a new fact \
next to the ones it belongs with.

Rules, in order of importance:

1. Return the COMPLETE note, not a fragment and not a patch. Every part you \
were not asked to change comes back exactly as it was — same wording, same \
markdown, same order. Do not tidy, restructure, reformat or "improve" \
untouched text.
2. Never invent. No fact, figure, date, name or amount appears in the result \
unless it was in the note or in the dictation.
3. When it is unclear whether something is an instruction or content, treat it \
as content and add it. Adding a stray sentence is visible and one keystroke to \
undo; deleting the user's own words on a guess is not.
4. Never write a `---` frontmatter block. You are given the note's body only, \
and the properties are edited elsewhere.
5. Dictated punctuation becomes punctuation: a spoken "Komma", "Punkt", \
"Doppelpunkt", "Fragezeichen", "neue Zeile", "neuer Absatz" (or comma, period, \
colon, question mark, new line, new paragraph …) is written as the mark and the \
word is dropped — unless it is plainly meant as content.
6. Write in the language the note is in. If the note is empty, write in the \
language of the dictation.

`text` is the note AFTER your change — the finished thing, ready to replace \
what the user is looking at. It is not a description of the change and not the \
note as it was. Copying the note back unchanged while reporting a change in \
`summary` is the one failure that cannot be detected by reading your answer, so \
check the two against each other before you reply: if `summary` says something \
was deleted, that text must be gone from `text`.

Then say in one short sentence what you changed, in the language of the \
dictation. Name the change ("second bullet deleted, two items added"), not the \
note. If the instruction asked for nothing you can carry out, leave the note \
exactly as it was and say so — that is a valid answer and better than a \
guess."""


# Sent verbatim as a second user turn when the model hands back the note it was
# given. Naming the mistake beats re-sending the same prompt and hoping.
_ECHO_NUDGE = """\
You returned the note unchanged, but your summary claims you changed it. One of \
the two is wrong.

Carry the instruction out on the note this time and put the RESULT in `text` — \
the note as it should look afterwards, complete. If the instruction genuinely \
cannot be carried out on this note, return the note unchanged and say exactly \
that in `summary`."""


EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {
            "type": "string",
            "description": "The complete revised note in Markdown, body only.",
        },
        "summary": {
            "type": "string",
            "description": "One short sentence naming what changed.",
        },
    },
    "required": ["text", "summary"],
}


class NoteEditError(Exception):
    """The revision did not come back. The note is untouched."""


async def apply_voice_edit(
    note: str,
    instruction: str,
    *,
    model: str,
    user_id: str,
    token: str,
) -> tuple[str, str, bool]:
    """Return (revised_note, summary, changed). Raises `NoteEditError` on failure.

    `changed` is computed here by comparing the answer to what was sent, never
    taken from the model. A model that hands the note straight back while
    reporting an edit in `summary` produces the one failure the user cannot see:
    a confident confirmation over an untouched draft. It is retried once, and if
    it happens again the caller is told the truth instead of the summary.

    Unlike the memo path there is no fallback: every degraded outcome here
    would mean writing something into the user's own note that they did not
    say, so the honest answer is to change nothing and say so.
    """
    spoken = (instruction or "").strip()
    if not spoken:
        raise NoteEditError("There was nothing to act on.")
    if not model:
        raise NoteEditError("No model is configured for dictation.")

    from werkbank.llm_lane import complete_structured

    body = (note or "").strip()
    user_message = (
        f"NOTE AS IT STANDS:\n{body or '(the note is empty)'}\n\nDICTATION:\n{spoken}"
    )

    messages: list[dict] = [{"role": "user", "content": user_message}]

    async def _ask() -> tuple[str, str]:
        try:
            result = await complete_structured(
                EDIT_SYSTEM,
                messages,
                model=model,
                user_id=user_id,
                token=token,
                json_schema=EDIT_SCHEMA,
                tool_name="revise_note",
                max_tokens=8000,
                temperature=0.1,
            )
        except Exception as exc:
            _log.warning("voice note edit failed: %s", exc)
            raise NoteEditError("The note could not be revised.") from exc
        text = (result.get("text") or "").strip()
        # A structurally valid response with an empty body is still a failure,
        # and the one that must never be applied: it would wipe the note.
        if not text and body:
            raise NoteEditError("The note could not be revised.")
        return text, (result.get("summary") or "").strip()

    text, summary = await _ask()
    if not body or text != body:
        return text, summary, text != body

    # The note came back exactly as it was sent. Say so once, in a new turn —
    # re-sending the identical prompt would only re-roll the same dice.
    _log.warning(
        "voice edit returned the note unchanged (summary=%r); retrying once", summary[:120]
    )
    messages = [
        {"role": "user", "content": user_message},
        {"role": "assistant", "content": f"(returned the note unchanged; {summary})"},
        {"role": "user", "content": _ECHO_NUDGE},
    ]
    text, summary = await _ask()
    changed = text != body
    if not changed:
        _log.warning("voice edit still unchanged after the retry (summary=%r)", summary[:120])
    return text, summary, changed
