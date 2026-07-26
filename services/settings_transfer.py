# services/settings_transfer.py
"""Export / import of the user-scoped settings as a portable JSON string.

Covers exactly what the settings page lets a user change *for themselves*:
language and theme (``app.storage.user``) plus the encrypted credential blob
(LLM model registry, IMAP, calendar, sender profile, dream model).

Deliberately **not** covered: the werkbank settings store. Those keys are global
— they apply to every user of the instance — so moving them around inside a
per-user export string would let one user silently reconfigure everyone else.

Secrets (API keys, passwords, iCal URLs) are replaced by :data:`REDACTED` unless
the caller explicitly asks for them. An import merges rather than replaces: a
redacted field keeps whatever is already stored, so a secret-free export is
still a complete config transfer for everything else.
"""

from __future__ import annotations

import json
from typing import Any

from services.credential_store import load_credentials, save_credentials

APP_ID = "paperless-brain"
SCHEMA_VERSION = 1

#: Placeholder written in place of a secret, and recognised again on import.
REDACTED = "__OMITTED__"

#: Credential keys carried by the export, in the order they are written.
_CRED_KEYS = ("llm_models", "imap", "calendar", "sender_profile", "dream_model")

_VALID_THEMES = ("dark", "light")


# ── Export ────────────────────────────────────────────────────────────────────


def _redact_models(models: list[dict]) -> list[dict]:
    out = []
    for m in models:
        m = dict(m)
        if m.get("api_key"):
            m["api_key"] = REDACTED
        out.append(m)
    return out


def _redact_calendar(cal: dict) -> dict:
    cal = dict(cal)
    if cal.get("password"):
        cal["password"] = REDACTED
    # An iCal URL carries a secret token in the path — the whole URL is a secret.
    if cal.get("ical_urls"):
        cal["ical_urls"] = [REDACTED if u else u for u in cal["ical_urls"]]
    if cal.get("ical_url"):
        cal["ical_url"] = REDACTED
    return cal


def _redact(creds: dict) -> dict:
    out = dict(creds)
    if out.get("llm_models"):
        out["llm_models"] = _redact_models(out["llm_models"])
    if out.get("imap", {}).get("password"):
        out["imap"] = {**out["imap"], "password": REDACTED}
    if out.get("calendar"):
        out["calendar"] = _redact_calendar(out["calendar"])
    return out


def build_export(
    username: str,
    token: str,
    *,
    language: str,
    theme: str,
    include_secrets: bool = False,
) -> str:
    """Return the settings of ``username`` as an indented JSON string."""
    creds = load_credentials(username, token) if username and token else {}
    payload: dict[str, Any] = {
        "_app": APP_ID,
        "_schema": SCHEMA_VERSION,
        "_secrets": bool(include_secrets),
        "language": language,
        "theme": theme,
    }
    for key in _CRED_KEYS:
        if key in creds:
            payload[key] = creds[key]
    if not include_secrets:
        payload.update(_redact({k: payload[k] for k in _CRED_KEYS if k in payload}))
    return json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False)


def export_contains_secrets(text: str) -> bool:
    """Whether ``text`` was exported with secrets included (best effort)."""
    try:
        return bool(json.loads(text).get("_secrets"))
    except (ValueError, AttributeError):
        return False


# ── Import ────────────────────────────────────────────────────────────────────


class SettingsImportError(ValueError):
    """Raised when the pasted string is not a usable export."""


def parse_export(text: str) -> dict:
    """Validate ``text`` and return the payload. Raises :class:`SettingsImportError`."""
    text = (text or "").strip()
    if not text:
        raise SettingsImportError("Empty input")
    try:
        payload = json.loads(text)
    except ValueError as e:
        raise SettingsImportError(f"Not valid JSON: {e}") from e
    if not isinstance(payload, dict):
        raise SettingsImportError("Expected a JSON object")
    if payload.get("_app") != APP_ID:
        raise SettingsImportError("Not a PaperlessBrain settings export")
    schema = payload.get("_schema")
    if not isinstance(schema, int) or schema > SCHEMA_VERSION:
        raise SettingsImportError(f"Unsupported export version: {schema}")
    return payload


