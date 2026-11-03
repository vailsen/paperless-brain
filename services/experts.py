"""Experts (docs/experts.md): a chat persona with a narrowed view of the archive.

One Markdown file per expert in `<vault>/PaperlessBrain Experts/`: the body is
the personality text, the frontmatter holds name, icon, scope, skills,
switched-off tool groups and a default model. Configuration, not knowledge —
never embedded (`vault.router.is_config_path`).

The **Universal expert** is built in: always present (the file is recreated
if deleted), all documents, all tools, not renamable, not deletable. Only its
personality text and its skills are the user's. A scope or `tools_off` in its
file — an Obsidian edit, say — is ignored. It inherits the old chat custom
instructions once (`ensure_universal`).

Every expert has its memory and vault tools: those groups are never in
`tools_off`, whatever the file says.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from config.settings import settings
from services import vault_config
from services.expert_scope import Scope

log = logging.getLogger("experts")

UNIVERSAL_ID = "universal"
#: Same cap and the same prompt slot as the old custom instructions
#: (config/chat_prompts.MAX_CUSTOM_INSTRUCTIONS_CHARS).
MAX_PERSONALITY_CHARS = 2000
MAX_NAME_CHARS = 60
#: Tool groups an expert can never lose (docs/experts.md: every expert has
#: full access to memory and vault).
ALWAYS_ON_GROUPS = frozenset({"memory", "vault"})
#: Opt-in dimensions, then their "never these" counterparts.
SCOPE_KEYS = ("correspondents", "tags", "document_types",
              "exclude_correspondents", "exclude_tags", "exclude_document_types")

#: Icons offered in the editor. Material names, rendered in text colour —
#: experts carry no colour of their own (UI visual discipline).
ICONS = (
    "psychology", "shield", "receipt_long", "home", "directions_car", "medical_services",
    "account_balance", "work", "school", "gavel", "savings", "build", "family_restroom",
    "flight", "pets", "bolt", "science", "translate", "description", "support_agent",
)


@dataclass
class Expert:
    expert_id: str
    name: str
    icon: str = "psychology"
    builtin: bool = False
    personality: str = ""
    scope_meta: dict = field(default_factory=dict)
    skills: list[str] = field(default_factory=list)
    tools_off: list[str] = field(default_factory=list)
    default_model: str = ""
    rel: str = ""
    sha: str = ""

    @property
    def scope(self) -> Scope:
        return Scope() if self.builtin else Scope.from_meta(self.scope_meta)

    @property
    def disabled_groups(self) -> set[str]:
        if self.builtin:
            return set()
        return {g for g in self.tools_off if g not in ALWAYS_ON_GROUPS}

    def scope_names(self) -> dict[str, list[str]]:
        """Display names as stored in the file (refreshed on save)."""
        out: dict[str, list[str]] = {}
        if self.builtin:
            return out
        for key in SCOPE_KEYS:
            names = []
            for item in self.scope_meta.get(key) or []:
                if isinstance(item, dict):
                    names.append(str(item.get("name") or item.get("id")))
                else:
                    names.append(str(item))
            if names:
                out[key] = names
        return out


def enabled() -> bool:
    """The user's "Use experts" switch (Settings > AI). Off, every chat is the
    Universal expert and no picker is offered. Needs a page context."""
    from services.user_prefs import get_pref

    return bool(get_pref("experts_enabled", True))


def _subfolder() -> str:
    return settings.experts_subfolder


def _from_file(f: vault_config.ConfigFile) -> Expert | None:
    m = f.meta
    if m.get("kind") != "expert" or not m.get("expert_id"):
        return None
    builtin = str(m.get("expert_id")) == UNIVERSAL_ID
    scope = m.get("scope") if isinstance(m.get("scope"), dict) else {}
    return Expert(
        expert_id=str(m["expert_id"]),
        name=str(m.get("name") or "").strip()[:MAX_NAME_CHARS] or ("Universal" if builtin else "?"),
        icon=str(m.get("icon") or "psychology"),
        builtin=builtin,
        personality=(f.body or "").strip(),
        scope_meta=scope,
        skills=[str(s) for s in (m.get("skills") or []) if str(s).strip()],
        tools_off=[str(t) for t in (m.get("tools_off") or [])],
        default_model=str(m.get("default_model") or ""),
        rel=f.rel,
        sha=f.sha,
    )


def _meta(e: Expert) -> dict:
    meta = {
        "expert_id": e.expert_id,
        "kind": "expert",
        "name": e.name,
        "icon": e.icon,
        "skills": list(e.skills),
        "default_model": e.default_model,
    }
    if not e.builtin:
        meta["scope"] = {k: list(e.scope_meta.get(k) or []) for k in SCOPE_KEYS if e.scope_meta.get(k)}
        meta["tools_off"] = sorted(set(e.tools_off) - ALWAYS_ON_GROUPS)
    else:
        meta["builtin"] = True
    return meta


def list_experts(user: str) -> list[Expert]:
    """Universal first, then by name. Files that are not experts are skipped;
    a second file claiming the universal id is ignored."""
    seen: set[str] = set()
    out: list[Expert] = []
    for f in vault_config.list_files(user, _subfolder()):
        e = _from_file(f)
        if e is None or e.expert_id in seen:
            continue
        seen.add(e.expert_id)
        out.append(e)
    out.sort(key=lambda e: (not e.builtin, e.name.casefold()))
    if not any(e.builtin for e in out):
        out.insert(0, Expert(expert_id=UNIVERSAL_ID, name="Universal", builtin=True))
    return out


def get(user: str, expert_id: str | None) -> Expert | None:
    if not expert_id:
        return None
    return next((e for e in list_experts(user) if e.expert_id == expert_id), None)


def get_or_universal(user: str, expert_id: str | None) -> Expert:
    return get(user, expert_id) or get(user, UNIVERSAL_ID) or Expert(UNIVERSAL_ID, "Universal", builtin=True)


async def ensure_universal(user: str, token: str | None = None) -> Expert:
    """Write the Universal expert's file if there is none.

    On the very first write its personality is the old chat custom
    instructions (Settings > AI). The old value stays in the credential store
    as a fallback copy; `custom_instructions_migrated` keeps a deleted file
    from importing it again.
    """
    for e in list_experts(user):
        if e.builtin and e.rel:
            return e
    personality = ""
    creds = None
    if token:
        try:
            from services.credential_store import load_credentials

            creds = load_credentials(user, token)
            cs = creds.get("chat_settings") or {}
            if not cs.get("custom_instructions_migrated"):
                personality = str(cs.get("custom_instructions") or "")[:MAX_PERSONALITY_CHARS]
        except Exception:
            log.warning("could not read custom instructions for %s", user, exc_info=True)
            creds = None
    e = Expert(expert_id=UNIVERSAL_ID, name="Universal", builtin=True, personality=personality)
    f = await vault_config.create(user, _subfolder(), "Universal", _meta(e), personality)
    if creds is not None:
        try:
            from services.credential_store import save_credentials

            creds.setdefault("chat_settings", {})["custom_instructions_migrated"] = True
            save_credentials(user, token, creds)
        except Exception:
            log.warning("could not mark custom instructions migrated for %s", user, exc_info=True)
    return _from_file(f) or e


class ExpertError(ValueError):
    """A save the user must correct (shown as a message)."""


def _check_name(user: str, name: str, expert_id: str) -> str:
    name = " ".join((name or "").split())[:MAX_NAME_CHARS]
    if not name:
        raise ExpertError("name")
    for other in list_experts(user):
        if other.expert_id != expert_id and other.name.casefold() == name.casefold():
            raise ExpertError("duplicate")
    return name


async def create(user: str, name: str, icon: str = "psychology") -> Expert:
    eid = uuid.uuid4().hex[:12]
    name = _check_name(user, name, eid)
    e = Expert(expert_id=eid, name=name, icon=icon if icon in ICONS else "psychology")
    f = await vault_config.create(user, _subfolder(), name, _meta(e), "")
    return _from_file(f) or e


async def save(user: str, e: Expert, *, expected_sha: str | None) -> Expert:
    """Write an edited expert. The Universal expert keeps its name, gets no
    scope and no switched-off tools, whatever `e` says."""
    if e.builtin:
        e.name, e.scope_meta, e.tools_off = "Universal", {}, []
        if not e.rel:
            e = await ensure_universal(user) if not e.personality else e
    else:
        e.name = _check_name(user, e.name, e.expert_id)
    e.personality = (e.personality or "")[:MAX_PERSONALITY_CHARS]
    if not e.rel:
        f = await vault_config.create(user, _subfolder(), e.name, _meta(e), e.personality)
        return _from_file(f) or e
    f = await vault_config.update(
        user, e.rel, set_=_meta(e), remove=() if e.builtin else ("builtin",),
        body=e.personality, expected_sha=expected_sha,
    )
    out = _from_file(f) or e
    if not e.builtin:
        new_rel = await vault_config.rename(user, out.rel, out.name)
        if new_rel != out.rel:
            out = get(user, out.expert_id) or out
    return out


async def delete(user: str, expert_id: str) -> bool:
    e = get(user, expert_id)
    if e is None or e.builtin or not e.rel:
        return False
    await vault_config.delete(user, e.rel)
    return True


async def scope_with_names(pl, scope_meta: dict) -> dict:
    """Refresh the display names of a scope from the user's Paperless maps
    (a correspondent renamed in Paperless shows its new name). Ids Paperless
    no longer knows are kept with their stored name, marked `missing`."""
    by_dim = {
        "correspondents": await pl.get_correspondent_map(),
        "tags": await pl.get_tag_map(),
        "document_types": await pl.get_document_types_map(),
    }
    out = {}
    for key in SCOPE_KEYS:
        names = by_dim[key.removeprefix("exclude_")]
        items = []
        for item in scope_meta.get(key) or []:
            raw = item.get("id") if isinstance(item, dict) else item
            try:
                i = int(raw)
            except (TypeError, ValueError):
                continue
            stored = item.get("name") if isinstance(item, dict) else None
            entry = {"id": i, "name": names.get(i) or stored or str(i)}
            if i not in names:
                entry["missing"] = True
            items.append(entry)
        out[key] = items
    return out
