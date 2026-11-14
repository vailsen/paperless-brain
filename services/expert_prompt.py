"""What the active expert adds to a chat turn (docs/experts.md §5.1).

Blocks, in this order, all code-framed, placed by `build_system_prompt` before
the user's own personality text (which stays in the subordinated custom-
instructions slot):

1. scope   — which part of the archive this expert sees (names, not ids)
2. briefing — a map, not evidence (`services/briefings.py`)
3. skills  — inlined or listed for `use_skill`

The scope itself is enforced in the Paperless client
(`services/expert_scope.py`); the scope block only tells the model why some
documents are not there, so it says so instead of claiming they do not exist.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from services import briefings, skills

#: Universal expert: the code-made archive map of the whole archive, cached —
#: listing every document on every turn would cost more than the map is worth.
_MAP_TTL_S = 600
_map_cache: dict[str, tuple[float, dict]] = {}


@dataclass
class TurnExpert:
    blocks: list[str] = field(default_factory=list)
    personality: str = ""
    skills: list = field(default_factory=list)
    needs_use_skill: bool = False


def scope_block(expert) -> str:
    if expert.builtin or expert.scope.unrestricted:
        return ""
    names = expert.scope_names()
    labels = {"correspondents": "correspondents", "document_types": "document types", "tags": "tags (any of)",
              "exclude_correspondents": "NOT from correspondents",
              "exclude_document_types": "NOT of document types",
              "exclude_tags": "NOT tagged with any of"}
    lines = [f"- {labels[k]}: {', '.join(v)}" for k, v in names.items() if v]
    return (
        f"You are the expert \"{expert.name}\". You see only part of the user's archive — "
        "documents matching ALL of these:\n" + "\n".join(lines) + "\n"
        "Documents outside this scope are invisible to your tools; that is not evidence that "
        "they do not exist. If a question needs them, say that it is outside your scope and "
        "suggest asking the Universal expert."
    )


_filling: set[str] = set()


def _universal_map(user: str, token: str) -> dict:
    """The cached map, or {} while it is being built in the background — the
    first turn must not wait for a listing of the whole archive."""
    hit = _map_cache.get(user)
    if hit and time.monotonic() - hit[0] < _MAP_TTL_S:
        return hit[1]
    if user not in _filling:
        _filling.add(user)
        import asyncio

        async def _fill() -> None:
            try:
                await _build_map(user, token)
            except Exception:
                pass
            finally:
                _filling.discard(user)

        asyncio.ensure_future(_fill())
    return hit[1] if hit else {}


async def _build_map(user: str, token: str) -> dict:
    from config.settings import settings
    from pipelines.search import _drop_no_ingest
    from services.paperless import PaperlessClient

    docs = _drop_no_ingest(await PaperlessClient(settings.paperless_url, token).list_documents())
    m = briefings.archive_map(docs)
    _map_cache[user] = (time.monotonic(), m)
    return m


async def for_turn(user: str, token: str, expert, *, ctx_window: int = 0,
                   loaded_skills: set[str] | frozenset[str] = frozenset(), _=lambda s: s) -> TurnExpert:
    out = TurnExpert(personality=expert.personality or "")
    sb = scope_block(expert)
    if sb:
        out.blocks.append(sb)
    b = briefings.load(user, expert.expert_id)
    if b is not None:
        out.blocks.append(briefings.prompt_block(b, ctx_window=ctx_window))
    elif expert.builtin and token:
        try:
            out.blocks.append(briefings.map_only_block(_universal_map(user, token), _))
        except Exception:
            pass  # the map is a convenience; the turn must not fail on it
    out.skills = skills.resolve_all(user, expert.skills)
    block, out.needs_use_skill = skills.prompt_block(
        out.skills, ctx_window=ctx_window, loaded=loaded_skills)
    if block:
        out.blocks.append(block)
    return out
