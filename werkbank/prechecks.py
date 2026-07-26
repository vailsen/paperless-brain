"""werkbank/prechecks.py — deterministic sanity checks on raw Worker output.

Run BEFORE the Critic. These are cheap, non-LLM checks that catch obviously
broken results (empty, too short, too long) so the Critic isn't wasted on junk.
"""

from __future__ import annotations

import re
from pathlib import Path

_MIN_LEN = 30
_MAX_LEN = 50_000
_DOC_REF_RE = re.compile(r"#(\d+)")


def _existing_doc_ids() -> set[int] | None:
    """Doc IDs with a sidecar on disk, or None if the index is unavailable."""
    try:
        from config.settings import settings

        sidecar_dir = Path(settings.app_path) / settings.extraction_sidecar_path
        return {int(f.stem) for f in sidecar_dir.glob("*.json") if f.stem.isdigit()}
    except Exception:
        return None


class PrecheckError(Exception):
    pass


_FOUND_NOTHING_PHRASES = (
    "keine dokumente",
    "nicht gefunden",
    "kein ergebnis",
    "keine ergebnisse",
    "0 dokument",
    "nichts gefunden",
    "no results",
    "not found",
    "no documents",
    "nothing found",
    "no result",
)


def run(raw_result: str, archetype_name: str = "") -> None:
    """Raise PrecheckError if raw_result fails basic sanity checks.

    Args:
        raw_result:     The Worker's raw text output.
        archetype_name: Optional archetype name for archetype-specific checks.
    """
    if not raw_result or not raw_result.strip():
        raise PrecheckError("Ergebnis ist leer.")

    stripped = raw_result.strip()
    lower    = stripped.lower()

    # Retriever check runs first: explicit "nothing found" is a valid short result.
    if archetype_name == "retriever":
        found_nothing = any(phrase in lower for phrase in _FOUND_NOTHING_PHRASES)
        if found_nothing:
            return  # valid — no further checks needed
        cited = {int(m) for m in _DOC_REF_RE.findall(stripped)}
        if not cited:
            raise PrecheckError(
                "Retriever result contains no document reference (#ID) "
                "and no explicit 'not found' notice."
            )
        # Hallucination gate: at least one cited #ID must exist in the archive.
        # (Not all — "#4711" may be an invoice number quoted from a document,
        # so partial mismatches are legitimate.)
        existing = _existing_doc_ids()
        if existing is not None and cited.isdisjoint(existing):
            shown = ", ".join(f"#{i}" for i in sorted(cited)[:10])
            raise PrecheckError(
                f"Keine der zitierten Dokument-IDs existiert im Archiv ({shown}). "
                f"Nur IDs aus echten Suchergebnissen zitieren."
            )

    if len(stripped) < _MIN_LEN:
        raise PrecheckError(
            f"Ergebnis zu kurz ({len(stripped)} Zeichen, mindestens {_MIN_LEN} erwartet)."
        )

    if len(raw_result) > _MAX_LEN:
        raise PrecheckError(
            f"Ergebnis zu lang ({len(raw_result)} Zeichen, max. {_MAX_LEN})."
        )
