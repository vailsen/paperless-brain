"""Per-user proactive settings in `proactive.db` (`proactive_prefs`).

Not `services/user_prefs.py`: that store lives in NiceGUI's `app.storage.general`,
which the worker cannot safely read (diagnostics §8.2). Two values from there
are *copied* here when the user saves their proactive settings — language
(notification templates) and chat model (the last step of model resolution) —
because the worker has no other way to learn them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from proactive import clock
from proactive.db import connect

CHANNEL_CHOICES = ("telegram", "email", "both")


@dataclass
class ProactivePrefs:
    user: str
    optin: bool = False
    channels: str = ""  # telegram | email | both | ""
    model: str = ""  # display name in the user's model registry, "" = default
    deadline_offsets: list[int] = field(default_factory=list)  # [] = operator default
    max_pushes_per_week: int | None = None  # None = operator default
    language: str = ""
    chat_model: str = ""

    def channel_list(self) -> list[str]:
        if self.channels == "both":
            return ["telegram", "email"]
        return [self.channels] if self.channels in ("telegram", "email") else []


def _from_row(r) -> ProactivePrefs:
    offsets = [int(x) for x in (r["deadline_offsets"] or "").split(",") if x.strip().isdigit()]
    return ProactivePrefs(
        user=r["user"],
        optin=bool(r["proactive_optin"]),
        channels=r["channels"] or "",
        model=r["model"] or "",
        deadline_offsets=offsets,
        max_pushes_per_week=r["max_pushes_per_week"],
        language=r["language"] or "",
        chat_model=r["chat_model"] or "",
    )


def get(user: str) -> ProactivePrefs:
    with connect() as conn:
        r = conn.execute("SELECT * FROM proactive_prefs WHERE user = ?", (user,)).fetchone()
    return _from_row(r) if r else ProactivePrefs(user=user)


def save(p: ProactivePrefs) -> None:
    if p.channels and p.channels not in CHANNEL_CHOICES:
        raise ValueError(f"unknown channel choice {p.channels!r}")
    with connect() as conn:
        conn.execute(
            "INSERT INTO proactive_prefs (user, proactive_optin, channels, model,"
            " deadline_offsets, max_pushes_per_week, language, chat_model, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(user) DO UPDATE SET proactive_optin = excluded.proactive_optin,"
            " channels = excluded.channels, model = excluded.model,"
            " deadline_offsets = excluded.deadline_offsets,"
            " max_pushes_per_week = excluded.max_pushes_per_week,"
            " language = excluded.language, chat_model = excluded.chat_model,"
            " updated_at = excluded.updated_at",
            (
                p.user,
                int(p.optin),
                p.channels,
                p.model,
                ",".join(str(n) for n in p.deadline_offsets),
                p.max_pushes_per_week,
                p.language,
                p.chat_model,
                clock.now_iso(),
            ),
        )


def opted_in_users() -> list[str]:
    """Users who opted in. A user without an active background-access grant is
    still listed; jobs skip them via `background_access.token_for()`."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT user FROM proactive_prefs WHERE proactive_optin = 1 ORDER BY user"
        ).fetchall()
    return [r["user"] for r in rows]
