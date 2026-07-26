import asyncio

_user_locks: dict[str, asyncio.Lock] = {}


def get_user_lock(username: str) -> asyncio.Lock:
    return _user_locks.setdefault(username, asyncio.Lock())
