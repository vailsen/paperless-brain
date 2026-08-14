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


def atomic_write_text(path: Path, content: str) -> None:
    """Write text to a vault file atomically (temp file → rename, same dir).

    The rename replaces the inode, so ownership/mode must be applied to the
    *temp* file — preserving the original file's owner when overwriting.
    ``newline=""`` disables newline translation: the caller decides the line
    endings, which is what makes a byte-identical round trip possible.
    """
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8", newline="")
    align_vault_perms(tmp, ref=path if path.exists() else None)
    tmp.rename(path)


def vault_path(username: str) -> Path:
    return settings.vault_root / username


def brain_path(username: str) -> Path:
    return vault_path(username) / settings.brain_subfolder


def memo_path(username: str) -> Path:
    """Voice-memo subfolder. Deliberately NOT under brain_subfolder — memos are
    the user's own notes and belong in the chunked `vault` collection, not the
    agent-curated `brain` one."""
    return vault_path(username) / settings.memo_subfolder


def git_dir_path(username: str) -> Path:
    """Local git metadata dir — kept off the vault mount to avoid mmap issues on WebDAV/FUSE."""
    return settings.app_path / "data" / "vault_git" / username


def ensure_vault_dir(path: Path) -> None:
    """Create a directory inside the vault mount (idempotent).

    ``mkdir`` is not the same on a bind-mounted / FUSE-backed vault as it is on
    a local disk: when the directory is already there but was created by another
    uid (WebDAV server, host side of the mount, unprivileged-LXC id mapping),
    the kernel answers EPERM instead of EEXIST, and ``exists()`` can report
    False when the mount refuses the ``stat``. So the existence check happens
    *after* the failure, not before it, and only a genuinely missing directory
    is reported as an error — with the mount named, since that is what has to be
    fixed.
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except FileExistsError:
        return
    except OSError as exc:
        if path.is_dir():
            return
        raise OSError(
            f"cannot create {path} — check the permissions of the vault mount "
            f"({settings.vault_root}) for the user running PaperlessBrain: {exc}"
        ) from exc
    align_vault_perms(path, mode=VAULT_DIR_MODE)


def ensure_git_dir(username: str) -> None:
    """Local git metadata dir. Lives outside the vault mount, so a plain mkdir."""
    git_dir_path(username).mkdir(parents=True, exist_ok=True)


def ensure_user_dirs(username: str) -> None:
    """Create vault + brain subdirectory and local git dir for the user (idempotent)."""
    ensure_vault_dir(brain_path(username))
    ensure_git_dir(username)


def ensure_memo_dir(username: str) -> None:
    """Create the memo subfolder and the local git dir (idempotent).

    Deliberately does *not* touch the brain subfolder: a voice memo is a vault
    note and has no business failing because the agent's memory folder cannot be
    created. `parents=True` covers the vault root for a first-time user.
    """
    ensure_vault_dir(memo_path(username))
    ensure_git_dir(username)
