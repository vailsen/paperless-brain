# services/cross_ref_index.py
"""Inverted index over sidecar cross-references.

Built once at startup; rebuilt after each sync.  All operations are
synchronous and fast (pure dict lookups after build).

Value normalisation: lower-cased, collapsed whitespace.
Trivial-value filter: bare years (1900-2099), numbers ≤ 4 digits, strings
shorter than 4 characters — these appear in almost every document and carry
no discriminating power.

Related-document matching requires the (normalised) ref *type* to match too:
a shared value alone connects unrelated documents when the value is e.g. a
person's name ("Hausverwaltung: Max Mustermann" must not link to
"Patientenname: Max Mustermann"). Type labels come from the same
extraction prompt and are fairly consistent; normalisation collapses spelling
variants like "Rechnungs-Nr." / "Rechnungsnummer".
"""

import json
import os
import re
from collections import defaultdict

_TRIVIAL_RE = re.compile(r"^(\d{1,4}|(19|20)\d{2})$")


def _is_trivial(value: str) -> bool:
    v = " ".join(value.split())  # normalise whitespace
    if len(v) < 4:
        return True
    if _TRIVIAL_RE.match(v):
        return True
    return False


def _normalise(value: str) -> str:
    return " ".join(value.lower().split())


# Observed label synonyms the suffix rule cannot collapse.
_TYPE_ALIASES = {
    "idnummer": "identifikationsnummer",
    "mandatsreferenznummer": "mandatsreferenz",
    "gläubigerid": "gläubigeridentifikationsnummer",
}


def _normalise_type(type_label: str) -> str:
    """Collapse type-label variants: "Rechnungs-Nr." == "rechnungsnummer"."""
    t = re.sub(r"[^a-z0-9äöüß]", "", (type_label or "").lower())
    if t.endswith("nr"):
        t = t[:-2] + "nummer"
    return _TYPE_ALIASES.get(t, t)


class CrossRefIndex:
    def __init__(self) -> None:
        # normalised value → set of (doc_id, normalised type) carrying this value
        self._index: dict[str, set[tuple[int, str]]] = defaultdict(set)
        # doc_id → raw cross_refs list from sidecar
        self._doc_refs: dict[int, list[dict]] = {}

    # ── Build ─────────────────────────────────────────────────────────────────

    def build(self, sidecar_dir: str) -> None:
        """Scan all sidecars and populate the index."""
        index: dict[str, set[tuple[int, str]]] = defaultdict(set)
        doc_refs: dict[int, list[dict]] = {}

        for filename in os.listdir(sidecar_dir):
            if filename == "index.json" or not filename.endswith(".json"):
                continue
            path = os.path.join(sidecar_dir, filename)
            try:
                with open(path) as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue

            doc_id = data.get("paperless_id")
            if not isinstance(doc_id, int):
                continue

            refs = data.get("cross_refs") or []
            doc_refs[doc_id] = refs
            for ref in refs:
                val = (ref.get("value") or "").strip()
                if val and not _is_trivial(val):
                    index[_normalise(val)].add(
                        (doc_id, _normalise_type(ref.get("type", "")))
                    )

        self._index = index
        self._doc_refs = doc_refs

    def rebuild(self, sidecar_dir: str) -> None:
        """Alias for build — call after ingesting new documents."""
        self.build(sidecar_dir)

    # ── Query ─────────────────────────────────────────────────────────────────

    def get_related(self, doc_id: int) -> list[dict]:
        """Return enriched cross-ref list for *doc_id*.

        Each item: ``{"type": str, "value": str, "matching_ids": list[int]}``
        where ``matching_ids`` are other docs sharing the same reference value
        AND the same (normalised) reference type.
        Items whose value is trivial or has no matches are included but have an
        empty ``matching_ids`` list (so the dialog can still display raw refs).
        """
        refs = self._doc_refs.get(doc_id, [])
        result = []
        for ref in refs:
            val = (ref.get("value") or "").strip()
            if not val:
                continue
            if _is_trivial(val):
                matching: list[int] = []
            else:
                ref_type = _normalise_type(ref.get("type", ""))
                matching = sorted(
                    {
                        d
                        for d, t in self._index.get(_normalise(val), set())
                        if d != doc_id and t == ref_type
                    }
                )
            result.append(
                {
                    "type": ref.get("type", ""),
                    "value": val,
                    "matching_ids": matching,
                }
            )
        return result

    def find_by_value(self, value: str) -> set[int]:
        """Return doc_ids carrying a cross-ref whose value matches *value*.

        Value-based lookup (not doc-based): used by the analytic search tool to
        resolve a literal reference (Aktenzeichen, Rechnungsnummer, …) to every
        document that shares it. Trivial values (bare years, <4 chars) are not
        indexed, so they return an empty set.
        """
        if not value or not value.strip():
            return set()
        return {d for d, _t in self._index.get(_normalise(value), set())}

    def has_related(self, doc_id: int) -> bool:
        """Quick check: does *doc_id* have any non-trivial cross-refs with matches?"""
        for r in self.get_related(doc_id):
            if r["matching_ids"]:
                return True
        return False

    # ── Incremental update ────────────────────────────────────────────────────

    def add_document(self, doc_id: int, refs: list[dict]) -> None:
        """Incrementally add or replace one document's entries in the index."""
        self.remove_document(doc_id)
        self._doc_refs[doc_id] = list(refs)
        for ref in refs:
            val = (ref.get("value") or "").strip()
            if val and not _is_trivial(val):
                self._index[_normalise(val)].add(
                    (doc_id, _normalise_type(ref.get("type", "")))
                )

    def remove_document(self, doc_id: int) -> None:
        """Remove a document from the index (call before deleting its sidecar)."""
        old_refs = self._doc_refs.pop(doc_id, [])
        for ref in old_refs:
            val = (ref.get("value") or "").strip()
            if val and not _is_trivial(val):
                key = _normalise(val)
                s = self._index.get(key)
                if s is not None:
                    s.discard((doc_id, _normalise_type(ref.get("type", ""))))
                    if not s:
                        del self._index[key]
