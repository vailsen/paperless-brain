"""Web sources in a chat answer: `[^Cleveland Clinic]` → a link.

Document citations are `[^241-p3]`, copied from a tool result. For a web
source there is no such marker to copy, so a model that verifies with
`web_search` / `web_fetch_page` writes the same footnote syntax with the
site's name: `[^Cleveland Clinic]`. The chat rendered that literally — carets,
brackets, nothing to click.

`link_web_refs` resolves each such marker against the pages **a tool returned
in this turn** (`sources_from_trace`) and stores it as a Markdown link,
`([Cleveland Clinic](https://my.clevelandclinic.org/…))`, which reads the same
in the bubble, after a reload, in the PDF export and in a dossier. A URL is
linked only if a tool returned it — a model-made URL is not a source. A name
that matches nothing stays as plain text, `(Zommunique)`, never as a link.

`plain_web_refs` is the render-time fallback for answers stored before this
existed, or without a trace: the same plain text, no lookup.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

WEB_TOOLS = ("web_search", "web_fetch_page")

# A footnote marker that is not a document citation ([^241], [^241-p3] are
# handled by services.citations): any label with a letter in it.
WEB_REF_RE = re.compile(r"\[\^\s*([^\]\n]*[^\W\d_][^\]\n]*?)\s*\]")
# Several in a row — "[^A][^B]" or "[^A], [^B]" — become one parenthesis.
_RUN_RE = re.compile(r"(?:\[\^[^\]\n]+\](?:[ \t]*,[ \t]*|[ \t]?)?)+")
_MD_LINK_RE = re.compile(r"\[([^\]\n]{0,300})\]\((https?://[^)\s]+)\)")
_URL_RE = re.compile(r"https?://[^\s<>()\[\]\"']+[^\s<>()\[\].,;:!?'\"]")
_DOC_REF_RE = re.compile(r"^\s*#?\d{1,7}(?:\s*[-,:]?\s*(?:p|S|page|Seite)\.?\s*\d{1,4})?\s*$", re.I)


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower()
                  .replace("ä", "a").replace("ö", "o").replace("ü", "u").replace("ß", "ss"))


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9äöüß]+", (text or "").lower()) if len(w) >= 3}


def _domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def sources_from_trace(trace: list[dict]) -> list[dict]:
    """`[{url, title, fetched}]` of every page a web tool returned this turn."""
    out: dict[str, dict] = {}
    for t in trace or []:
        name = t.get("name")
        if name not in WEB_TOOLS:
            continue
        output = str(t.get("output") or "")
        if name == "web_fetch_page":
            url = str((t.get("input") or {}).get("url") or "")
            # only a page that was read ("[trafilatura] Content of …"); a failed
            # or blocked fetch is no source
            if url.startswith("http") and re.match(r"\[[^\]]+\] Content of ", output):
                out.setdefault(url, {"url": url, "title": "", "fetched": True})["fetched"] = True
        for title, url in _MD_LINK_RE.findall(output):
            src = out.setdefault(url, {"url": url, "title": "", "fetched": False})
            src["title"] = src["title"] or title.strip()
        for url in _URL_RE.findall(output):
            out.setdefault(url, {"url": url, "title": "", "fetched": False})
    return list(out.values())


def _match(label: str, sources: list[dict]) -> dict | None:
    """The source a label names: the URL itself, the site (`Cleveland Clinic`
    ↔ clevelandclinic.org), or the page title. Fetched pages win a tie — that
    is what the model read."""
    label = label.strip()
    if label.startswith("http"):
        return next((s for s in sources if s["url"].rstrip("/") == label.rstrip("/")), None)
    sq, words = _squash(label), _words(label)
    if len(sq) < 3:
        return None
    best, best_score = None, 0.0
    for s in sources:
        host = _domain(s["url"])
        core = _squash(host.rsplit(".", 1)[0]) if "." in host else _squash(host)
        score = 0.0
        if sq == _squash(host) or sq in core or (len(core) >= 4 and core.split("www")[-1] in sq):
            score = 3.0
        elif words and words <= _words(s["title"]):
            score = 2.0
        elif words and len(words & _words(s["title"] + " " + host)) >= max(1, len(words) - 1):
            score = 1.0
        if score:
            score += 0.5 if s.get("fetched") else 0.0
            if score > best_score:
                best, best_score = s, score
    return best


def _label_md(label: str) -> str:
    return label.replace("[", "(").replace("]", ")")


def _render(labels: list[str], sources: list[dict] | None) -> str:
    parts, seen = [], set()
    for label in labels:
        src = _match(label, sources) if sources else None
        shown = _label_md(_domain(label) if label.startswith("http") else label)
        key = src["url"] if src else shown
        if key in seen:
            continue
        seen.add(key)
        parts.append(f"[{shown}]({src['url']})" if src else shown)
    return "(" + ", ".join(parts) + ")" if parts else ""


def _rewrite(text: str, sources: list[dict] | None) -> str:
    def run(m: re.Match) -> str:
        chunk = m.group(0)
        out, web = [], []

        def flush() -> None:
            if web:
                out.append(_render(web, sources))
                web.clear()

        for mm in re.finditer(r"\[\^([^\]\n]+)\]", chunk):
            label = mm.group(1).strip()
            if _DOC_REF_RE.match(label) or not re.search(r"[^\W\d_]", label):
                flush()
                out.append(mm.group(0))      # a document citation: services.citations
            else:
                web.append(label)
        flush()
        if not any(not x.startswith("[^") for x in out):
            return chunk
        body = ""
        for x in out:
            body += (" " if body and not x.startswith("[^") else "") + x
        tail = " " if chunk.endswith(" ") else ""
        # "brennt's.[^A]" → "brennt's. (A)"
        lead = " " if m.start() and not m.string[m.start() - 1].isspace() and not out[0].startswith("[^") else ""
        return lead + body + tail

    return _RUN_RE.sub(run, text or "")


# A footnote the model defined itself: "[^1]: [Runners World – …](https://…)".
# Its marker "[^1]" looks like a document citation (#1) — "Document #1 not
# found" — so a footnote whose definition is a web address is a web source.
_FOOTDEF_RE = re.compile(r"^[ \t]*\[\^([^\]\n]{1,40})\]:[ \t]*(.+?)[ \t]*$", re.M)


def _footnote_defs(text: str) -> dict[str, tuple[str, str]]:
    out = {}
    for m in _FOOTDEF_RE.finditer(text):
        body = m.group(2)
        md = _MD_LINK_RE.search(body)
        if md:
            out[m.group(1).strip()] = (md.group(1).strip(), md.group(2))
            continue
        url = _URL_RE.search(body)
        if url:
            title = body.replace(url.group(0), "").strip(" -–—:<>()[]")
            out[m.group(1).strip()] = (title, url.group(0))
    return out


def _web_footnotes(text: str, sources: list[dict] | None) -> str:
    """Markers of web footnotes → `([title](url))` inline, definitions gone.
    With `sources` (store time) the link stays only for a page a tool returned
    this turn; without (render time, an answer stored earlier) the model's own
    link stays as the definition already showed it."""
    defs = _footnote_defs(text)
    if not defs:
        return text
    text = _FOOTDEF_RE.sub(lambda m: "" if m.group(1).strip() in defs else m.group(0), text)
    known = {s["url"].rstrip("/") for s in sources or []}

    def one(label: str) -> str:
        title, url = defs[label]
        shown = _label_md(title or _domain(url))
        if sources is not None and url.rstrip("/") not in known:
            return shown
        return f"[{shown}]({url})"

    labels = "|".join(re.escape(k) for k in sorted(defs, key=len, reverse=True))
    run = re.compile(r"(?:\[\^(?:" + labels + r")\](?!:)(?:[ \t]*,[ \t]*|[ \t]?)?)+")

    def sub(m: re.Match) -> str:
        seen, parts = set(), []
        for lab in re.findall(r"\[\^([^\]]+)\]", m.group(0)):
            if lab not in seen:
                seen.add(lab)
                parts.append(one(lab))
        lead = " " if m.start() and not m.string[m.start() - 1].isspace() else ""
        tail = " " if m.group(0).endswith(" ") else ""
        return lead + "(" + ", ".join(parts) + ")" + tail

    return re.sub(r"\n{3,}", "\n\n", run.sub(sub, text)).rstrip() + ("\n" if text.endswith("\n") else "")


def link_web_refs(text: str, trace: list[dict]) -> str:
    """Store-time: web markers → links to pages a tool returned this turn."""
    if not text or "[^" not in text:
        return text
    sources = sources_from_trace(trace)
    return _rewrite(_web_footnotes(text, sources), sources)


def plain_web_refs(text: str) -> str:
    """Render-time fallback: leftover web markers → plain `(name)`."""
    if not text or "[^" not in text:
        return text
    return _rewrite(_web_footnotes(text, None), None)


# A "Sources" list the model writes itself at the end, despite the prompt:
# "## Quellen" and one link per line. Its pages go into the source box under
# the answer, numbered like the rest — the list is not repeated in the text.
_LIST_HEAD_RE = re.compile(
    r"^[ \t]*(?:#{1,4}[ \t]*|\*\*)?(?:Quellen|Quelle|Sources|Source|Referenzen|References)"
    r"(?:[ \t]*\([^)\n]*\))?[ \t]*:?(?:\*\*)?[ \t]*:?[ \t]*$", re.I | re.M)
_LIST_ITEM_RE = re.compile(r"^[ \t]*(?:[-*•]|\d+[.)])[ \t]+.*?\[([^\]\n]{1,200})\]\((https?://[^)\s]+)\)")


def split_source_list(text: str) -> tuple[str, list[tuple[str, str]]]:
    """`(text without the list, [(name, url)])` for a trailing source list —
    a heading followed only by link items. Anything else is left alone."""
    if not text:
        return text, []
    heads = list(_LIST_HEAD_RE.finditer(text))
    if not heads:
        return text, []
    head = heads[-1]
    lines = text[head.end():].split("\n")
    items, used = [], 0
    for i, line in enumerate(lines):
        if not line.strip():
            if items:
                used = i
                break
            continue
        m = _LIST_ITEM_RE.match(line)
        if not m:
            if not items:
                return text, []
            used = i
            break
        items.append((m.group(1).strip(), m.group(2)))
        used = i + 1
    if not items:
        return text, []
    rest = "\n".join(lines[used:]).strip("\n")
    before = text[:head.start()].rstrip()
    return (before + ("\n\n" + rest if rest else "")).rstrip() + "\n", items
