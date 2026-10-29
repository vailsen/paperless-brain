"""Typed settings of the proactive system (`docs/proactive-architecture.md` §2).

Read once from `.env` into `proactive_settings()`. Every proactive code path
checks this object — never `os.environ` — so `PROACTIVE_ENABLED=false` has one
meaning everywhere: nothing proactive runs, is registered or creates files.

Channel *availability* follows from server config presence (VAPID keys for
push, an SMTP server for email); there are no per-channel switches. Whether a channel is
*used* is each user's choice in their settings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class SweepWindow:
    """A daily window for budgeted backlog work. `end <= start` crosses midnight."""

    start: time
    end: time
    max_docs: int

    def contains(self, t: time) -> bool:
        if self.start < self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end

    def label(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}/{self.max_docs}"


_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _parse_hhmm(raw: str, key: str) -> time:
    m = _HHMM.match(raw.strip())
    if not m:
        raise ValueError(f"{key}: expected HH:MM, got {raw!r}")
    return time(int(m.group(1)), int(m.group(2)))


def _split_list(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def parse_offsets(raw: str, key: str = "PROACTIVE_DEADLINE_OFFSETS_DAYS") -> list[int]:
    """`"30,7,1"` → `[30, 7, 1]` (unique, descending, each 0 … 365)."""
    out: list[int] = []
    for part in _split_list(raw):
        if not part.isdigit():
            raise ValueError(f"{key}: {part!r} is not a whole number of days")
        n = int(part)
        if n > 365:
            raise ValueError(f"{key}: {n} days is more than a year")
        out.append(n)
    if not out:
        raise ValueError(f"{key}: at least one offset is required")
    return sorted(set(out), reverse=True)


def parse_windows(raw: str) -> list[SweepWindow]:
    """`"01:00-05:00/200,13:00-14:00/20"` → windows. Empty = no sweep windows."""
    out: list[SweepWindow] = []
    for part in _split_list(raw):
        m = re.match(r"^(\S+)-(\S+)/(\d+)$", part)
        if not m:
            raise ValueError(
                f"PROACTIVE_SWEEP_WINDOWS: expected HH:MM-HH:MM/max_docs, got {part!r}"
            )
        start = _parse_hhmm(m.group(1), "PROACTIVE_SWEEP_WINDOWS")
        end = _parse_hhmm(m.group(2), "PROACTIVE_SWEEP_WINDOWS")
        if start == end:
            raise ValueError(f"PROACTIVE_SWEEP_WINDOWS: {part!r} has no duration")
        out.append(SweepWindow(start, end, int(m.group(3))))
    return out


class ProactiveSettings(BaseSettings):
    # The same .env as config/settings.py; keys of other subsystems are ignored.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    proactive_enabled: bool = False

    # Relative paths resolve against APP_PATH. Default `data/proactive.db`: the
    # `./data` volume both containers mount (diagnostics §8.3).
    proactive_db_path: str = "data/proactive.db"
    # `{user}` = Paperless username. Must lie inside that user's vault, because
    # the ledger relies on the vault's lock, git change detection and (Phase 9)
    # indexing. Empty = `<VAULT_ROOT>/{user}/PaperlessBrain Findings`.
    proactive_findings_root: str = ""
    proactive_app_base_url: str = ""
    proactive_analysis_version: int = 1

    proactive_deadline_offsets_days: str = "30,7,1"
    proactive_daily_run_time: str = "07:00"
    proactive_newdoc_poll_seconds: int = 300

    proactive_wol_auto: bool = True
    proactive_idle_shutdown_minutes: int = 5
    proactive_sweep_windows: str = "01:00-05:00/200"
    proactive_model_fixed: str = ""
    proactive_model_allowlist: str = ""
    proactive_model_default: str = ""
    proactive_cloud_allowed: bool = False
    proactive_cloud_max_docs_per_user_day: int = 20
    proactive_push_threshold: float = 0.5
    proactive_quiet_days: int = 10
    proactive_digest_held_items: int = 2

    # Where the worker reaches the app's internal API (similar documents, deep
    # dives — work that needs Chroma or the chat tools, which stay in the app).
    # Empty = http://127.0.0.1:<PORT>; in Docker: http://paperless-brain:8080.
    proactive_internal_url: str = ""

    # Job runner (not in the spec's .env sketch; sane defaults, rarely changed).
    proactive_job_max_attempts: int = 5
    proactive_job_backoff_seconds: int = 30

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""

    # Web Push (the installed app / browser). Keys: `python -m proactive.notify.webpush`.
    push_vapid_public_key: str = ""
    push_vapid_private_key: str = ""
    push_contact: str = ""  # mailto:… or https://…; empty = PROACTIVE_APP_BASE_URL

    # ── Validation ────────────────────────────────────────────────────────────

    @field_validator("proactive_deadline_offsets_days")
    @classmethod
    def _v_offsets(cls, v: str) -> str:
        parse_offsets(v)
        return v

    @field_validator("proactive_daily_run_time")
    @classmethod
    def _v_daily(cls, v: str) -> str:
        _parse_hhmm(v, "PROACTIVE_DAILY_RUN_TIME")
        return v

    @field_validator("proactive_sweep_windows")
    @classmethod
    def _v_windows(cls, v: str) -> str:
        parse_windows(v)
        return v

    @field_validator("proactive_findings_root")
    @classmethod
    def _v_findings_root(cls, v: str) -> str:
        if v and "{user}" not in v:
            raise ValueError(
                "PROACTIVE_FINDINGS_ROOT must contain {user} — findings belong to "
                "one user and live in that user's vault"
            )
        return v

    @field_validator("proactive_push_threshold")
    @classmethod
    def _v_threshold(cls, v: float) -> float:
        if not 0 <= v <= 2:
            raise ValueError("PROACTIVE_PUSH_THRESHOLD must be between 0 and 2")
        return v

    @model_validator(mode="after")
    def _v_ranges(self) -> "ProactiveSettings":
        positive = {
            "PROACTIVE_NEWDOC_POLL_SECONDS": (self.proactive_newdoc_poll_seconds, 30),
            "PROACTIVE_ANALYSIS_VERSION": (self.proactive_analysis_version, 1),
            "PROACTIVE_JOB_MAX_ATTEMPTS": (self.proactive_job_max_attempts, 1),
            "PROACTIVE_JOB_BACKOFF_SECONDS": (self.proactive_job_backoff_seconds, 1),
        }
        for key, (value, floor) in positive.items():
            if value < floor:
                raise ValueError(f"{key} must be at least {floor}")
        for key, value in {
            "PROACTIVE_CLOUD_MAX_DOCS_PER_USER_DAY": self.proactive_cloud_max_docs_per_user_day,
            "PROACTIVE_QUIET_DAYS": self.proactive_quiet_days,
            "PROACTIVE_DIGEST_HELD_ITEMS": self.proactive_digest_held_items,
        }.items():
            if value < 0:
                raise ValueError(f"{key} must not be negative")
        return self

    # ── Derived values ────────────────────────────────────────────────────────

    @property
    def deadline_offsets(self) -> list[int]:
        return parse_offsets(self.proactive_deadline_offsets_days)

    @property
    def daily_run_time(self) -> time:
        return _parse_hhmm(self.proactive_daily_run_time, "PROACTIVE_DAILY_RUN_TIME")

    @property
    def sweep_windows(self) -> list[SweepWindow]:
        return parse_windows(self.proactive_sweep_windows)

    @property
    def model_allowlist(self) -> list[str]:
        return _split_list(self.proactive_model_allowlist)

    @property
    def email_available(self) -> bool:
        """Send-only: an SMTP server and a sender address."""
        return bool(self.smtp_host and self.smtp_from)

    @property
    def push_available(self) -> bool:
        return bool(self.push_vapid_public_key and self.push_vapid_private_key)

    def db_path(self, app_path: Path) -> Path:
        p = Path(self.proactive_db_path)
        return p if p.is_absolute() else Path(app_path) / p


@lru_cache(maxsize=1)
def proactive_settings() -> ProactiveSettings:
    return ProactiveSettings()


def is_enabled() -> bool:
    return proactive_settings().proactive_enabled
