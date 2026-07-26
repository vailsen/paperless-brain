import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config.settings import settings
from pipelines import paperless_db_sync
from services.chroma import ChromaClient
from services.paperless import PaperlessClient
from services.sidecar_service import SidecarService
from services.thumbnail_service import ThumbnailService
from services.vision import OllamaVisionClient

os.environ["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"


async def main():
    paperless = PaperlessClient(
        settings.paperless_url,
        settings.paperless_superuser_token,
    )

    chroma = ChromaClient(
        collection_name=settings.chroma_collection,
        persist_directory=str(settings.app_path / settings.chroma_path),
        embedding_function=settings.embedding_model,
    )
    vision = OllamaVisionClient(
        base_url=settings.ollama_server, model=settings.ollama_ingest_model
    )

    sidecar_service = SidecarService(
        extraction_sidecar_path=str(
            settings.app_path / settings.extraction_sidecar_path
        )
    )

    thumbnail_service = ThumbnailService(
        paperless_client=paperless,
        thumbnail_path=str(settings.app_path / settings.thumb_path),
    )

    print(
        await paperless_db_sync.run_sync(
            paperless, chroma, vision, sidecar_service, thumbnail_service
        )
    )

    print("A")


asyncio.run(main())
