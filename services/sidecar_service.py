# services/sidecar_service.py
import json
import os
import re

from models.extraction import JsonSidecar

# The inline picture marker of prompt versions < 0.3, in the three spellings the
# model actually produced on the live archive. Used only to decide what a
# re-ingest must redo; nothing reads pictures from page text any more.
_OLD_PICTURE_MARKER_RE = re.compile(
    r"\[\s*(image description|bildbeschreibung|abbildung)\s*:", re.IGNORECASE
)


def _mentions_pictures(sidecar: dict) -> bool:
    for page in sidecar.get("pages") or []:
        if page.get("images") or _OLD_PICTURE_MARKER_RE.search(page.get("page_text") or ""):
            return True
    return False


# What a page the model read before 0.4 shows when it held a formula: the model
# wrote it in Unicode. Greek letters (µ excepted, it is a unit), the operators
# that only occur in mathematics, and a one-letter variable set equal to
# something ("x = 2a + b"). "m²", "±" and "×" are left out on purpose: every
# energy certificate and invoice has them.
_MATH_RE = re.compile(
    r"[α-λν-ωΓΔΘΛΞΠΣΦΨΩ∑∏∫∮√∂∇∞∝≈≠≤≥]|(?<![\w.])[A-Za-z]\s*=\s*[-A-Za-z0-9(√]"
)


def _mentions_math(sidecar: dict) -> bool:
    return any(
        _MATH_RE.search(page.get("page_text") or "")
        for page in sidecar.get("pages") or []
    )


# Prompt versions in order, and the steps between them whose change concerns
# only some documents: step → test whether a sidecar of the older version has
# anything that step is about. A sidecar is outdated as soon as one step on its
# way to the current version concerns it — or a step has no test (a change for
# every document). Without this, each such step would re-run the vision model
# over the whole archive: 0.2 → 0.3 touched 9 of 523 documents on the live one.
# Add a test only when nothing else in the prompt changed in that step.
_VERSIONS: list[str] = ["0.2", "0.3", "0.4"]
_SCOPED_CHANGES: dict[tuple[str, str], object] = {
    ("0.2", "0.3"): _mentions_pictures,  # pictures in "images", not inline
    ("0.3", "0.4"): _mentions_math,  # formulas as LaTeX
}


def is_outdated(sidecar: dict, current_version: str) -> bool:
    """Whether a re-ingest with the current prompt would change this sidecar."""
    version = str(sidecar.get("prompt_version", ""))
    current = str(current_version)
    if version == current:
        return False
    if version not in _VERSIONS or current not in _VERSIONS:
        return True
    i, j = _VERSIONS.index(version), _VERSIONS.index(current)
    if i > j:
        return True
    for step in zip(_VERSIONS[i:j], _VERSIONS[i + 1 : j + 1]):
        test = _SCOPED_CHANGES.get(step)
        if test is None or test(sidecar):
            return True
    return False


# Paperless takes the id filter as one comma-joined query param; a whole archive's
# worth of ids in a single URL gets rejected by the proxy long before Paperless
# sees it. 200 ids ≈ 1.4 kB, comfortably under any default limit.
_ID_BATCH = 200


async def filter_visible_actions(actions: list[dict], paperless_client) -> list[dict]:
    """Keep only the actions whose source document the caller may read.

    index.json is built by the superuser sync and therefore aggregates the
    actions of every document in the archive, across all owners. The action
    descriptions are extracted document content, so they need the same
    permission check as the documents — done here by re-fetching the referenced
    ids through the caller's own Paperless client and keeping what comes back.

    Fails closed: any error drops the affected batch rather than showing it.
    Actions without a paperless_id (manually created) are not index.json's and
    are left to the caller.
    """
    ids = list({a["paperless_id"] for a in actions if a.get("paperless_id")})
    if not ids:
        return list(actions)

    visible: set[int] = set()
    for start in range(0, len(ids), _ID_BATCH):
        batch = ids[start : start + _ID_BATCH]
        try:
            docs = await paperless_client.list_documents(ids=batch)
        except Exception:
            continue  # batch stays invisible
        visible.update(d.id for d in docs)

    return [a for a in actions if a.get("paperless_id") in visible]


