"""The vault lock holds across processes, not only across tasks.

The proactive worker is a second process writing into the same vaults; an
asyncio.Lock alone would let it interleave with the app's `git add -A … commit`.
"""

import asyncio
import subprocess
import sys
import textwrap
import time

import pytest

from vault import locks

pytestmark = pytest.mark.skipif(locks.fcntl is None, reason="POSIX file locks only")


def _hold_in_other_process(path, seconds):
    """Start a process that takes the same flock and holds it."""
    code = textwrap.dedent(f"""
        import fcntl, os, sys, time
        fd = os.open({str(path)!r}, os.O_RDWR | os.O_CREAT, 0o660)
        fcntl.flock(fd, fcntl.LOCK_EX)
        print("held", flush=True)
        time.sleep({seconds})
    """)
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    return proc


def test_another_process_holding_the_lock_blocks_us():
    path = locks.lock_path("alice-xproc")
    path.parent.mkdir(parents=True, exist_ok=True)
    other = _hold_in_other_process(path, 0.6)

    async def _take():
        start = time.monotonic()
        async with locks.get_user_lock("alice-xproc"):
            return time.monotonic() - start

    waited = asyncio.run(_take())
    other.wait()
    assert waited >= 0.4


def test_a_cancelled_waiter_leaves_no_lock_behind():
    path = locks.lock_path("bob-cancel")
    path.parent.mkdir(parents=True, exist_ok=True)
    other = _hold_in_other_process(path, 0.4)

    async def _scenario():
        lock = locks.get_user_lock("bob-cancel")

        async def _wait():
            async with lock:
                pass

        task = asyncio.create_task(_wait())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.to_thread(other.wait)
        start = time.monotonic()
        async with lock:          # must not wait for a leaked holder
            return time.monotonic() - start

    assert asyncio.run(_scenario()) < 0.2


def test_tasks_in_one_process_still_take_turns():
    order = []

    async def _worker(name):
        async with locks.get_user_lock("carol-tasks"):
            order.append(f"{name}-in")
            await asyncio.sleep(0.05)
            order.append(f"{name}-out")

    async def _both():
        await asyncio.gather(_worker("a"), _worker("b"))

    asyncio.run(_both())
    assert order in (["a-in", "a-out", "b-in", "b-out"], ["b-in", "b-out", "a-in", "a-out"])


def test_a_username_cannot_point_outside_the_lock_directory():
    for name in ("../../etc/passwd", "..", "a/b", ""):
        path = locks.lock_path(name)
        assert path.parent == locks._lock_dir()
        assert "/" not in path.name
