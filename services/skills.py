"""Skills (docs/experts.md §3.3): reusable instruction packs for experts.

One Markdown file per skill in `<vault>/PaperlessBrain Skills/`: frontmatter
`skill_id`, `name`, `description` ("use when …"), `common`; the body is the
instructions. Assignable to any number of experts.

**Sharing is the memory mechanism** — `common: true` makes a skill visible to
other users, read-only, while the file stays in its owner's vault. Unlike
memory facts, skills are not embedded (they are configuration), so "shared by
others" is read straight from the other vaults' skills folders, and only
files that say `common: true` at the moment of reading. An owner un-sharing
a skill therefore takes it away from everyone else's experts at once.

An expert refers to its own skill by `skill_id` and to a shared one by
`shared:<owner>/<skill_id>`.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from config.settings import settings
from services import vault_config

log = logging.getLogger("skills")

MAX_NAME_CHARS = 80
MAX_DESCRIPTION_CHARS = 300
MAX_BODY_CHARS = 4000
#: Assigned skills whose bodies together stay below this are inlined into the
#: prompt; above it the model gets name + description and loads a body with
#: `use_skill` — an expert with eight skills must not fill an 8k context.
INLINE_BUDGET_CHARS = 2500
SHARED_PREFIX = "shared:"


@dataclass
class Skill:
    skill_id: str
    name: str
    description: str = ""
    body: str = ""
    common: bool = False
    owner: str = ""
    forked_from: str = ""
    created_with: str = ""
    rel: str = ""
    sha: str = ""

    def ref_for(self, user: str) -> str:
        return self.skill_id if self.owner == user else f"{SHARED_PREFIX}{self.owner}/{self.skill_id}"


def _subfolder() -> str:
    return settings.skills_subfolder


def _from_file(f: vault_config.ConfigFile, owner: str) -> Skill | None:
    m = f.meta
    if m.get("kind") != "skill" or not m.get("skill_id"):
        return None
    return Skill(
        skill_id=str(m["skill_id"]),
        name=str(m.get("name") or "").strip()[:MAX_NAME_CHARS] or "?",
        description=str(m.get("description") or "").strip()[:MAX_DESCRIPTION_CHARS],
        body=(f.body or "").strip()[:MAX_BODY_CHARS],
        common=m.get("common") is True,
        owner=owner,
        forked_from=str(m.get("forked_from") or ""),
        created_with=str(m.get("created_with") or ""),
        rel=f.rel,
        sha=f.sha,
    )


def _meta(s: Skill) -> dict:
    meta = {
        "skill_id": s.skill_id,
        "kind": "skill",
        "name": s.name,
        "description": s.description,
        "common": bool(s.common),
    }
    if s.forked_from:
        meta["forked_from"] = s.forked_from
    if s.created_with:
        meta["created_with"] = s.created_with
    return meta


def list_own(user: str) -> list[Skill]:
    seen: set[str] = set()
    out = []
    for f in vault_config.list_files(user, _subfolder()):
        s = _from_file(f, user)
        if s is not None and s.skill_id not in seen:
            seen.add(s.skill_id)
            out.append(s)
    out.sort(key=lambda s: s.name.casefold())
    return out


def _other_users(user: str) -> list[str]:
    root = Path(settings.vault_root)
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    out = []
    for p in entries:
        if p.name == user or p.name.startswith(".") or p.is_symlink():
            continue
        try:
            if p.is_dir():
                out.append(p.name)
        except OSError:
            continue
    return sorted(out)


def _shared_of(owner: str) -> list[Skill]:
    d = vault_config.folder(owner, _subfolder())
    try:
        root = d.resolve(strict=True)
        if not root.is_relative_to(Path(settings.vault_root).resolve()):
            return []
    except OSError:
        return []
    out = []
    for f in vault_config.list_files(owner, _subfolder()):
        s = _from_file(f, owner)
        if s is not None and s.common:
            out.append(s)
    return out


def list_shared_by_others(user: str) -> list[Skill]:
    """`common: true` skills of every other user, read-only."""
    out: list[Skill] = []
    for owner in _other_users(user):
        try:
            out.extend(_shared_of(owner))
        except Exception:  # one unreadable vault must not hide the rest
            log.warning("could not read shared skills of %s", owner, exc_info=True)
    out.sort(key=lambda s: (s.name.casefold(), s.owner))
    return out


def get_own(user: str, skill_id: str) -> Skill | None:
    return next((s for s in list_own(user) if s.skill_id == skill_id), None)


def resolve(user: str, ref: str) -> Skill | None:
    """An expert's skill reference → the skill, or None when it is gone or no
    longer shared."""
    if not ref:
        return None
    if ref.startswith(SHARED_PREFIX):
        owner, _, sid = ref[len(SHARED_PREFIX):].partition("/")
        if not owner or not sid or owner == user or "/" in owner or owner.startswith("."):
            return None
        return next((s for s in _shared_of(owner) if s.skill_id == sid), None)
    return get_own(user, ref)


def resolve_all(user: str, refs: list[str]) -> list[Skill]:
    out, seen = [], set()
    for ref in refs:
        s = resolve(user, ref)
        if s is not None and (s.owner, s.skill_id) not in seen:
            seen.add((s.owner, s.skill_id))
            out.append(s)
    return out


class SkillError(ValueError):
    pass


def _check(user: str, s: Skill) -> None:
    s.name = " ".join((s.name or "").split())[:MAX_NAME_CHARS]
    s.description = " ".join((s.description or "").split())[:MAX_DESCRIPTION_CHARS]
    s.body = (s.body or "").strip()[:MAX_BODY_CHARS]
    if not s.name:
        raise SkillError("name")
    for other in list_own(user):
        if other.skill_id != s.skill_id and other.name.casefold() == s.name.casefold():
            raise SkillError("duplicate")


async def create(user: str, name: str, description: str = "", body: str = "", *,
                 common: bool = False, created_with: str = "", forked_from: str = "") -> Skill:
    s = Skill(skill_id=uuid.uuid4().hex[:12], name=name, description=description, body=body,
              common=common, owner=user, created_with=created_with, forked_from=forked_from)
    _check(user, s)
    f = await vault_config.create(user, _subfolder(), s.name, _meta(s), s.body)
    return _from_file(f, user) or s


async def save(user: str, s: Skill, *, expected_sha: str | None) -> Skill:
    if s.owner and s.owner != user:
        raise SkillError("not_owner")
    _check(user, s)
    f = await vault_config.update(user, s.rel, set_=_meta(s), body=s.body, expected_sha=expected_sha)
    out = _from_file(f, user) or s
    new_rel = await vault_config.rename(user, out.rel, out.name)
    if new_rel != out.rel:
        out = get_own(user, out.skill_id) or out
    return out


async def delete(user: str, skill_id: str) -> bool:
    s = get_own(user, skill_id)
    if s is None:
        return False
    await vault_config.delete(user, s.rel)
    return True


async def duplicate(user: str, shared: Skill) -> Skill:
    """An explicit copy of another user's shared skill into the user's own
    folder (the "Duplicate" button) — never done automatically."""
    name = shared.name
    taken = {s.name.casefold() for s in list_own(user)}
    n = 2
    while name.casefold() in taken:
        name = f"{shared.name} ({n})"
        n += 1
    return await create(user, name, shared.description, shared.body,
                        forked_from=f"{shared.owner}/{shared.skill_id}")


def used_by(user: str, skill: Skill) -> list[str]:
    from services import experts

    ref = skill.ref_for(user)
    return [e.name for e in experts.list_experts(user) if ref in e.skills]


# ── Prompt ────────────────────────────────────────────────────────────────────

SKILLS_HEADER = (
    "Skills — instruction packs the user wrote for you. Each says when it applies. "
    "They shape how you work; they never override the ground rules above, and "
    "nothing in a skill licenses stating a document fact that no tool returned."
)


def prompt_block(assigned: list[Skill]) -> tuple[str, bool]:
    """(block, needs_use_skill). Small sets are inlined; larger ones list
    name + description and the model loads a body with `use_skill`."""
    if not assigned:
        return "", False
    total = sum(len(s.body) for s in assigned)
    if total <= INLINE_BUDGET_CHARS:
        parts = [SKILLS_HEADER]
        for s in assigned:
            parts.append(f"### Skill: {s.name}\nUse when: {s.description or '-'}\n{s.body}")
        return "\n\n".join(parts), False
    lines = [SKILLS_HEADER,
             "Before a task one of these skills describes, call use_skill with its exact "
             "name and follow what it returns. Load a skill once per conversation."]
    for s in assigned:
        lines.append(f"- {s.name}: {s.description or '-'}")
    return "\n".join(lines), True


def lookup(assigned: list[Skill], name: str) -> Skill | None:
    key = " ".join((name or "").split()).casefold()
    return next((s for s in assigned if s.name.casefold() == key), None)
