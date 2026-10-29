"""Per-user vault lock — within this process *and* across processes.

Every vault write and every sync takes this lock (`async with
get_user_lock(user)`), because sync's `git add -A` … `commit` window must not
interleave with a write: a file written in between is committed without ever
being embedded, and two `git` processes collide on `index.lock`.

An `asyncio.Lock` alone only covers this process. The proactive worker is a
second process writing finding files into the same vaults, so the lock is an
`asyncio.Lock` (tasks of this process) plus an exclusive `flock` on a per-user
lock file under `data/vault_locks/` (other processes, including a second
container sharing the `./data` volume).

Not reentrant, as before: never take it again inside a block that holds it.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

try:  # POSIX only; the server and the container are Linux.
    import fcntl
except ImportError:  # pragma: no cover — Windows dev machines keep in-process locking
    fcntl = None

#: How often a waiting task retries the file lock. Non-blocking attempts in a
#: loop rather than one blocking call in a thread: a cancelled waiter must not
#: leave a thread behind that acquires the lock later and never releases it.
POLL_SECONDS = 0.05

_user_locks: dict[str, "UserLock"] = {}


def _lock_dir() -> Path:
    from config.settings import settings

    return Path(settings.app_path) / "data" / "vault_locks"


def lock_path(username: str) -> Path:
    """The lock file for one user. The name is sanitised: usernames come from
    Paperless and must not be able to point outside the lock directory."""
    safe = re.sub(r"[^A-Za-z0-9._@-]", "_", username) or "_"
    return _lock_dir() / f"{safe}.lock"


class UserLock:
    """`async with` lock: in-process `asyncio.Lock`, then a file lock."""

    def __init__(self, username: str) -> None:
        self.username = username
        self._lock = asyncio.Lock()
        self._fd: int | None = None

    def locked(self) -> bool:
        return self._lock.locked()

    async def __aenter__(self) -> "UserLock":
        await self._lock.acquire()
        try:
            self._fd = await self._acquire_file()
        except BaseException:
            self._lock.release()
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        fd, self._fd = self._fd, None
        try:
            if fd is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
        finally:
            self._lock.release()

    async def _acquire_file(self) -> int | None:
        if fcntl is None:
            return None
        path = lock_path(self.username)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o660)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return fd
                except BlockingIOError:
                    await asyncio.sleep(POLL_SECONDS)
        except BaseException:
            os.close(fd)
            raise


def get_user_lock(username: str) -> UserLock:
    return _user_locks.setdefault(username, UserLock(username))
