# pipelines/paperless_db_sync.py
from pydantic import BaseModel

from pipelines.delete import delete_document
from werkbank.settings_store import get_no_ingest_tag
from pipelines.ingest import ingest_document
from services.chroma import ChromaClient
from services.paperless import PaperlessClient
from services.sidecar_service import SidecarService
from services.thumbnail_service import ThumbnailService
from services.vision import VisionClient


class SyncResult(BaseModel):
    new_ids: list[int]  # in Paperless, not in Chroma → need ingestion
    deleted_ids: list[int]  # in Chroma, not in Paperless → need removal
    current_ids: list[int]  # in both → nothing to do


async def check_sync_state(
    paperless: PaperlessClient,
    chroma: ChromaClient,
) -> SyncResult:
    """Compare Paperless and Chroma, return what's out of sync."""

    paperless_docs = await paperless.list_documents()
    paperless_ids = set(
        [
            el.id
            for el in paperless_docs
            if get_no_ingest_tag() not in el.tags
        ]
    )
    get_result_chroma = await chroma.get(include=["metadatas"])
    chroma_paperless_ids = {
        doc["metadata"]["paperless_id"] for doc in get_result_chroma
    }

    return SyncResult(
        new_ids=sorted(paperless_ids - chroma_paperless_ids),
        deleted_ids=sorted(chroma_paperless_ids - paperless_ids),
        current_ids=sorted(paperless_ids & chroma_paperless_ids),
    )


async def run_sync(
    paperless: PaperlessClient,
    chroma: ChromaClient,
    vision: VisionClient,
    sidecar_service: SidecarService,
    thumbnail_service: ThumbnailService,
    llm=None,
) -> SyncResult:
    """Full sync: check state, ingest new, remove deleted.

    When an llm callable (werkbank.llm_lane.create_llm result) is given, the
    aggregated actions/deadlines are LLM-reviewed as final step so junk and
    cross-document duplicates never reach index.json.
    """

    result = await check_sync_state(paperless, chroma)

    # these will call other pipelines/services
    for doc_id in result.new_ids[:20]:
        await ingest_document(
            doc_id, paperless, chroma, vision, sidecar_service, thumbnail_service
        )

    for doc_id in result.deleted_ids:
        await delete_document(doc_id, chroma, sidecar_service)

    if llm is not None:
        from services.action_review import collect_actions, review_actions

        await review_actions(
            sidecar_service.extr_path, collect_actions(sidecar_service.extr_path), llm
        )

    sidecar_service.create_index_file()

    return result
