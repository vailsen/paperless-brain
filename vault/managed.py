"""The folders PaperlessBrain writes into the vault — the one place that knows them.

Other parts of the app parse what is in them: a stray keystroke in a finding's
frontmatter changes its status, one in an expert file changes the expert. The
note explorer therefore opens their notes read-only until the user unlocks one
(`is_locked`), and never renames, moves or deletes the folders themselves
(`managed_root`) — a renamed folder silently orphans everything in it, see the
warnings in `config/settings.py`.

Memos are the exception to the lock (decided 2026-11-27): they are the user's
own dictated notes, so they edit freely — only their folder is protected.

A UI guard against accidents, not security: the app's own writers (memory,
memos, findings, research) are unaffected.
"""

from __future__ import annotations


def roots() -> dict[str, str]:
    """kind → folder name, from the settings (never hard-coded)."""
    from config.settings import settings

    out = {
        "memory": settings.brain_subfolder,
        "memos": settings.memo_subfolder,
        "research": settings.research_subfolder,
        "experts": settings.experts_subfolder,
        "skills": settings.skills_subfolder,
    }
    try:
        from proactive.paths import findings_rel

        out["findings"] = findings_rel().split("/", 1)[0]
    except Exception:  # noqa: BLE001 — proactive absent or misconfigured: the default name
        out["findings"] = "PaperlessBrain Findings"
    return {k: v.strip("/") for k, v in out.items() if v}


def _first(rel: str) -> str:
    return (rel or "").replace("\\", "/").strip("/").split("/", 1)[0]


def managed_root(rel: str) -> str | None:
    """The kind of managed folder `rel` lies in (or is), else None."""
    first = _first(rel)
    for kind, name in roots().items():
        if first == name:
            return kind
    return None


def is_root(rel: str) -> bool:
    """`rel` is one of the managed folders itself — never renamed, moved or deleted."""
    rel = (rel or "").replace("\\", "/").strip("/")
    return bool(rel) and "/" not in rel and managed_root(rel) is not None


def is_locked(rel: str) -> bool:
    """Notes here open read-only until unlocked (everything managed but memos)."""
    kind = managed_root(rel)
    return kind is not None and kind != "memos"
