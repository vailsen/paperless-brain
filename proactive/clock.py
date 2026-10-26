"""One place for "now", so tests can move time without sleeping.

Stored timestamps are UTC ISO strings in one fixed format, which makes
SQLite's string comparison a time comparison.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

_FMT = "%Y-%m-%dT%H:%M:%SZ"

_offset = timedelta(0)


def utcnow() -> datetime:
    return datetime.now(timezone.utc) + _offset


def local_now() -> datetime:
    from config.settings import local_tz

    return utcnow().astimezone(local_tz())


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(_FMT)


def now_iso() -> str:
    return iso(utcnow())


def parse(s: str) -> datetime:
    return datetime.strptime(s, _FMT).replace(tzinfo=timezone.utc)


def advance(delta: timedelta) -> None:
    """Tests only: shift the clock."""
    global _offset
    _offset += delta


def reset() -> None:
    global _offset
    _offset = timedelta(0)
