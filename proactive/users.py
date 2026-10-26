"""Who the worker may act for, and with which token.

A user is eligible when they opted in *and* their background-access grant is
active. The token comes from `background_access.token_for()` and nowhere else;
a 401 from Paperless marks the grant `needs_reauth` (the user renews it by
logging in), after which the user is skipped instead of retried.
"""

from __future__ import annotations

import logging

from services import background_access

log = logging.getLogger("proactive.users")


def token(user: str) -> str | None:
    return background_access.token_for(user)


def eligible() -> list[tuple[str, str]]:
    """`[(user, token)]` for every opted-in user with an active grant."""
    from proactive import prefs

    out = []
    for user in prefs.opted_in_users():
        tok = token(user)
        if tok:
            out.append((user, tok))
    return out


def token_rejected(user: str) -> None:
    log.warning("Paperless rejected the stored token of %s — skipping them until they sign in again", user)
    background_access.mark_needs_reauth(user)
