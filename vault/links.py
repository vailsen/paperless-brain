"""Links between notes: resolve, find backlinks, rewrite after a rename or move.

Obsidian's rules, so a vault edited in both behaves the same:

- `[[Name]]`, `[[Name|label]]`, `[[Name#Heading]]`, `[[Folder/Name]]`, and
  relative Markdown links to `.md` files (`[label](Folder/Name.md)`);
- a target resolves to the exact path if there is one, else to the note of
  that file name — the shortest path when several share it;
- after a rename or move, every link that resolved to the old path is
  rewritten (decided 2026-11-27: automatically, like Obsidian's "Automatically
  update internal links"). Only the target changes; label, heading and the
  rest of the line stay byte-identical. A bare name stays a bare name while it
  is unique, else it becomes the path.

Rewrites go through `vault.notes.save_note` (user lock, hash check, three-way
merge), so a note open in Obsidian or being written by the worker is merged,
not overwritten. Links inside managed folders are rewritten too — a finding
pointing at a moved research note must not break.
"""

from __future__ import annotations

import posixpath
import re
from urllib.parse import quote, unquote

from vault.scan import iter_notes, note_paths

WIKI_RE = re.compile(r"(!?)\[\[([^\]\n|#]*)(#[^\]\n|]*)?(\|[^\]\n]*)?\]\]")
MD_LINK_RE = re.compile(r"(?<!!)\[([^\]\n]*)\]\(([^)\s]+\.md)(#[^)\s]*)?\)")


def _stem(rel: str) -> str:
    return rel[:-3] if rel.lower().endswith(".md") else rel


def build_index(paths: list[str]) -> tuple[dict[str, str], dict[str, list[str]]]:
    """(lower-case path without .md → rel, lower-case file name → [rels])."""
    by_path = {_stem(p).lower(): p for p in paths}
    by_name: dict[str, list[str]] = {}
    for p in paths:
        by_name.setdefault(_stem(p).rsplit("/", 1)[-1].lower(), []).append(p)
    for v in by_name.values():
        v.sort(key=lambda r: (r.count("/"), r.casefold()))
    return by_path, by_name


def resolve(target: str, paths: list[str] | None = None, index=None) -> str | None:
    """The note a wikilink target names, or None."""
    t = _stem(unquote((target or "").strip()).replace("\\", "/").strip("/")).lower()
    if not t:
        return None
    by_path, by_name = index or build_index(paths or [])
    if t in by_path:
        return by_path[t]
    cands = by_name.get(t.rsplit("/", 1)[-1])
    if not cands:
        return None
    if "/" in t:  # a partial path: the candidates ending in it
        ends = [c for c in cands if _stem(c).lower().endswith("/" + t) or _stem(c).lower() == t]
        if ends:
            return ends[0]
    return cands[0]


def _md_target(from_rel: str, href: str) -> str:
    base = posixpath.dirname(from_rel)
    return posixpath.normpath(posixpath.join(base, unquote(href)))


def links_in(text: str, from_rel: str, index) -> list[tuple[str, int]]:
    """(resolved rel, line number) for every note link in `text`."""
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        for m in WIKI_RE.finditer(line):
            if m.group(1):
                continue  # an embed, not a link
            r = resolve(m.group(2), index=index) if m.group(2).strip() else None
            if r:
                out.append((r, i))
        for m in MD_LINK_RE.finditer(line):
            if "://" in m.group(2):
                continue
            t = _md_target(from_rel, m.group(2))
            r = resolve(t, index=index)
            if r:
                out.append((r, i))
    return out


def backlinks(username: str, rel: str) -> list[tuple[str, int, str]]:
    """(note, line number, line text) of every link to `rel` from another note."""
    paths = note_paths(username)
    index = build_index(paths)
    out = []
    for src, text in iter_notes(username):
        if src == rel:
            continue
        lines = text.splitlines()
        for target, n in links_in(text, src, index):
            if target == rel:
                out.append((src, n, lines[n - 1].strip()[:200]))
    return out


# ── Rewriting after a rename or move ─────────────────────────────────────────

def _new_wiki_target(old_target: str, new_rel: str, new_index) -> str:
    """Keep the link's style: a bare name stays bare while unique."""
    stem = _stem(new_rel)
    name = stem.rsplit("/", 1)[-1]
    bare = "/" not in old_target.strip().replace("\\", "/")
    if bare and len(new_index[1].get(name.lower(), [])) <= 1:
        return name
    return stem


def rewrite_text(text: str, from_rel: str, moved: dict[str, str], old_index, new_index,
                 new_from_rel: str | None = None) -> str:
    """`text` with every link to a key of `moved` pointing at its value.
    `from_rel` is where the text lived before, `new_from_rel` where it lives now
    (relative Markdown links are relative to the note)."""
    new_from_rel = new_from_rel or from_rel

    def wiki(m: re.Match) -> str:
        if m.group(1) or not m.group(2).strip():
            return m.group(0)
        old = resolve(m.group(2), index=old_index)
        if old not in moved:
            return m.group(0)
        tgt = _new_wiki_target(m.group(2), moved[old], new_index)
        return f"[[{tgt}{m.group(3) or ''}{m.group(4) or ''}]]"

    def md(m: re.Match) -> str:
        href = m.group(2)
        if "://" in href:
            return m.group(0)
        old = resolve(_md_target(from_rel, href), index=old_index)
        target = moved.get(old)
        if target is None and from_rel == new_from_rel:
            return m.group(0)
        target = target or old
        if not target:
            return m.group(0)
        rel_href = posixpath.relpath(target, posixpath.dirname(new_from_rel) or ".")
        return f"[{m.group(1)}]({quote(rel_href, safe='/._-~')}{m.group(3) or ''})"

    lines = text.split("\n")
    for i, line in enumerate(lines):
        if "[[" in line:
            line = WIKI_RE.sub(wiki, line)
        if "](" in line:
            line = MD_LINK_RE.sub(md, line)
        lines[i] = line
    return "\n".join(lines)


async def rewrite_after_move(username: str, moved: dict[str, str], old_paths: list[str]) -> int:
    """Rewrite every note's links after notes moved (`moved`: old rel → new rel).
    `old_paths` is the note list from before the move. Returns how many notes
    changed."""
    from vault import notes

    if not moved:
        return 0
    old_index = build_index(old_paths)
    new_paths = note_paths(username)
    new_index = build_index(new_paths)
    back = {v: k for k, v in moved.items()}
    changed = 0
    for rel, text in iter_notes(username):
        before = back.get(rel, rel)
        out = rewrite_text(text, before, moved, old_index, new_index, new_from_rel=rel)
        if out == text:
            continue
        try:
            await notes.save_note(username, rel, out, expected_sha=notes.sha_of(text), base_text=text)
            changed += 1
        except Exception:  # noqa: BLE001 — one note that cannot be written must not stop the rest
            import logging

            logging.getLogger("vault.links").warning("link rewrite failed for %s", rel, exc_info=True)
    return changed


def moved_map(old_paths: list[str], old_rel: str, new_rel: str) -> dict[str, str]:
    """Old → new note paths for a renamed or moved note or folder."""
    if old_rel.lower().endswith(".md"):
        return {old_rel: new_rel} if old_rel in old_paths else {}
    prefix = old_rel.rstrip("/") + "/"
    return {p: new_rel.rstrip("/") + "/" + p[len(prefix):] for p in old_paths if p.startswith(prefix)}
