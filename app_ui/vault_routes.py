"""Read-only file delivery for vault attachments.

The note editor edits `.md` and nothing else, but a note that references
`scan.pdf` is only half readable if the attachment cannot be opened. This
serves those files — and only those: `.md` never goes through here (the editor
already has the text), and there is no upload counterpart on purpose.

Path handling is delegated to `vault.notes.resolve`, the same choke point the
editor uses, so traversal and symlink escape are rejected in one place.
"""

import logging
import mimetypes

from fastapi.responses import FileResponse, JSONResponse
from nicegui import app as ng_app

from vault import notes

_log = logging.getLogger(__name__)

FILE_PATH = "/api/vault/file"


@ng_app.get(FILE_PATH)
async def vault_file(path: str, download: bool = False):
    try:
        username = ng_app.storage.user.get("paperless_user", "")
    except Exception:  # no session context
        username = ""
    if not username:
        return JSONResponse({"error": "not authenticated"}, status_code=401)

    try:
        target = notes.resolve(username, path, must_exist=True)
    except (notes.VaultPathError, FileNotFoundError, OSError):
        return JSONResponse({"error": "not found"}, status_code=404)
    if not target.is_file() or notes.is_hidden(target.name):
        return JSONResponse({"error": "not found"}, status_code=404)

    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(
        target,
        media_type=media_type,
        filename=target.name if download else None,
    )
