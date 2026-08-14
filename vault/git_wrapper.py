import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from vault.paths import align_vault_perms, git_dir_path

GITIGNORE = """\
# PaperSage vault — ingest only .md files
*
!*/
!*.md
!.gitignore
"""

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "PaperSage",
    "GIT_AUTHOR_EMAIL": "papersage@local",
    "GIT_COMMITTER_NAME": "PaperSage",
    "GIT_COMMITTER_EMAIL": "papersage@local",
}


@dataclass
class FileChange:
    status: Literal["Added", "Modified", "Deleted", "Renamed"]
    path: Path
    old_path: Path | None = None   # Renamed only
    similarity: int | None = None  # Renamed only, 0-100


def _username_from_vault(vault_path: Path) -> str:
    """Derive username from vault_path (last path component)."""
    return vault_path.name


def _git(vault_path: Path, *args: str) -> str:
    """Run git with local --git-dir and vault --work-tree. Return stdout."""
    git_dir = git_dir_path(_username_from_vault(vault_path))
    result = subprocess.run(
        [
            "git",
            f"--git-dir={git_dir}",
            f"--work-tree={vault_path}",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
        env=_GIT_ENV,
    )
    return result.stdout


def ensure_repo(vault_path: Path) -> bool:
    """Ensure a healthy git repo exists for vault_path.

    Git metadata lives under APP_PATH/data/vault_git/<username>/ (local filesystem)
    to avoid mmap failures on WebDAV/FUSE mounts. Returns True if initialised fresh.
    """
    git_dir = git_dir_path(_username_from_vault(vault_path))
    head_file = git_dir / "HEAD"

    if head_file.exists():
        # Verify repo is functional (has at least one commit)
        try:
            _git(vault_path, "rev-parse", "HEAD")
            return False  # healthy
        except subprocess.CalledProcessError:
            # Broken — wipe git dir and reinitialise
            shutil.rmtree(git_dir)
            git_dir.mkdir(parents=True, exist_ok=True)

    git_dir.mkdir(parents=True, exist_ok=True)
    vault_path.mkdir(parents=True, exist_ok=True)

    # Init bare repo into local dir — dir itself becomes the git dir (no .git subdir)
    subprocess.run(
        ["git", "init", "--bare", str(git_dir)],
        capture_output=True, text=True, check=True, env=_GIT_ENV,
    )
    # Point work-tree at vault mount via config (no .git file written to mount)
    subprocess.run(
        ["git", f"--git-dir={git_dir}", "config", "core.worktree", str(vault_path)],
        capture_output=True, text=True, check=True, env=_GIT_ENV,
    )
    subprocess.run(
        ["git", f"--git-dir={git_dir}", "config", "core.bare", "false"],
        capture_output=True, text=True, check=True, env=_GIT_ENV,
    )

    gitignore = vault_path / ".gitignore"
    gitignore.write_text(GITIGNORE, encoding="utf-8")
    align_vault_perms(gitignore)

    # Stage only .gitignore — existing .md files stay untracked so the first
    # sync_user() call detects them as Added and embeds them.
    _git(vault_path, "add", ".gitignore")
    _git(vault_path, "commit", "--allow-empty", "-m", "pbrain: init")
    return True


def diff_name_status(vault_path: Path) -> list[FileChange]:
    """Stage all working-tree changes then return the staged diff vs HEAD.

    Uses -z (NUL-separated records) so paths are emitted verbatim. Without it
    git quotes paths with non-ASCII bytes (e.g. German umlauts) as
    "...\\303\\237..." — the literal quotes/escapes then break Path(), the file
    appears not to exist, and the change is silently skipped (never embedded).
    With -z there is no quoting, so umlaut filenames sync like any other.
    """
    _git(vault_path, "add", "-A")

    raw = _git(vault_path, "diff", "--name-status", "--cached", "-M", "-z", "HEAD")
    # -z format: each record is STATUS \0 PATH \0; renames/copies are
    # STATUS \0 OLDPATH \0 NEWPATH \0. Walk the NUL-separated token stream.
    tokens = raw.split("\0")
    changes: list[FileChange] = []

    i = 0
    n = len(tokens)
    while i < n:
        code = tokens[i]
        if not code:
            i += 1
            continue
        if code[0] in ("R", "C"):  # rename / copy: two following paths
            if i + 2 >= n:
                break
            old_path, new_path = tokens[i + 1], tokens[i + 2]
            sim = int(code[1:]) if len(code) > 1 and code[1:].isdigit() else 100
            changes.append(FileChange(
                status="Renamed",
                path=Path(new_path),
                old_path=Path(old_path),
                similarity=sim,
            ))
            i += 3
            continue
        # single-path statuses: A / M / D (T typechange etc. ignored)
        if i + 1 >= n:
            break
        path = tokens[i + 1]
        i += 2
        if code == "A":
            changes.append(FileChange(status="Added", path=Path(path)))
        elif code == "M":
            changes.append(FileChange(status="Modified", path=Path(path)))
        elif code == "D":
            changes.append(FileChange(status="Deleted", path=Path(path)))

    return changes


def status_porcelain(vault_path: Path) -> set[str]:
    """Working-tree paths that differ from HEAD, without touching the index.

    The mutating counterpart is diff_name_status(), which runs `git add -A`
    first — the UI must never call that just to draw a badge. `-z` for the same
    umlaut reason documented above, `-uall` so a new folder is reported per
    file instead of as `?? Folder/`, `--no-optional-locks` so a read never
    rewrites the index's stat cache. Returns an empty set when the user has no
    repo yet: drawing a page must not initialise one as a side effect.
    """
    git_dir = git_dir_path(_username_from_vault(vault_path))
    if not (git_dir / "HEAD").exists():
        return set()
    try:
        raw = _git(
            vault_path,
            "--no-optional-locks", "status", "--porcelain=v1", "-z", "-uall",
        )
    except subprocess.CalledProcessError:
        return set()

    tokens = [t for t in raw.split("\0")]
    paths: set[str] = set()
    i = 0
    while i < len(tokens):
        record = tokens[i]
        i += 1
        if len(record) < 4:
            continue
        code, path = record[:2], record[3:]
        if code[0] in ("R", "C"):
            i += 1  # rename/copy: the source path follows as its own token
        paths.add(path)
    return paths


def commit(vault_path: Path, msg: str) -> None:
    """Commit whatever is staged. No-op if nothing staged."""
    staged = _git(vault_path, "diff", "--cached", "--name-only").strip()
    if not staged:
        return
    _git(vault_path, "commit", "-m", msg)
