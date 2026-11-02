"""Save a chat answer to a dossier (docs/findings-mission-control.md §5).

Two halves, kept apart on purpose:

- **Provenance** (`provenance`) is computed by code from the turn's tool log:
  the documents the answer cites *and* some tool of that turn returned, the
  web pages a tool returned or read. A URL or document id the model wrote but
  no tool returned is not a source. Stored per answer (`chat_turn_meta`) so a
  reloaded conversation can still be saved with its real sources.
- **The merge round** (`merge`) turns the conversational excerpt into a dated,
  standalone insight and says which open questions it answers. Its output is
  checked before the user sees it: numbers must occur in the excerpt or the
  tool outputs; question indices must exist. The user confirms in a preview —
  nothing is written to a dossier silently.
"""

from __future__ import annotations

import re
from datetime import datetime

from services.citations import CITE_RE

_URL_RE = re.compile(r"https?://[^\s<>()\[\]\"']+[^\s<>()\[\].,;:!?'\"]")
_DIGITS = re.compile(r"\d[\d.,]*\d|\d")
_REF_RE = re.compile(r"#(\d{1,7})\b|\[\^\s*#?(\d{1,7})")
WEB_TOOLS = ("web_search", "web_fetch_page")
MAX_TITLE = 120
MAX_TEXT = 1500


def provenance(answer: str, trace: list[dict], *, model: str = "", expert: str = "") -> dict:
    """What an answer rests on, from the tool log alone."""
    outputs = "\n".join(str(t.get("output") or "") for t in trace)
    seen_ids = {int(a or b) for a, b in _REF_RE.findall(outputs)}
    docs: list[dict] = []
    for m in CITE_RE.finditer(answer or ""):
        did, page = int(m.group(1)), int(m.group(2)) if m.group(2) else None
        if did in seen_ids and not any(d["id"] == did and d.get("page") == page for d in docs):
            docs.append({"id": did, "page": page} if page else {"id": did})
    returned: list[str] = []
    for t in trace:
        if t.get("name") not in WEB_TOOLS:
            continue
        if t.get("name") == "web_fetch_page" and (t.get("input") or {}).get("url"):
            url = str(t["input"]["url"])
            if url not in returned and not str(t.get("output") or "").startswith("Error"):
                returned.append(url)
        for u in _URL_RE.findall(str(t.get("output") or "")):
            if u not in returned:
                returned.append(u)
    in_answer = [u for u in _URL_RE.findall(answer or "") if u in returned]
    fetched = [str((t.get("input") or {}).get("url")) for t in trace
               if t.get("name") == "web_fetch_page" and (t.get("input") or {}).get("url")]
    web: list[str] = []
    for u in in_answer + [u for u in fetched if u in returned]:
        if u not in web:
            web.append(u)
    kind = "+".join(k for k, v in (("documents", docs), ("web", web)) if v) or "reasoning"
    return {
        "docs": docs[:20], "web": [{"url": u} for u in web[:8]], "evidence": kind,
        "model": model, "expert": expert,
        "at": datetime.now().isoformat(timespec="minutes"),
        "haystack": (outputs[:20000]),  # for the number check, never shown
    }


def plain_refs(text: str) -> str:
    """Chat citation markers (`[^241-p3]`) mean nothing outside the chat
    bubble; in a dossier they become readable references (`[#241 p3]`)."""
    def repl(m: re.Match) -> str:
        return f"[#{int(m.group(1))}" + (f" p{int(m.group(2))}" if m.group(2) else "") + "]"

    return CITE_RE.sub(repl, text or "")


_PLAIN_REF = re.compile(r"\[#\d+(?: p\d+)?\]")


def numbers_missing(text: str, haystack: str) -> list[str]:
    """Numbers of `text` that occur nowhere in `haystack` (document refs
    like `[#241 p3]` are references, not claims, and are skipped)."""
    text = _PLAIN_REF.sub(" ", plain_refs(text))
    flat = re.sub(r"[\s.,]", "", haystack)
    out = []
    for n in _DIGITS.findall(text or ""):
        n = n.rstrip(".,")
        if n in haystack or re.sub(r"[.,]", "", n) in flat:
            continue
        if n not in out:
            out.append(n)
    return out


