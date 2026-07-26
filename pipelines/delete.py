# pipelines/delete.py
from services.chroma import ChromaClient
from services.sidecar_service import SidecarService
from services.thumbnail_service import ThumbnailService


async def delete_document(
    doc_id: int,
    chroma: ChromaClient,
    sidecar_service: SidecarService,
    thumbnail_service: ThumbnailService,
) -> None:
    """Remove all chunks and sidecar for a deleted Paperless document."""

    from services.clients import cross_ref_index as _idx
    _idx.remove_document(doc_id)

    await chroma.delete(where={"paperless_id": {"$eq": doc_id}})
    sidecar_service.delete_sidecar(doc_id)
    thumbnail_service.delete_thumbnail(doc_id)
