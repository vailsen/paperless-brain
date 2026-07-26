# services/thumbnail_service.py
import os

from services.paperless import PaperlessClient


class ThumbnailService:
    """Provides all services regarding thumbnails."""

    def __init__(self, paperless_client: PaperlessClient, thumbnail_path: str):
        self.thumb_path = thumbnail_path
        self.paperless = paperless_client
        os.makedirs(thumbnail_path, exist_ok=True)

    def save_thumbnail(self, thumbnail: bytes, doc_id: int) -> None:
        """Save the thumbnail to the thumbnail location"""
        path = f"{self.thumb_path}/{doc_id}.jpg"
        try:
            with open(path, "wb") as f:
                f.write(thumbnail)
        except OSError as e:
            raise RuntimeError(
                f"Failed to save thumbnail for document {doc_id}: {e}"
            ) from e

    def delete_thumbnail(self, doc_id: int) -> None:
        """Delete a thumbnail by doc_id"""
        path = f"{self.thumb_path}/{doc_id}.jpg"
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            raise RuntimeError(
                f"Failed to delete thumbnail for document {doc_id}: {e}"
            ) from e

    async def get_thumbnail(self, doc_id: int) -> bytes:
        """Create or update index.json with all actions from all sidecars."""
        return await self.paperless.get_thumbnail(doc_id)
