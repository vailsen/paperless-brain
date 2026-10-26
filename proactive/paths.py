"""Where a user's findings live (architecture §4.2).

`PROACTIVE_FINDINGS_ROOT` is a template with `{user}`; it must resolve inside
that user's vault, because the ledger relies on the vault's lock, git change
detection and (Phase 9) indexing. Default: `<VAULT_ROOT>/{user}/PaperlessBrain Findings`.

    PaperlessBrain Findings/
    ├── _interests.md      code-maintained relevance stats; the user may edit `muted`
    ├── Notices/           one file per finding
    └── Dossiers/          one file per topic (Phase 8)
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

DEFAULT_SUBFOLDER = "PaperlessBrain Findings"
NOTICES = "Notices"
DOSSIERS = "Dossiers"
INTERESTS = "_interests.md"


def findings_rel() -> str:
    """The findings folder relative to a vault root (POSIX, no leading slash).
    Raises ValueError for a template outside the vault."""
    from config.proactive import proactive_settings
    from config.settings import settings

    template = proactive_settings().proactive_findings_root
    if not template:
        return DEFAULT_SUBFOLDER
    marker = "\0user\0"
    resolved = PurePosixPath(template.replace("{user}", marker))
    root = PurePosixPath(str(settings.vault_root)) / marker
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        raise ValueError(
            f"PROACTIVE_FINDINGS_ROOT={template!r} is not inside <VAULT_ROOT>/{{user}}/"
        ) from None
    if marker in str(rel) or not rel.parts or ".." in rel.parts:
        raise ValueError(f"PROACTIVE_FINDINGS_ROOT={template!r} is not a folder in the vault")
    return rel.as_posix()


def findings_root(user: str) -> Path:
    from vault.paths import vault_path

    return vault_path(user) / findings_rel()


def is_findings_path(rel_path: Path | str) -> bool:
    """True for a vault-relative path inside the findings folder."""
    try:
        prefix = PurePosixPath(findings_rel()).parts
    except ValueError:
        return False
    parts = PurePosixPath(str(rel_path).replace("\\", "/")).parts
    return parts[: len(prefix)] == prefix