def describe(payload: dict, supported_languages: tuple[str, ...] | list[str]) -> list[str]:
    """Human-readable list of what :func:`apply_import` would change."""
    notes: list[str] = []
    if payload.get("language") in supported_languages:
        notes.append(f"Language: {payload['language']}")
    if payload.get("theme") in _VALID_THEMES:
        notes.append(f"Theme: {payload['theme']}")
    if isinstance(payload.get("llm_models"), list):
        notes.append(f"AI models: {len(payload['llm_models'])}")
    if isinstance(payload.get("imap"), dict):
        notes.append("Email (IMAP)")
    if isinstance(payload.get("calendar"), dict):
        notes.append("Calendar")
    if isinstance(payload.get("sender_profile"), dict):
        notes.append("Sender profile")
    if payload.get("dream_model"):
        notes.append(f"Memory maintenance model: {payload['dream_model']}")
    return notes


def _merge_models(new: list, existing: list) -> list:
    """Carry redacted API keys over from the models already stored, by name."""
    by_name = {m.get("name"): m for m in existing if isinstance(m, dict)}
    out = []
    for m in new:
        if not isinstance(m, dict):
            continue
        m = dict(m)
        if m.get("api_key") == REDACTED:
            m["api_key"] = by_name.get(m.get("name"), {}).get("api_key", "")
        out.append(m)
    return out


def _merge_imap(new: dict, existing: dict) -> dict:
    new = dict(new)
    if new.get("password") == REDACTED:
        new["password"] = existing.get("password", "")
    return new


def _merge_calendar(new: dict, existing: dict) -> dict:
    new = dict(new)
    if new.get("password") == REDACTED:
        new["password"] = existing.get("password", "")
    old_urls = existing.get("ical_urls") or ([existing["ical_url"]] if existing.get("ical_url") else [])
    if isinstance(new.get("ical_urls"), list):
        new["ical_urls"] = [
            (old_urls[i] if i < len(old_urls) else "") if u == REDACTED else u
            for i, u in enumerate(new["ical_urls"])
        ]
        new["ical_urls"] = [u for u in new["ical_urls"] if u]
    if new.get("ical_url") == REDACTED:
        new["ical_url"] = old_urls[0] if old_urls else ""
    return new


def apply_import(
    username: str,
    token: str,
    payload: dict,
    supported_languages: tuple[str, ...] | list[str],
) -> dict[str, str]:
    """Write the credential part of ``payload``.

    Returns the UI preferences the caller must write to ``app.storage.user``
    (``language`` / ``theme``) — this module has no session context of its own.
    """
    creds = load_credentials(username, token) if username and token else {}

    if isinstance(payload.get("llm_models"), list):
        creds["llm_models"] = _merge_models(payload["llm_models"], creds.get("llm_models") or [])
    if isinstance(payload.get("imap"), dict):
        creds["imap"] = _merge_imap(payload["imap"], creds.get("imap") or {})
    if isinstance(payload.get("calendar"), dict):
        creds["calendar"] = _merge_calendar(payload["calendar"], creds.get("calendar") or {})
    if isinstance(payload.get("sender_profile"), dict):
        creds["sender_profile"] = payload["sender_profile"]
    if isinstance(payload.get("dream_model"), str):
        creds["dream_model"] = payload["dream_model"]

    if username and token:
        save_credentials(username, token, creds)

    prefs: dict[str, str] = {}
    if payload.get("language") in supported_languages:
        prefs["language"] = payload["language"]
    if payload.get("theme") in _VALID_THEMES:
        prefs["theme"] = payload["theme"]
    return prefs
