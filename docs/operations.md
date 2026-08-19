# Operations

Write-back into Paperless, waking and shutting down a GPU host, security
posture, and installing without Docker. The [README](../README.md) has the overview.

## Text write-back (opt-in)

Paperless-ngx stores whatever its OCR engine produced. The vision model usually
reads the same pages better — scans, tables, poor originals — but that text only
lives in the sidecars here, so Paperless' own full-text search keeps hitting the
weaker OCR.

Enable **Settings > Paperless-ngx write-back > "Push AI extracted text to
Paperless-ngx"** and every sync ends with a comparison pass: for each document
whose sidecar text differs from the Paperless content, the sidecar text is
PATCHed in. The comparison ignores whitespace differences, and texts under 40
characters are treated as failed extractions and never pushed.

The setting is **per user and off by default**, and it travels with the settings
export/import.

Three things to know before switching it on:

- **It overwrites.** Paperless-ngx keeps no history of document text — the
  previous OCR result is gone. Everything else about the document (title, tags,
  correspondent, the file itself) is untouched.
- **Paperless can overwrite it back.** Reprocess, rotate, split, merge or edit a
  document there and its own OCR replaces the text again. The next sync pushes
  yours back.
- **It runs with your permissions.** The PATCH uses the signed-in user's own
  Paperless token, not the superuser token the sync otherwise uses, so it can
  only touch documents you may edit. Failures are counted and logged in the sync
  log rather than aborting the run.

It is idempotent: after a push both sides hold the same string, so the next sync
finds nothing to do.

## Power management (optional)

For homelab GPU servers: the app can wake the Ollama host via Wake-on-LAN on
first use and shut it down over SSH after idle. Set
`OLLAMA_HOST_LAN_MAC_ADDRESS_WOL` and `OLLAMA_SSH_USER` to enable — with Docker
this needs `network_mode: host` (magic packets don't cross the bridge network).

The shutdown half (dashboard button *and* the idle watchdog) runs
`ssh <OLLAMA_SSH_USER>@<ollama host> "sudo shutdown -h now"` non-interactively —
no password prompt is possible, so both steps below are mandatory:

1. **Key-based SSH login** from the app to the Ollama host. On the app host:
   `ssh-copy-id <OLLAMA_SSH_USER>@<ollama host>`. In Docker the container has no
   identity of its own — mount the key read-only (see the commented line in
   `docker-compose.yml`):
   `- /root/.ssh/id_ed25519:/root/.ssh/id_ed25519:ro`
2. **Passwordless sudo for shutdown** on the Ollama host. `visudo` and add:
   ```
   <OLLAMA_SSH_USER> ALL=(ALL) NOPASSWD: /usr/bin/shutdown
   ```
   Without it the SSH call fails with `sudo: a password is required`.

Verify without powering anything off:

```bash
ssh -o BatchMode=yes <OLLAMA_SSH_USER>@<ollama host> "sudo -n /usr/bin/shutdown --help >/dev/null && echo READY"
```

(Inside Docker: prefix with `docker exec <container> `.) `READY` means both the
button and the idle watchdog will work.

## Security notes

- The **superuser token** is used only for sync/ingestion; every chat request
  runs with the logged-in user's own Paperless session token, so Paperless
  object permissions apply.
- Sessions are encrypted server-side with `STORAGE_SECRET`.
- Per-user IMAP/CalDAV credentials and API keys are stored encrypted with a key
  derived from the user's session — never in plaintext.
- The app is designed for LAN / reverse-proxy deployment; it does not implement
  rate limiting or public-internet hardening. Put it behind your proxy + SSO if
  you expose it.

## Bare-metal install

> **On Windows, use Docker.** The bare-metal path needs the GTK3 runtime for
> PDF generation, which has no pip-installable equivalent — the container ships
> it for you and behaves identically on every host OS.

WeasyPrint (PDF generation) loads Pango at render time, so a missing library
shows up as a failed PDF export rather than a startup error. Install it and a
font family up front:

```bash
# Debian/Ubuntu
sudo apt install libpango-1.0-0 libpangoft2-1.0-0 fonts-dejavu-core
# Fedora
sudo dnf install pango dejavu-sans-fonts
# Arch
sudo pacman -S pango ttf-dejavu
# macOS
brew install pango
```

Verify before you rely on it:

```bash
python -c "from weasyprint import HTML; HTML(string='<p>ok</p>').write_pdf(); print('PDF OK')"
```

```bash
python3.12 -m venv .venv && source .venv/bin/activate   # 3.12–3.14
pip install --extra-index-url https://download.pytorch.org/whl/cpu -e ".[crawl]"
playwright install chromium        # only for the [crawl] extra
cp .env.example .env               # edit values, APP_PATH = repo root
python main.py
```

On GPU machines drop the `--extra-index-url` to get CUDA torch. A systemd unit
is the recommended way to run it as a service.
