import asyncio
import os

# Session storage (.nicegui/) and credentials must not be group/world-readable.
# Must run before any import that creates files or directories.
os.umask(0o077)

from fastapi.responses import FileResponse
from nicegui import app, core, ui

import app_ui.pages.brain  # noqa: F401
import app_ui.pages.browser  # noqa: F401
import app_ui.pages.chat  # noqa: F401
import app_ui.pages.dashboard  # noqa: F401
import app_ui.pages.login  # noqa: F401
import app_ui.pages.settings  # noqa: F401
import app_ui.memo_routes  # noqa: F401  — registers POST /api/memo/transcribe
import app_ui.vault_routes  # noqa: F401  — registers GET /api/vault/file
import werkbank.v2.ui.page  # noqa: F401  — registers /werkbank
from app_ui.tag_style import refresh_tag_colors
from config.settings import settings
from config.version import __version__
from services.clients import cross_ref_index
from services.ollama_watchdog import idle_watchdog
from werkbank.repository import init_db as _werkbank_init_db
from werkbank.v2.store import reset_stale_runs as _werkbank_reset_runs

core.sio.eio.max_http_buffer_size = 16 * 1024 * 1024  # 16 MB for HTTP polling fallback
# NiceGUI 3.x derives ping_interval/ping_timeout from reconnect_timeout at startup.
# We do not want that coupling — see `_keep_pings_short()` below, which pins the
# keep-alive to 25s/20s (safe under nginx proxy_read_timeout) regardless.


def _check_vault_root() -> None:
    """Warn loudly if VAULT_ROOT is unusable.

    The common misconfiguration is setting VAULT_ROOT to a *host* path under
    Docker: that directory does not exist inside the container, so vault and
    memory features silently do nothing. Failing quietly here is the worst
    outcome — the app looks healthy and the user loses their notes' indexing
    without ever seeing an error.
    """
    root = settings.vault_root
    if not root.exists():
        print(
            f"[startup] WARNING: VAULT_ROOT does not exist: {root}\n"
            f"          Vault and memory features will not work.\n"
            f"          In Docker, VAULT_ROOT is the path INSIDE the container "
            f"(default /mnt/vaults); set the host location on the left-hand side "
            f"of the volume mapping in docker-compose.yml.",
            flush=True,
        )
        return
    if not os.access(root, os.W_OK):
        print(
            f"[startup] WARNING: VAULT_ROOT is not writable: {root}\n"
            f"          Memory writes will fail. Check ownership and permissions.",
            flush=True,
        )


@app.on_startup
def _keep_pings_short() -> None:
    """Decouple the socket keep-alive from `reconnect_timeout`.

    NiceGUI derives `ping_interval = 0.8 * reconnect_timeout` at startup, so
    raising the reconnect window to survive a backgrounded PWA would also push
    the keep-alive past nginx's `proxy_read_timeout` and have the proxy cut
    every idle socket. The two settings answer different questions — how often
    to prove the socket is alive, and how long to hold a client's UI after it
    goes quiet — so this pins the first and leaves the second long.

    Registered as a startup handler on purpose: NiceGUI assigns its derived
    values before it invokes these, so anything set at import time is lost.
    """
    core.sio.eio.ping_interval = 25
    core.sio.eio.ping_timeout = 20


@app.on_startup
async def _start_watchdog() -> None:
    _check_vault_root()
    _werkbank_init_db()
    # A run interrupted by a restart is resumable, never auto-resumed:
    # each subtask costs model calls, so restarting one is the user's call.
    _werkbank_reset_runs()
    if settings.ollama_ssh_user:  # idle-shutdown watchdog only with remote-shutdown config
        asyncio.create_task(idle_watchdog())
    # Card renderers read the tag colour map synchronously, so it has to be warm
    # before the first page renders — otherwise every chip briefly shows its
    # fallback hue and then jumps.
    asyncio.create_task(refresh_tag_colors())
    cross_ref_index.build(str(settings.app_path / settings.extraction_sidecar_path))


app.add_static_files("/thumbnails", str(settings.app_path / settings.thumb_path))
app.add_static_files("/static", str(settings.app_path / "app_ui" / "static"))

_manifest_path = str(settings.app_path / "app_ui" / "static" / "manifest.json")
_sw_path = str(settings.app_path / "app_ui" / "static" / "sw.js")


@app.get("/manifest.json")
async def _serve_manifest() -> FileResponse:
    return FileResponse(_manifest_path, media_type="application/manifest+json")


# The service worker must be served from the origin root so its default control
# scope is "/" (a worker at /static/sw.js could only control /static/*). Chrome
# on Android needs an active SW with a fetch handler to offer the WebAPK
# "Install app" flow — without it the app is demoted to a home-screen shortcut.
@app.get("/sw.js")
async def _serve_sw() -> FileResponse:
    return FileResponse(
        _sw_path,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


_pdf_cache = str(settings.app_path / "data" / "pdf_cache")
os.makedirs(_pdf_cache, exist_ok=True)
app.add_static_files("/pdftmp", _pdf_cache)

print(f"PaperlessBrain v{__version__}", flush=True)

ui.run(
    host=settings.host,
    title="PaperlessBrain",
    # Without this NiceGUI serves its own default icon at /favicon.ico. Chrome
    # on Android ignores SVG favicons and falls back to that path, which is why
    # only Chrome-mobile showed the generic NiceGUI logo.
    favicon=str(settings.app_path / "app_ui" / "static" / "icon-192.png"),
    storage_secret=settings.storage_secret,
    reload=False,
    port=settings.port,
    proxy_headers=True,
    forwarded_allow_ips="*",
    # How long the server keeps a client's UI alive after its socket goes quiet.
    # A PWA that gets switched away from on a phone is suspended, not closed:
    # the tab is intact, only the websocket dies. At 30s the server had already
    # dropped the client by the time the user came back, and NiceGUI's only
    # answer to a missing client is a full page reload — losing the half-typed
    # message, the open document, the streaming answer. Five minutes covers a
    # normal "check something else and come back". `_keep_pings_short()` keeps
    # the keep-alive independent of this number.
    reconnect_timeout=300,
)
