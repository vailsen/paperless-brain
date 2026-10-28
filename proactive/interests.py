"""`_interests.md` — per-user relevance counts, maintained by code (architecture §8).

The LLM reads this (topic labels to reuse), it never writes it. The user may
edit `muted` in the frontmatter and merge topics by editing the table; both are
read back on every use. Counts change on every reaction and every push.

    ---
    pbrain_id: …
    muted: [handyvertrag]
    ---
    | topic | surfaced | interested | dismissed | never | last_reaction |
    |---|---|---|---|---|---|
    | kfz-versicherung | 3 | 2 | 0 | 0 | 2026-10-20 |
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
import uuid
from dataclasses import dataclass, field

from proactive import clock
from proactive.paths import INTERESTS, findings_root

COLUMNS = ("surfaced", "interested", "dismissed", "never")
HEADER = "| topic | surfaced | interested | dismissed | never | last_reaction |\n|---|---|---|---|---|---|\n"


def slugify(label: str) -> str:
    s = (label or "").strip().lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:60]


@dataclass
class Row:
    surfaced: int = 0
    interested: int = 0
    dismissed: int = 0
    never: int = 0
    last_reaction: str = ""


@dataclass
class Interests:
    muted: set[str] = field(default_factory=set)
    rows: dict[str, Row] = field(default_factory=dict)

    def weight(self, topic: str) -> float:
        """§8: clamp(2·(i+1)/(i+d+2), 0.2, 2.0); unknown topic → 1.0."""
        r = self.rows.get(topic)
        if r is None:
            return 1.0
        w = 2 * (r.interested + 1) / (r.interested + r.dismissed + 2)
        return max(0.2, min(2.0, w))


def _path(user: str):
    return findings_root(user) / INTERESTS


def parse(text: str) -> Interests:
    from vault.note_text import properties, split_note

    note = split_note(text)
    meta = properties(note.fm_raw)
    muted = meta.get("muted") or []
    out = Interests(muted={slugify(str(m)) for m in muted if str(m).strip()} if isinstance(muted, list) else set())
    for line in note.body.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5 or cells[0] in ("topic", "") or set(cells[0]) <= {"-", ":"}:
            continue
        topic = slugify(cells[0])
        if not topic:
            continue
        nums = []
        for c in cells[1:5]:
            try:
                nums.append(int(c))
            except ValueError:
                nums.append(0)
        r = out.rows.setdefault(topic, Row())  # a manual merge: rows add up
        r.surfaced += nums[0]
        r.interested += nums[1]
        r.dismissed += nums[2]
        r.never += nums[3]
        last = cells[5] if len(cells) > 5 else ""
        r.last_reaction = max(r.last_reaction, last)
    return out


def render_table(i: Interests) -> str:
    lines = [HEADER]
    for topic in sorted(i.rows):
        r = i.rows[topic]
        lines.append(
            f"| {topic} | {r.surfaced} | {r.interested} | {r.dismissed} | {r.never} | {r.last_reaction} |\n"
        )
    return "".join(lines)


def load(user: str) -> Interests:
    try:
        return parse(_path(user).read_text(encoding="utf-8"))
    except OSError:
        return Interests()


async def record(user: str, topic: str, *events: str, mute: bool = False, unmute: bool = False) -> Interests:
    """Count events (`surfaced` | `interested` | `dismissed` | `never`) for a
    topic in one write; `mute` also adds the topic to `muted`, `unmute` takes
    it off again."""
    from vault.locks import get_user_lock
    from vault.note_text import apply_edits, join_note, split_note
    from vault.notes import NoteConflict, read_note, save_note
    from vault.paths import atomic_write_text, ensure_vault_dir, vault_path

    topic = slugify(topic)
    if not topic:
        return load(user)
    today = clock.local_now().date().isoformat()

    def _apply(i: Interests) -> Interests:
        r = i.rows.setdefault(topic, Row())
        for event in events:
            if event in COLUMNS:
                setattr(r, event, getattr(r, event) + 1)
            if event != "surfaced":
                r.last_reaction = today
        if mute:
            i.muted.add(topic)
        if unmute:
            i.muted.discard(topic)
        return i

    path = _path(user)
    rel = path.relative_to(vault_path(user)).as_posix()
    for _attempt in range(2):
        if not path.exists():
            i = _apply(Interests())

            def _create() -> bool:
                if path.exists():
                    return False
                ensure_vault_dir(path.parent)
                from vault.note_text import dump_mapping

                meta = {"pbrain_id": str(uuid.uuid4()), "muted": sorted(i.muted)}
                atomic_write_text(path, f"---\n{dump_mapping(meta)}---\n{render_table(i)}")
                return True

            async with get_user_lock(user):
                if await asyncio.to_thread(_create):
                    return i
            continue
        snap = await read_note(user, rel)
        i = _apply(parse(snap.text))
        note = split_note(snap.text)
        fm_raw = note.fm_raw
        if mute or unmute:
            fm_raw = apply_edits(fm_raw, set_={"muted": sorted(i.muted)})
        text = join_note(type(note)(fm_raw, render_table(i), note.open_fence, note.close_fence))
        try:
            await save_note(user, rel, text, expected_sha=snap.sha, base_text=snap.text)
            return i
        except NoteConflict:
            continue
    raise RuntimeError(f"{user}: _interests.md kept changing")
