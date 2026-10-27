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

CHANNELS = ("push", "telegram", "email")


@dataclass
class ProactivePrefs:
    user: str
    optin: bool = False
    channels: str = ""  # comma list of CHANNELS; "both" (older) = telegram,email
    model: str = ""  # display name in the user's model registry, "" = default
    deadline_offsets: list[int] = field(default_factory=list)  # [] = operator default
    max_pushes_per_week: int | None = None  # None = operator default
    language: str = ""
    chat_model: str = ""
    notify_sync: bool = True  # push only: "sync finished"
    notify_werkbank: bool = True  # push only: "Werkbank run done"
    findings_language: str = ""  # "" = `language` (the app's)
    quiet_start: str = "22:00"  # HH:MM local; start == end = no quiet hours
    quiet_end: str = "07:00"

    @property
    def content_language(self) -> str:
        """What findings, dossiers and their messages are written in."""
        return self.findings_language or self.language or "en"

    def channel_list(self) -> list[str]:
        raw = "telegram,email" if self.channels == "both" else self.channels
        chosen = {c.strip() for c in raw.split(",")}
        return [c for c in CHANNELS if c in chosen]


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
        notify_sync=bool(r["notify_sync"]),
        notify_werkbank=bool(r["notify_werkbank"]),
        findings_language=r["findings_language"] or "",
        quiet_start=r["quiet_start"] or "22:00",
        quiet_end=r["quiet_end"] or "07:00",
    )


def get(user: str) -> ProactivePrefs:
    with connect() as conn:
        r = conn.execute("SELECT * FROM proactive_prefs WHERE user = ?", (user,)).fetchone()
    return _from_row(r) if r else ProactivePrefs(user=user)


def save(p: ProactivePrefs) -> None:
    for hhmm in (p.quiet_start, p.quiet_end):
        h, _, m = hhmm.partition(":")
        if not (h.isdigit() and m.isdigit() and int(h) < 24 and int(m) < 60):
            raise ValueError(f"not a time: {hhmm!r}")
    unknown = {c.strip() for c in p.channels.split(",") if c.strip()} - set(CHANNELS) - {"both"}
    if unknown:
        raise ValueError(f"unknown channel choice {p.channels!r}")
    if p.max_pushes_per_week is not None:
        from config.proactive import proactive_settings

        # Clamped, not refused: the operator may lower the ceiling after a user
        # saved a higher value, and saving any other setting must still work.
        ceiling = proactive_settings().proactive_max_pushes_per_week_limit
        p.max_pushes_per_week = max(0, min(int(p.max_pushes_per_week), ceiling))
    with connect() as conn:
        conn.execute(
            "INSERT INTO proactive_prefs (user, proactive_optin, channels, model,"
            " deadline_offsets, max_pushes_per_week, language, chat_model,"
            " notify_sync, notify_werkbank, findings_language, quiet_start, quiet_end, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(user) DO UPDATE SET proactive_optin = excluded.proactive_optin,"
            " channels = excluded.channels, model = excluded.model,"
            " deadline_offsets = excluded.deadline_offsets,"
            " max_pushes_per_week = excluded.max_pushes_per_week,"
            " language = excluded.language, chat_model = excluded.chat_model,"
            " notify_sync = excluded.notify_sync, notify_werkbank = excluded.notify_werkbank,"
            " findings_language = excluded.findings_language,"
            " quiet_start = excluded.quiet_start, quiet_end = excluded.quiet_end,"
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
                int(p.notify_sync),
                int(p.notify_werkbank),
                p.findings_language,
                p.quiet_start,
                p.quiet_end,
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