class SidecarService:
    """Provides all services regarding the json sidecar file management alongside pdf extraction."""

    def __init__(self, extraction_sidecar_path: str):
        self.extr_path = extraction_sidecar_path
        os.makedirs(extraction_sidecar_path, exist_ok=True)

    def save_sidecar(self, sidecar: JsonSidecar) -> None:
        """Save the sidecar to the extraction location"""
        path = f"{self.extr_path}/{sidecar.paperless_id}.json"
        try:
            with open(path, "w") as f:
                f.write(sidecar.model_dump_json(indent=2))
        except OSError as e:
            raise RuntimeError(
                f"Failed to save sidecar for document {sidecar.paperless_id}: {e}"
            ) from e

    def delete_sidecar(self, paperless_id: int) -> None:
        """Delete a sidecar by paperless_id"""
        path = f"{self.extr_path}/{paperless_id}.json"
        try:
            os.remove(path)
        except FileNotFoundError:
            pass  # already gone, that's fine
        except OSError as e:
            raise RuntimeError(
                f"Failed to delete sidecar for document {paperless_id}: {e}"
            ) from e

    def load_sidecar(self, paperless_id: int) -> dict | None:
        """Return the raw sidecar dict, or None if not found / invalid."""
        path = f"{self.extr_path}/{paperless_id}.json"
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def has_actions(self, paperless_id: int) -> bool:
        """Return True if the sidecar for this document has any actions."""
        path = f"{self.extr_path}/{paperless_id}.json"
        try:
            with open(path) as f:
                data = json.load(f)
            return bool(data.get("actions"))
        except (OSError, json.JSONDecodeError):
            return False

    def outdated_ids(self, current_version: str) -> list[int]:
        """Return doc_ids whose sidecar prompt_version differs from current_version.

        Used by the dashboard to show how far ingestion is behind the code's
        extraction prompt and to drive a re-ingest of stale documents.

        "Differs" in the sense of ``is_outdated``: a version step that only
        changed picture handling leaves picture-less documents alone.

        Ordered by what a capped re-ingest should do first: documents whose old
        extraction mentions pictures, then the newest documents.
        """
        out: list[tuple[int, int]] = []
        for filename in os.listdir(self.extr_path):
            if filename == "index.json" or not filename.endswith(".json"):
                continue
            try:
                with open(f"{self.extr_path}/{filename}") as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            pid = data.get("paperless_id")
            if pid is None:
                continue
            if is_outdated(data, current_version):
                out.append((0 if _mentions_pictures(data) else 1, -int(pid)))
        return [-neg_pid for _prio, neg_pid in sorted(out)]

    def create_index_file(self) -> None:
        """Create or update index.json with all actions from all sidecars."""
        # Dedupe per doc (multi-page repeats incl. paraphrases) so old
        # sidecars written before dedupe existed stay clean in the index.
        from services.action_dedupe import dedupe_actions
        from services.action_review import filter_actions

        all_actions = []
        all_cross_refs = []
        for filename in os.listdir(self.extr_path):
            if filename == "index.json" or not filename.endswith(".json"):
                continue
            try:
                with open(f"{self.extr_path}/{filename}") as f:
                    data = json.load(f)
                paperless_id = data.get("paperless_id")
                for action in dedupe_actions(data.get("actions", [])):
                    all_actions.append({"paperless_id": paperless_id, **action})
                for cross_ref in data.get("cross_refs") or []:
                    all_cross_refs.append({"paperless_id": paperless_id, **cross_ref})
            except (OSError, json.JSONDecodeError):
                continue

        # Post-sync LLM review verdicts: junk/duplicate deadlines stay in the
        # sidecars (ground truth) but never reach the index.
        all_actions = filter_actions(self.extr_path, all_actions)

        index_path = f"{self.extr_path}/index.json"
        try:
            with open(index_path, "w") as f:
                json.dump(
                    {"actions": all_actions, "cross_refs": all_cross_refs},
                    f,
                    indent=2,
                    ensure_ascii=False,
                )
        except OSError as e:
            raise RuntimeError(f"Failed to write index file: {e}") from e
