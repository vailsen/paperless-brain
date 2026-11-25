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

## AI document names (opt-in)

Scanners and mail importers name files `scan_0042.pdf` or `20261017_143012.pdf`,
and Paperless turns that into the title. PaperlessBrain can replace it with a name
built from a template you write:

```
{year}_{month}_{day}_{doc_type}_{description}_{correspondent}
→ 2026_10_17_Invoice_Computer_parts_Amazon
```

Only `{description}` — what the document is about — comes from the model, plus
`{doc_type}` when Paperless has no document type. The model is asked for the
category rather than the items (an invoice for CPU, RAM and SSD is "computer
parts" / "Computerteile"), and the answer is then made fit for a file name
whatever the model wrote: filler words in English and German ("and", "of",
"und", "für" …) are dropped, at most three words remain, joined with `_`, and
trailing words go when the description would exceed 40 characters. Date and
correspondent are Paperless' own metadata, so they always match what Paperless
shows. The model is the ingestion model chosen under **Settings > Documents >
Processing new documents**; it names a document from the summary it already
wrote, with one short text request and no page images.

**Paperless renames the file, not PaperlessBrain.** Paperless-ngx offers no way
for an app to set a file name; it builds the stored name from its own filename
format on every save. PaperlessBrain sets the *title*, so for the file on disk to
carry it, add this line to `docker-compose.env` in your Paperless-ngx folder
(not to its `.env`, which only feeds Docker Compose itself) and restart Paperless:

```
PAPERLESS_FILENAME_FORMAT={{ title }}
```

Without it only the title changes. Files that already exist are renamed the next
time their title changes, or all at once with
`docker compose exec webserver document_renamer`. Paperless' own download button
always prefixes the date and correspondent, whatever the title says.

Enable it under **Settings > Documents > "Rename documents after AI analysis"**.
Like the text write-back it is **per user, off by default**, and runs with your
own Paperless token, so it only touches documents you may edit.

- **New documents** are renamed at the end of each sync — only the ones that
  sync ingested.
- **Existing documents** are renamed only through **"Rename existing
  documents…"**. It explains the steps, then — if you start it — a dry run shows
  10 of them old → new without changing anything. "Rename all" then works through the rest on the server, with progress
  and a stop button; closing the dialog or the page does not stop it.
- **Every document is renamed once,** hand-edited titles included. After that it
  is never touched again, so a title you change later stays. The old and new
  titles are kept in `data/title_renames.json`.

## Sleeping GPU host

paperless-brain talks to any Ollama- or OpenAI-compatible endpoint. It does no
power management of its own: no Wake-on-LAN, no idle shutdown, no power
buttons.

If your GPU host sleeps, put a **wake-on-request proxy** in front of it and
point `OLLAMA_SERVER` (and the base URL of the local models in
*Settings > AI Models*) at the proxy instead of the host.

### Proxy contract

What paperless-brain relies on:

- **Hold inference requests while the host wakes**, then forward them
  (streaming passes through). paperless-brain's read timeouts for local
  inference are 330 s, enough for a 240 s wake plus model load.
- **Answer `/api/tags`, `/api/version`, `/v1/models` and `/api/ps` without
  waking the host** (from a cache while it sleeps), and without counting them
  as activity. The dashboard and the worker poll these.
- **Only shut down what the proxy itself woke.** A host someone switched on
  stays on.

Optional, used when present (a plain Ollama ignores the headers):

- `GET /proxy/status` → `{"host": "asleep|waking|up|shutting_down", …}`.
  The dashboard shows that state, the chat shows "The model host is starting…"
  while it wakes, and the proactive worker decides with it: inside a sweep
  window a sleeping host counts as ready (the job's request wakes it), outside
  a window only a host that is up does.
- `X-Idle-Profile: background` on every request of the proactive worker, so
  the proxy can shut a host it woke for background work down sooner.
- `X-No-Wake: 1` on the worker's requests outside a sweep window: if the host
  is asleep the proxy answers `503` instead of waking it.

Without `/proxy/status` the chat shows the generic hint after about 5 s
without a first token, and the worker falls back to probing the endpoint.

### Migrating from the built-in Wake-on-LAN

Earlier versions woke and shut down the host themselves. To move to a proxy:

1. **First** empty `OLLAMA_HOST_LAN_MAC_ADDRESS_WOL` and `OLLAMA_SSH_USER`
   (better: remove all of the settings below) and restart. Do this before the
   proxy goes live: two watchdogs would otherwise shut down the same host,
   one of them without knowing about the other's requests.
2. Set `OLLAMA_SERVER=http://<proxy>:11434`.
3. Change the base URL of every local model in *Settings > AI Models* from the
   host to the proxy. Each user's models are stored encrypted with their own
   Paperless token, so each user changes their own.
4. Remove the SSH key mount and `network_mode: host` from your
   `docker-compose.yml` if they were only there for Wake-on-LAN, and the app's
   key from the GPU host's `authorized_keys`.

These settings no longer do anything; if one is still set, the app logs a
warning once at start: `OLLAMA_HOST_LAN_MAC_ADDRESS_WOL`, `OLLAMA_SSH_USER`,
`OLLAMA_IDLE_SHUTDOWN_MINUTES`, `OLLAMA_CHAT_IDLE_SHUTDOWN_MINUTES`,
`OLLAMA_WAKE_TIMEOUT_S`, `SHUTDOWN_PASSWORD`, `PROACTIVE_WOL_AUTO`,
`PROACTIVE_IDLE_SHUTDOWN_MINUTES`.

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
