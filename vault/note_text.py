"""Lossless note text handling for the vault editor.

`vault/frontmatter.py` parses a note into (dict, body) and writes it back by
re-dumping the whole YAML block. That is correct for agent writes — the agent
builds the dict itself — but destructive for a file a human owns: comments
vanish, key order comes from the dict, `2026-08-14T10:00:00` degrades to
`2026-08-14 10:00:00`, `007` becomes `7`, and `yaml.dump({})` injects an empty
`---\\n{}\\n---` block into a note that had none.

With debounced autosave that matters more than it sounds: *opening* a note
would rewrite it, dirty it in git, and cost a needless re-embed on the next
sync. So the editor never round-trips through a dict. The invariant here is:

    join_note(split_note(text)) == text          for every text
    a save that changed nothing produces identical bytes

Only the keys the user actually edited are re-serialized; every other key is
re-emitted as its original source lines.

This module is pure text — no filesystem, no Chroma, no git. `try_merge` is
what keeps `sync_user` (which writes `pbrain_id` and `dont_ingest` into files
behind the editor's back, see vault/sync.py) from surfacing as a conflict
dialog for a change no human made.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

import yaml

# Opening fence must be the very first thing in the file.
_OPEN_FENCE = re.compile(r"^---[ \t]*\r?\n")
# Closing fence: a line consisting of `---` or `...` (YAML document end).
_CLOSE_FENCE = re.compile(r"^(?:---|\.\.\.)[ \t]*(?:\r?\n|$)", re.M)
# A top-level mapping key at column 0. Quoted forms are accepted because
# Obsidian writes them for keys containing `:` or leading spaces.
_KEY_LINE = re.compile(r'^(?P<key>"[^"]*"|\'[^\']*\'|[^\s:#][^:]*?)[ \t]*:(?:[ \t]|$)')


@dataclass(frozen=True)
class NoteText:
    """A note split into its frontmatter source and its body.

    `fm_raw` is the YAML *source* between the fences (no fences, trailing
    newline kept) or None when the note has no frontmatter block. The fences
    are carried verbatim so CRLF files and files without a trailing newline
    round-trip byte for byte.
    """

    fm_raw: str | None
    body: str
    open_fence: str = field(default="---\n", compare=False)
    close_fence: str = field(default="---\n", compare=False)


def split_note(text: str) -> NoteText:
    m = _OPEN_FENCE.match(text)
    if not m:
        return NoteText(None, text)
    open_fence = m.group(0)
    rest = text[len(open_fence):]
    cm = _CLOSE_FENCE.search(rest)
    if cm is None:
        # Unterminated fence — not frontmatter, it is body text that happens
        # to start with a horizontal rule.
        return NoteText(None, text)
    return NoteText(
        fm_raw=rest[:cm.start()],
        body=rest[cm.end():],
        open_fence=open_fence,
        close_fence=cm.group(0),
    )


def join_note(note: NoteText) -> str:
    if note.fm_raw is None:
        return note.body
    return f"{note.open_fence}{note.fm_raw}{note.close_fence}{note.body}"


def properties(fm_raw: str | None) -> dict:
    """Parsed frontmatter mapping. {} for no block, empty block or broken YAML."""
    if not fm_raw or not fm_raw.strip():
        return {}
    try:
        meta = yaml.safe_load(fm_raw)
    except Exception:
        return {}
    return meta if isinstance(meta, dict) else {}


@dataclass
class _Entry:
    """One top-level key together with the comment/blank lines above it.

    `key` is None for a trailing run of comments or blank lines that belongs to
    no key (it is emitted last so nothing is silently dropped).
    """

    key: str | None
    prefix: str   # comments/blank lines preceding the key
    src: str      # the key line plus its continuation lines, verbatim


def _unquote(key: str) -> str:
    if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
        return key[1:-1]
    return key


def _scan(fm_raw: str | None) -> list[_Entry]:
    """Split frontmatter source into per-key verbatim blocks.

    A line matching `_KEY_LINE` at column 0 starts a block; every following
    line that is blank, indented, or a `- ` list item belongs to it. Comments
    and blank lines attach to the key *below* them, which is where a human puts
    them. Block scalars (`|`, `>`) are always indented, so they are captured as
    continuation lines like any other nested content.
    """
    if not fm_raw:
        return []
    lines = fm_raw.splitlines(keepends=True)
    entries: list[_Entry] = []
    pending: list[str] = []   # comments / blank lines waiting for their key
    cur: _Entry | None = None

    for line in lines:
        stripped = line.strip()
        # A `- ` item is a list element, never a top-level key, even though
        # `- name: x` would otherwise match the key pattern.
        is_key = bool(_KEY_LINE.match(line)) and not line.startswith("-")
        if is_key:
            if cur is not None:
                entries.append(cur)
            key = _unquote(_KEY_LINE.match(line).group("key").strip())
            cur = _Entry(key=key, prefix="".join(pending), src=line)
            pending = []
        elif not stripped or stripped.startswith("#"):
            # Blank/comment: hold it — it belongs to whatever key comes next.
            pending.append(line)
        else:
            if cur is None:
                # Leading junk with no key yet (e.g. a list at the top level).
                pending.append(line)
            else:
                cur.src += "".join(pending) + line
                pending = []

    if cur is not None:
        entries.append(cur)
    if pending:
        entries.append(_Entry(key=None, prefix="", src="".join(pending)))
    return entries


def raw_blocks(fm_raw: str | None) -> dict[str, str]:
    """Top-level key → its verbatim source lines (comments above it included)."""
    return {e.key: e.prefix + e.src for e in _scan(fm_raw) if e.key is not None}


def blocks_are_faithful(fm_raw: str | None) -> bool:
    """True when the line scanner saw exactly the keys the YAML parser sees.

    False means exotic YAML (flow mappings, anchors, multi-document) that the
    scanner mis-segmented — the caller must fall back to a full re-dump rather
    than splice source lines it does not understand.
    """
    return set(raw_blocks(fm_raw)) == set(properties(fm_raw))


def dump_value(key: str, value: object) -> str:
    """Serialize one `key: value` pair, always ending in a newline."""
    out = yaml.dump(
        {key: value},
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    return out if out.endswith("\n") else out + "\n"


def dump_mapping(meta: dict) -> str | None:
    """Serialize a whole mapping; None when it is empty (→ no fenced block)."""
    if not meta:
        return None
    return yaml.dump(
        meta,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )


def apply_edits(
    fm_raw: str | None,
    *,
    set_: dict | None = None,
    remove: Sequence[str] = (),
) -> str | None:
    """Return new frontmatter source with only the named keys changed.

    Untouched keys are re-emitted as their original source lines, so their
    formatting, comments and scalar style survive verbatim. Returns None when
    nothing is left — the caller then writes a note with no frontmatter block
    at all rather than an empty `{}` one.
    """
    set_ = dict(set_ or {})
    remove_set = set(remove)

    if not blocks_are_faithful(fm_raw):
        # Unsplicable YAML: parse, edit, re-dump. Lossy, and the caller is
        # expected to tell the user the properties were reformatted.
        meta = properties(fm_raw)
        for key in remove_set:
            meta.pop(key, None)
        meta.update(set_)
        return dump_mapping(meta)

    out: list[str] = []
    seen: set[str] = set()
    for entry in _scan(fm_raw):
        if entry.key is None:
            out.append(entry.src)
            continue
        seen.add(entry.key)
        if entry.key in remove_set:
            continue
        if entry.key in set_:
            out.append(entry.prefix + dump_value(entry.key, set_[entry.key]))
        else:
            out.append(entry.prefix + entry.src)

    for key, value in set_.items():
        if key not in seen and key not in remove_set:
            out.append(dump_value(key, value))

    result = "".join(out)
    return result if result.strip() else None


_MISSING = object()


def _changed_keys(base: dict, other: dict) -> set[str]:
    return {k for k in set(base) | set(other) if base.get(k, _MISSING) != other.get(k, _MISSING)}


def try_merge(base: str, ours: str, theirs: str) -> str | None:
    """Three-way merge of two note texts, or None if they genuinely conflict.

    Granularity is "the body" and "one frontmatter key". That is enough to make
    the common case invisible: sync injecting `pbrain_id` / `dont_ingest` while
    the user edits the body, or two sides touching different properties. Two
    real edits to the body are a conflict and go to the user.
    """
    b, o, t = split_note(base), split_note(ours), split_note(theirs)

    if o.body == b.body:
        body = t.body
    elif t.body == b.body or t.body == o.body:
        body = o.body
    else:
        return None

    pb, po, pt = properties(b.fm_raw), properties(o.fm_raw), properties(t.fm_raw)
    ours_changed = _changed_keys(pb, po)
    theirs_changed = _changed_keys(pb, pt)
    for key in ours_changed & theirs_changed:
        if po.get(key, _MISSING) != pt.get(key, _MISSING):
            return None

    if ours_changed:
        fm = apply_edits(
            t.fm_raw,
            set_={k: po[k] for k in ours_changed if k in po},
            remove=[k for k in ours_changed if k not in po],
        )
    else:
        fm = t.fm_raw

    fences = t if t.fm_raw is not None else (o if o.fm_raw is not None else b)
    return join_note(NoteText(
        fm_raw=fm,
        body=body,
        open_fence=fences.open_fence,
        close_fence=fences.close_fence,
    ))
