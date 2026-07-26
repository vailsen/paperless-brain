"""Shared test setup.

`config.settings` instantiates a pydantic Settings at import time, so the
required environment must exist before any application module is imported.
pytest imports conftest first, which makes this the only reliable place for it.

APP_PATH points at a throwaway directory: several modules create directories
under it at import (e.g. credential_store), and tests must never touch the
developer's real `data/`.
"""

import os
import tempfile
from pathlib import Path

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="paperlessbrain-tests-"))

os.environ.update(
    {
        "APP_PATH": f"{_TEST_ROOT}/",
        "PAPERLESS_URL": "http://paperless.invalid",
        "PAPERLESS_SUPERUSER_TOKEN": "test-token",
        "EMBEDDING_MODEL": "intfloat/multilingual-e5-large-instruct",
        "CHROMA_PATH": "data/chroma_db/",
        "CHROMA_COLLECTION": "test_documents",
        "EXTRACTION_SIDECAR_PATH": "data/extractions/",
        "THUMB_PATH": "data/thumbnails/",
        "STORAGE_SECRET": "test-secret",
        "VAULT_ROOT": str(_TEST_ROOT / "vaults"),
    }
)

import pytest  # noqa: E402


@pytest.fixture
def tmp_sidecar_dir(tmp_path: Path) -> Path:
    """Empty directory standing in for EXTRACTION_SIDECAR_PATH."""
    d = tmp_path / "extractions"
    d.mkdir()
    return d
