# services/sidecar_service.py
import json
import os

from models.extraction import JsonSidecar


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
        """
        out: list[int] = []
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
            if str(data.get("prompt_version", "")) != str(current_version):
                out.append(int(pid))
        return out

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
