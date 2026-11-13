# services/clients.py
from config.settings import settings
from services.brain_service import BrainService
from services.cross_ref_index import CrossRefIndex
from services.paperless import PaperlessClient
from services.sidecar_service import SidecarService
from services.thumbnail_service import ThumbnailService
from services.vector_index import ActiveCollection
from services.vision import OllamaVisionClient
from vault.brain_writer import VaultBrainWriter
from vault.memo_writer import VaultMemoWriter

# Superuser client — used for sync / ingestion / admin operations
paperless = PaperlessClient(
    settings.paperless_url,
    settings.paperless_superuser_token,
)

# The active embedding model's collections (`services/vector_index.py`): an
# admin switching the model changes what these resolve to, without a restart.
chroma = ActiveCollection("documents")
vision = OllamaVisionClient()  # reads base_url/model from settings_store on each call

sidecar_service = SidecarService(
    extraction_sidecar_path=str(settings.app_path / settings.extraction_sidecar_path)
)

cross_ref_index = CrossRefIndex()

brain = BrainService(chroma=ActiveCollection("brain"))

# One-time migration: wipe Chroma-direct brain entries (they have no file backing).
# Vault sync will repopulate from .md files on the first sync_user() call.
_migration_flag = settings.app_path / "data" / "vault_migrated_v1.flag"
if not _migration_flag.exists():
    _name = brain._chroma.collection.name
    brain._chroma.client.delete_collection(_name)
    brain._chroma.collection = brain._chroma._open(_name)
    _migration_flag.parent.mkdir(parents=True, exist_ok=True)
    _migration_flag.touch()

vault_chroma = ActiveCollection("vault")

vault_brain_writer = VaultBrainWriter(brain_chroma=brain._chroma)
vault_memo_writer = VaultMemoWriter(vault_chroma=vault_chroma)

thumbnail_service = ThumbnailService(
    paperless_client=paperless,
    thumbnail_path=str(settings.app_path / settings.thumb_path),
)


def get_session_paperless() -> PaperlessClient:
    """Return a PaperlessClient scoped to the currently logged-in browser user.

    Falls back to the admin token if no session token is present (e.g. during
    sync pipelines or server-side calls outside a page context).
    """
    try:
        from services.session_auth import get_session_token

        token = get_session_token()
        if token:
            return PaperlessClient(settings.paperless_url, token)
    except RuntimeError:
        pass  # called outside a NiceGUI client context
    return paperless
