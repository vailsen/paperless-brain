"""Search the vault's notes by their text — the note explorer's search field.

Deliberately small query language:

- words: all must occur (any order), case- and accent-insensitive;
- `"exact phrase"`;
- `tag:x` — frontmatter `tags` or an inline `#x`;
- `path:folder` — the note's path contains it;
- `-word` — the note must not contain it.

Name hits rank first, then notes by how many lines match. Each result carries
up to three matching lines (number + text) so the page can show snippets and
jump to the first one.
"""

from __future__ import annotations

import re
import shlex
import unicodedata
from dataclasses import dataclass, field

from vault.scan import iter_notes

MAX_RESULTS = 200
SNIPPETS = 3


def fold(s: str) -> str:
    """Lower case without accents: "Müller" finds "muller" and vice versa."""
    s = unicodedata.normalize("NFKD", s.casefold())
    return "".join(c for c in s if not unicodedata.combining(c)).replace("ß", "ss")


@dataclass
class Query:
    words: list[str] = field(default_factory=list)  # folded; phrases included
    exclude: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.words or self.tags or self.paths or self.exclude)


def parse(text: str) -> Query:
    q = Query()
    try:
        parts = shlex.split(text or "")
    except ValueError:  # an unbalanced quote: take the words as they are
        parts = (text or "").split()
    for p in parts:
        low = p.lower()
        if low.startswith("tag:") and len(p) > 4:
            q.tags.append(fold(p[4:].lstrip("#")))
        elif low.startswith("path:") and len(p) > 5:
            q.paths.append(fold(p[5:]))
        elif p.startswith("-") and len(p) > 1:
            q.exclude.append(fold(p[1:]))
        elif p:
            q.words.append(fold(p))
    return q


_FM_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.S)
_INLINE_TAG_RE = re.compile(r"(?:^|[\s(])#([\w\-/]+)", re.U)


def note_tags(text: str) -> set[str]:
    """Frontmatter `tags` (list or comma string) and inline `#tags`, folded."""
    out: set[str] = set()
    m = _FM_RE.match(text)
    body = text
    if m:
        body = text[m.end():]
        try:
            import yaml

            meta = yaml.safe_load(m.group(1)) or {}
        except Exception:  # noqa: BLE001 — broken frontmatter: no tags from it
            meta = {}
        tags = meta.get("tags") if isinstance(meta, dict) else None
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.replace(",", " ").split()]
        for t in tags or []:
            out.add(fold(str(t).lstrip("#")))
    for line in body.splitlines():
        if line.lstrip().startswith(("#" + " ", "##")):
            continue  # a heading, not a tag
        for t in _INLINE_TAG_RE.findall(line):
            if not t.isdigit():
                out.add(fold(t))
    return out


@dataclass
class Hit:
    rel: str
    name_hit: bool
    score: int
    lines: list[tuple[int, str]]  # (1-based line number, text)


def search(username: str, text: str, limit: int = MAX_RESULTS) -> list[Hit]:
    q = parse(text)
    if q.empty:
        return []
    hits: list[Hit] = []
    for rel, body in iter_notes(username):
        frel = fold(rel)
        if any(p not in frel for p in q.paths):
            continue
        whole = fold(body)
        name = fold(rel.rsplit("/", 1)[-1])
        if any(x in whole or x in name for x in q.exclude):
            continue
        if q.tags:
            tags = note_tags(body)
            if not all(any(t == x or t.startswith(x + "/") for x in tags) for t in q.tags):
                continue
        if not all(w in whole or w in name for w in q.words):
            continue
        lines: list[tuple[int, str]] = []
        count = 0
        if q.words:
            for i, line in enumerate(body.splitlines(), 1):
                fl = fold(line)
                if any(w in fl for w in q.words):
                    count += 1
                    if len(lines) < SNIPPETS:
                        lines.append((i, line.strip()[:220]))
        name_hit = bool(q.words) and all(w in name for w in q.words)
        hits.append(Hit(rel, name_hit, count, lines))
    hits.sort(key=lambda h: (not h.name_hit, -h.score, h.rel.casefold()))
    return hits[:limit]


def all_tags(username: str) -> list[str]:
    """Every tag in the vault, for `#` completion in the editor."""
    seen: dict[str, str] = {}
    for _rel, body in iter_notes(username):
        for t in note_tags(body):
            seen.setdefault(t, t)
    return sorted(seen)
