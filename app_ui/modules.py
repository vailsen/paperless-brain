"""The modules of the app — one list for everything that names them.

The header, the phone's module menu, the help dialog, the start page and the
"Modules" switches in Settings all read `visible_modules()`. A module the user
hides is out of the way, not off: it leaves every menu, but its address still
opens (push links, citations, bookmarks) with a banner offering to show it
again, and nothing it does in the background stops.

Hiding is about the module's page only: what the module offers inside other
pages stays (experts in the chat's picker, "Deep research from this chat").
Chat and dashboard cannot be hidden.
"""

from __future__ import annotations

from dataclasses import dataclass

from i18n import N_


@dataclass(frozen=True)
class Module:
    key: str
    icon: str
    label: str  # N_() msgid, translated at render
    path: str
    optional: bool = True


MODULES: tuple[Module, ...] = (
    Module("dashboard", "dashboard", N_("Dashboard"), "/", optional=False),
    Module("chat", "chat", N_("Chat"), "/chat", optional=False),
    Module("findings", "lightbulb", N_("Findings"), "/findings"),
    Module("vault", "note_alt", N_("Note vault"), "/brain"),
    Module("research", "auto_awesome", N_("Deep research"), "/werkbank"),
    Module("experts", "psychology", N_("Experts"), "/experts"),
    Module("browser", "grid_view", N_("Browser"), "/browser"),
)
BY_KEY = {m.key: m for m in MODULES}
OPTIONAL = tuple(m.key for m in MODULES if m.optional)

#: Start page choice "the module used last" (`start_target`).
LAST = "last"


def available(m: Module) -> bool:
    """Installed at all — findings only with PROACTIVE_ENABLED."""
    if m.key == "findings":
        from config.proactive import is_enabled

        return is_enabled()
    return True


def hidden() -> set[str]:
    """The modules this user hid. Needs a page context.

    Before this list existed, Experts had a switch of its own
    (`experts_enabled`); a user who had turned it off has the Experts page
    hidden."""
    from services.user_prefs import get_pref

    raw = get_pref("hidden_modules", None)
    if raw is None:
        return set() if get_pref("experts_enabled", True) else {"experts"}
    return {k for k in raw if k in OPTIONAL}


def set_hidden(keys) -> None:
    from services.user_prefs import set_pref

    clean = sorted(k for k in set(keys) if k in OPTIONAL)
    set_pref("hidden_modules", clean)
    # Kept in step so the migration in `hidden()` never reads a stale value.
    set_pref("experts_enabled", "experts" not in clean)


def is_hidden(key: str) -> bool:
    try:
        return key in hidden()
    except Exception:  # noqa: BLE001 — no page context (tests, a worker): nothing hidden
        return False


def visible_modules() -> list[Module]:
    try:
        off = hidden()
    except Exception:  # noqa: BLE001 — outside a page: the full list
        off = set()
    return [m for m in MODULES if available(m) and m.key not in off]


def module_for_path(path: str) -> Module | None:
    """The module a page belongs to (`/findings/abc` → findings), or None for
    pages outside the modules (settings, admin, login)."""
    path = path or ""
    for m in MODULES:
        if m.path == "/":
            if path == "/":
                return m
        elif path == m.path or path.startswith(m.path + "/"):
            return m
    return None


def start_target(choice: str, last: str) -> str:
    """Where `/start` (the installed app, a fresh login) leads.

    `choice` is the user's start page (a module key or `LAST`), `last` the
    module they used last. Anything hidden, unavailable or unknown falls back
    to the dashboard."""
    key = last if choice == LAST else choice
    m = BY_KEY.get(key or "")
    if m is None or not available(m) or is_hidden(m.key):
        return "/"
    return m.path
