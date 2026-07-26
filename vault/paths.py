import os
from pathlib import Path

from config.settings import settings

VAULT_FILE_MODE = 0o660
VAULT_DIR_MODE = 0o2775


def align_vault_perms(target: Path, ref: Path | None = None, mode: int = VAULT_FILE_MODE) -> None:
    """Make a vault entry accessible to the WebDAV server's user.

    PaperSage may run as a different user (e.g. root) than the WebDAV server
    serving the vault to Obsidian; without this, temp+rename writes leave files
    owned by the service user with umask-derived modes (root/0600 → Remotely
    Save gets 403). Sets a group-writable mode and, when possible, aligns
    uid/gid with ``ref`` (default: the parent directory). chown requires root
    and chmod may fail on FUSE mounts — both are best-effort; the setgid
    parent directory still guarantees the correct group.
    """
    try:
        os.chmod(target, mode)
    except OSError:
        pass
    try:
        st = os.stat(ref if ref is not None else target.parent)
        os.chown(target, st.st_uid, st.st_gid)
    except OSError:
        pass


def vault_path(username: str) -> Path:
    return settings.vault_root / username


def brain_path(username: str) -> Path:
    return vault_path(username) / settings.brain_subfolder


def git_dir_path(username: str) -> Path:
    """Local git metadata dir — kept off the vault mount to avoid mmap issues on WebDAV/FUSE."""
    return settings.app_path / "data" / "vault_git" / username


def ensure_user_dirs(username: str) -> None:
    """Create vault + brain subdirectory and local git dir for the user (idempotent)."""
    brain = brain_path(username)
    if not brain.exists():
        brain.mkdir(parents=True, exist_ok=True)
        align_vault_perms(brain, mode=VAULT_DIR_MODE)
    git_dir_path(username).mkdir(parents=True, exist_ok=True)