MERGE_SYSTEM = """\
You file an excerpt from a chat answer into a dossier (a binder about one \
matter). Write it as ONE standalone, dated insight: no "as mentioned above", \
no greeting, concise, in {lang}. Keep every number, date and name exactly as \
the excerpt has it; do not add facts that are not in the excerpt. Say which of \
the dossier's open questions (by number) the insight answers, if any, and \
which new open questions it raises (at most 3). If the insight contradicts \
the dossier's current state, say how in one sentence; otherwise leave it empty."""

MERGE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "insight": {"type": "string"},
        "answers_questions": {"type": "array", "items": {"type": "integer"}},
        "new_questions": {"type": "array", "items": {"type": "string"}},
        "contradicts_state": {"type": "string"},
    },
    "required": ["title", "insight", "answers_questions", "new_questions", "contradicts_state"],
}


def merge_input(excerpt: str, dossier, note: str = "") -> str:
    qs = "\n".join(f"{i}. {q}" for i, q in enumerate(dossier.open_questions, 1)) or "(none)"
    parts = [
        f"Dossier: {dossier.display}",
        f"Current state:\n{dossier.current_state or '(empty)'}",
        f"Open questions:\n{qs}",
    ]
    if note.strip():
        parts.append(f"The user's note on this excerpt: {note.strip()}")
    parts.append(f"Excerpt:\n{plain_refs(excerpt).strip()}")
    return "\n\n".join(parts)


def check_merge(result: dict, *, excerpt: str, haystack: str, n_questions: int) -> dict:
    """Clamp and check the merge output. Never raises; the preview shows what
    the check found."""
    title = " ".join(str(result.get("title") or "").split()).lstrip("#").strip()[:MAX_TITLE]
    text = plain_refs(str(result.get("insight") or "")).strip()
    # A heading inside the text would split the insight when the file is read back.
    text = "\n".join(ln.lstrip("#").lstrip() if ln.startswith("#") else ln for ln in text.splitlines())[:MAX_TEXT]
    answers = []
    for i in result.get("answers_questions") or []:
        try:
            i = int(i)
        except (TypeError, ValueError):
            continue
        if 1 <= i <= n_questions and i not in answers:
            answers.append(i)
    new_q = [" ".join(str(q).split())[:300] for q in (result.get("new_questions") or []) if str(q).strip()][:3]
    return {
        "title": title,
        "text": text,
        "answers": answers,
        "new_questions": new_q,
        "contradiction": " ".join(str(result.get("contradicts_state") or "").split())[:400],
        "unsupported": numbers_missing(f"{title} {text}", f"{excerpt}\n{haystack}"),
    }


def verbatim(excerpt: str) -> dict:
    """The escape hatch: store the excerpt as-is, no model."""
    excerpt = plain_refs(excerpt)
    first = next((ln.strip(" #*") for ln in excerpt.splitlines() if ln.strip()), "")
    return {"title": first[:80] or "Excerpt", "text": excerpt.strip()[:MAX_TEXT * 2], "answers": [],
            "new_questions": [], "contradiction": "", "unsupported": []}


async def merge(excerpt: str, dossier, *, model: str, user: str, token: str, haystack: str,
                note: str = "") -> dict:
    from i18n import language_name
    from werkbank import llm_lane

    res = await llm_lane.complete_structured(
        MERGE_SYSTEM.format(lang=language_name(dossier.lang)),
        [{"role": "user", "content": merge_input(excerpt, dossier, note)}],
        model=model, user_id=user, token=token, json_schema=MERGE_SCHEMA, tool_name="file_insight",
        max_tokens=2000, temperature=0.1,
    )
    return check_merge(res, excerpt=excerpt, haystack=haystack, n_questions=len(dossier.open_questions))
