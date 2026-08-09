<p align="center">
  <img src="app_ui/static/paperlessbrain_logo.png" alt="PaperlessBrain logo" width="140">
</p>

<h1 align="center">PaperlessBrain</h1>

<p align="center"><b>The brain for your <a href="https://docs.paperless-ngx.com/">Paperless-ngx</a> archive.</b><br>
Chat with your documents, let a vision LLM read every page, never miss a deadline.<br>
Installable PWA — built for mobile and desktop.</p>

<p align="center">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-blue">
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-blue">
  <a href="https://nicegui.io/"><img alt="Built with NiceGUI" src="https://img.shields.io/badge/built%20with-NiceGUI-4a9?logo=python&logoColor=white"></a>
</p>

<p align="center">
  <img src="docs/screenshots/chat.png" alt="Chatting with the archive — the assistant searches, reads and cites documents through an agentic tool loop" width="820">
</p>

---

> **Status and expectations.** This is a personal homelab project, shared as-is because it
> might be useful to someone else. It has rough edges, and I'm not a full-time
> maintainer — issues and PRs get answered when I have time.
>
> I'd rather this grew into one good app than five half-finished ones, so if something is
> missing, broken or awkward, please open an issue or a PR here. Good work gets merged, and if
> you want a larger role than that, just ask. It's MIT — you're free to fork, but I'd much
> rather build it with you.

## What it does

Paperless-ngx stores and organizes your documents. PaperlessBrain reads them —
every page, through a vision LLM — and turns the archive into something you can
talk to.

- **Chat with your archive** — an agentic tool loop that searches, reads,
  cross-references and cites your documents. Any Anthropic-compatible endpoint
  (Claude, MiniMax …) or OpenAI-compatible endpoint (local Ollama, OpenAI,
  OpenRouter, vLLM …) — you add models per user in Settings.
- **Vision-LLM ingestion** — each page is rendered to an image and read by a
  vision model: full-text summary, tables, actions and deadlines, extracted per
  document type. Runs entirely locally on Ollama if you want it to, or on any
  cloud vision model — same registry as the chat.
- **Deadlines & actions** — extracted obligations ("cancel by …", "pay until …")
  surfaced on the dashboard.
- **Brain memory** — the assistant remembers facts about you as plain Markdown
  files, embedded for recall in every conversation.
- **Voice memos** — hold the mic button (or swipe up to lock it), speak, and the
  recording is transcribed, tidied into structured Markdown and filed into your
  vault after you approve it. Upload an existing audio file instead if you
  recorded it elsewhere, or switch to **conversation mode** to turn a recorded
  dialog into speaker turns. Optional; needs your own transcription service —
  see [Voice memos](#voice-memos-optional).
- **Vault notes** — your own Markdown knowledge base, searchable in chat
  alongside the archive. Works with any editor; Obsidian optional.
- **Deep research** — autonomous multi-step research, but the kicker is the data
  source: the agent researches across **your own documents** alongside the web. A
  deterministic orchestrator splits a job into sub-tasks, runs scoped agents, and
  synthesizes a reviewed result.
- **Document generation** — DIN-5008 letters (German market), email drafts,
  chat-to-PDF saved back into Paperless.
- **Notes back into Paperless** — the assistant can file a note on a document
  under your own Paperless account ("paid on 12.05., bank transfer"), so the
  record lives where the document lives.
- **OCR write-back (opt-in)** — after each sync, documents whose vision-read
  text beats what Paperless-ngx has get their text replaced there, which fixes
  the Paperless full-text search too. Off by default; see
  [Text write-back](#text-write-back-opt-in).
- **Email & calendar tools** — per-user IMAP and CalDAV credentials, encrypted;
  the assistant can check mail and appointments when you ask.
- **Web search** — via your self-hosted SearXNG instance, with full-page
  reading (trafilatura, optional headless Chromium for JS-heavy pages).
- **PWA** — installable on phone and desktop, per-user language (English/German),
  dark/light theme.

## Why I built this

This is a small project I built for my own document system. In early 2026 I
tried several existing tools and none fit my needs — for what I wanted they were
missing the advanced parts: using high-end consumer hardware effectively, a more
detailed LLM ingestion, an information-rich vector database, and genuinely useful
document detail views. It grew feature by feature and is now feature-complete
enough that it feels worth sharing with the community.

My best personal results have been with **Qwen3.6-35B-A3B (MTP, Q4)** as the chat
model — Multi-Token Prediction makes it faster than any cloud model I've tried,
while staying really strong at tool use. Your mileage will vary with your own
hardware and models; nothing here is tied to a specific one.

## Screenshots

<table>
  <tr>
    <td width="50%"><img src="docs/screenshots/dashboard.png" alt="Dashboard showing extracted deadlines and actions"><br><sub><b>Dashboard</b> — deadlines & actions extracted from every document</sub></td>
    <td width="50%"><img src="docs/screenshots/document-detail.png" alt="Document detail dialog with vision-read pages, tables and actions"><br><sub><b>Document detail</b> — vision-read pages, tables, actions, cross-references</sub></td>
  </tr>
  <tr>
    <td colspan="2"><img src="docs/screenshots/demo.gif" alt="Deep research — autonomous multi-step research module in action" width="100%"><br><sub><b>Deep research</b> — autonomous multi-step research, reviewed before persist</sub></td>
  </tr>
</table>

## Architecture

```mermaid
flowchart LR
    P[Paperless-ngx] <-->|REST API| B(PaperlessBrain)
    B <--> C[(ChromaDB<br/>+ JSON sidecars)]
    B <-->|chat + vision ingest<br/>local| O[Ollama]
    B <-->|chat + vision ingest<br/>optional| A["Anthropic-compatible<br/>(Claude, MiniMax …)"]
    B <-->|chat + vision ingest<br/>optional| X["OpenAI-compatible<br/>(OpenAI, OpenRouter, vLLM …)"]
    B <--> V[/"Vault (Markdown + git)"/]
    B -->|web search| S[SearXNG]
    B <-->|mail| M[IMAP]
    B <-->|calendar| D[CalDAV / iCal]
```

Sync compares Paperless against the index, new documents are rendered page by
page and read by the vision model; results live in JSON sidecars plus ChromaDB
embeddings (`multilingual-e5` — multilingual retrieval). Each run ends with
removal of deleted documents, an LLM review of the extracted deadlines, and —
if you enabled it — the text write-back into Paperless. The two LLM backends
share one tool set and one streaming event protocol — see
[AI models & providers](#ai-models--providers) for what you can point them at.

## Quick start (Docker)

```bash
mkdir paperless-brain && cd paperless-brain
BASE=https://raw.githubusercontent.com/Vailsen/paperless-brain/main
curl -O  $BASE/docker-compose.yml
curl -o .env $BASE/.env.example   # fill in: PAPERLESS_URL, PAPERLESS_SUPERUSER_TOKEN, STORAGE_SECRET
docker compose up -d              # pulls the prebuilt image from GHCR (no local build)
```

Two files, ~6 KB — the app itself is the prebuilt image, so there is nothing to
clone. Prefer a fixed version over `main`: swap `main` for a tag such as
`v0.2.0` in `$BASE`.

Before the first start, edit the **vault mount** in `docker-compose.yml` so it
points at the directory holding your existing Markdown notes — one subfolder per
Paperless-ngx user (`/srv/obsidian/alice` for user `alice` → mount
`- /srv/obsidian:/mnt/vaults`). Left unchanged, the app creates an empty vault
under `./vaults` and your real notes are never indexed.

`.env.example` is the short version; grab
[`.env.example.full`](https://raw.githubusercontent.com/Vailsen/paperless-brain/main/.env.example.full)
the same way for the annotated reference with every key.

To build from source instead, clone the repository and swap `image:` for
`build: .` in `docker-compose.yml`.

Open `http://localhost:8080` and log in with your **Paperless-ngx username and
password** — users, permissions and sessions come from Paperless itself.

First boot downloads the embedding model (~2.2 GB) into a Docker volume; the
container needs internet access once.

Image variants: the default tag (`:latest`) includes headless Chromium for
JS-heavy web pages; the `:lean` tag is a ~1 GB smaller image where web reading
falls back to trafilatura only. Pin a release with `:1.2.3` (or `:1.2.3-lean`)
for reproducible deploys.

## Prerequisites

| You need | Notes |
|---|---|
| Paperless-ngx | any recent version + a superuser API token |
| A vision-capable model | for document ingestion — local via Ollama (e.g. a Qwen-VL-class model, can run on another machine) or any cloud vision model (Claude, GPT, MiniMax …). Picked in Settings > Processing |
| An LLM endpoint for chat | Anthropic-compatible or OpenAI-compatible — local, direct cloud provider or an AI gateway; added per user with URL + key in Settings > AI Models. See [AI models & providers](#ai-models--providers) |
| SearXNG | optional — enables the web-search tool |
| IMAP mailbox / CalDAV calendar | optional — enables the mail and appointment tools (per-user credentials, encrypted) |
| Wake-on-LAN capable GPU server | optional — see [Power management](#power-management-optional) |

## AI models & providers

Models are added **per user** in *Settings > AI Models* — base URL, API key,
model id. There are two backend types, `anthropic` and `openai_compatible`, and
both take a **custom base URL**, so anything speaking either protocol works:

- **Local** — Ollama, vLLM, llama.cpp, LM Studio, TGI
- **Direct cloud** — Anthropic, OpenAI, MiniMax, or any provider with a
  compatible endpoint
- **AI gateways / LLM routers** — OpenRouter, Requesty, LiteLLM, Portkey,
  Cloudflare AI Gateway and similar. Point `openai_compatible` at the gateway's
  endpoint (e.g. `https://openrouter.ai/api/v1` or
  `https://router.requesty.ai/v1`), use the gateway key, and set the model id in
  whatever `provider/model` form that gateway expects.

There are no hardcoded provider integrations and no API key belongs in `.env` —
every key lives with its model in the registry. The same registry serves chat,
deep research and vision ingestion, so a gateway model can do all three.

Each model also carries its own temperature, output-token cap and **thinking
mode**. Thinking defaults to *Auto*, which means the request says nothing about
it and the model decides: Claude then never thinks, and a reasoning model like
MiniMax only sometimes does. Set it to ON (with a token budget) to get reasoning
on every turn — on the `anthropic` backend that also pins temperature to 1,
which the API requires.

## Configuration (`.env` reference)

Copy `.env.example` to `.env` (the short list of keys most installs need);
`.env.example.full` is the annotated reference with every key and its default.
Keys without a default are **required**. Keys
marked *UI* can also be changed in the app's Settings page (the app value wins;
the env value is only the initial fallback).

| Key | Required | Default | Description |
|---|---|---|---|
| `APP_PATH` | ✅ | — | Absolute path to the app root, with trailing slash. In Docker: `/app/` |
| `PAPERLESS_URL` | ✅ | — | Base URL of your Paperless-ngx instance |
| `PAPERLESS_SUPERUSER_TOKEN` | ✅ | — | API token of a Paperless superuser (used for sync; chat requests use each user's own session token) |
| `IGNORE_INBOX_TAG_AT_SYNC` | | `Inbox` | *UI* — documents carrying this tag are skipped during sync |
| `EMBEDDING_MODEL` | ✅ | — | Sentence-transformers model id; ships tuned for `intfloat/multilingual-e5-large-instruct` |
| `CHROMA_PATH` | ✅ | — | ChromaDB directory, relative to `APP_PATH` (keep under `data/`) |
| `CHROMA_COLLECTION` | ✅ | — | Collection name for document embeddings |
| `EXTRACTION_SIDECAR_PATH` | ✅ | — | Directory for per-document JSON extraction sidecars |
| `THUMB_PATH` | ✅ | — | Directory for thumbnails |
| `CHROMA_MAX_RESULTS` | | `20` | *UI* — max search results per query |
| `BRAIN_HINT_SIMILARITY_THRESHOLD` | | `0.70` | *UI* — min similarity for memory hints in search |
| `BRAIN_HINT_WINDOW_FACTOR` | | `1.5` | *UI* — window factor for memory hints |
| `OLLAMA_SERVER` | | empty | *UI* — Ollama base URL for vision ingestion; also names the host the WoL/shutdown buttons control (otherwise inferred from your first local-lane model) |
| `OLLAMA_INGEST_MODEL` | | empty | *UI* — vision model used to read documents |
| `EXTRACTION_PROFILE` | | `en` | *UI* — extraction-rule profile: `en` or `de` (see [Extraction rules](#extraction-rules)) |
| `ARCHIVE_LANGUAGE` | | `en` | *UI* — language of AI-generated summaries (archive-level — sidecars are shared by all users) |
| `TZ` | | system | IANA timezone for timestamps on generated documents |
| `OLLAMA_HOST_LAN_MAC_ADDRESS_WOL` | | empty | MAC for Wake-on-LAN (empty = feature hidden) |
| `OLLAMA_SSH_USER` | | empty | SSH user for remote shutdown of the Ollama host (empty = feature hidden). Needs passwordless sudo for `/usr/bin/shutdown`; in Docker, mount an SSH key into the container |
| `OLLAMA_IDLE_SHUTDOWN_MINUTES` | | `30` | Idle minutes before the Ollama host is shut down |
| `AI_GENERATED_TAG_NAME` | | `AI-generated` | *UI* — tag applied to documents the app creates in Paperless |
| `AI_GENERATED_CORRESPONDENT` | | `PaperlessBrain AI` | *UI* — correspondent for AI-generated documents |
| `AI_GENERATED_DOC_TYPE` | | `Information` | *UI* — document type for AI-generated documents |
| `SEARXNG_HOST` | | `http://localhost:8888` | SearXNG base URL for the web-search tool |
| `VAULT_ROOT` | | `/mnt/vaults` | Root directory holding one vault subfolder per user. **In Docker this is the path inside the container — leave it at `/mnt/vaults` and set the host location on the left side of the volume mapping.** |
| `BRAIN_SUBFOLDER` | | `PaperlessBrain Memory` | Vault subfolder reserved for agent-curated memory (names a real folder — change only on a fresh install) |
| `MEMO_SUBFOLDER` | | `PaperlessBrain Memos` | Vault subfolder holding one file per voice memo (names a real folder — change only on a fresh install) |
| `VAULT_SYNC_COOLDOWN_S` | | `3` | Min seconds between vault sync runs per user |
| `WHISPER_URL` | | empty | OpenAI-compatible transcription endpoint incl. `/v1`. **Empty = voice memos hidden.** See [Voice memos](#voice-memos-optional) |
| `WHISPER_API_KEY` | | empty | Bearer token for that endpoint, if it needs one |
| `WHISPER_MODEL` | | `whisper-1` | Model id the endpoint expects; most self-hosted servers ignore it |
| `WHISPER_LANGUAGE` | | empty | Force a language (e.g. `de`); empty = auto-detect, less accurate on short clips |
| `MEMO_MAX_UPLOAD_MB` | | `25` | Upload cap for a single recording |
| `MEMO_MAX_SECONDS` | | `300` | Hard stop for a recording, so a stuck press cannot record forever |
| `CONVERSATION_MAX_UPLOAD_MB` | | `200` | Upload cap in conversation mode; keep at or below the transcription service's own limit |
| `CONVERSATION_MAX_SECONDS` | | `3600` | Hard stop for a conversation recording |
| `STORAGE_SECRET` | ✅ | — | Secret encrypting server-side sessions — generate with `python -c "import secrets; print(secrets.token_hex(32))"` |
| `SHUTDOWN_PASSWORD` | | empty | Confirmation prompt before the shutdown button acts. **Empty = no prompt**, one click powers the machine off. What hides the buttons is an empty `OLLAMA_HOST_LAN_MAC_ADDRESS_WOL` / `OLLAMA_SSH_USER` |
| `HOST` / `PORT` | | `0.0.0.0` / `8080` | Bind address and port |

## Vault & memory

The assistant's long-term memory and your personal notes live in a **plain
folder of Markdown files** — one subfolder per user under `VAULT_ROOT`:

```
vaults/
└── alice/
    ├── PaperlessBrain Memory/  ← agent-curated facts (one fact = one file)
    ├── PaperlessBrain Memos/   ← voice memos (one memo = one file)
    └── ... your notes ...      ← searchable knowledge base
```

- The folder is **git-tracked by the app itself** — change detection, sync
  bookmark and audit trail in one. You don't have to touch git.
- **Obsidian is NOT required.** Any editor works; the files are ordinary
  Markdown with a small YAML frontmatter.
- Optional topology for editing on other devices: point a WebDAV server at the
  same directory and sync it with Obsidian + Remotely Save. The WebDAV server
  must serve the *same* directory the app mounts — the app keeps the single
  authoritative copy.

Markdown on disk is the source of truth; ChromaDB is only the index and can
always be rebuilt from the files.

## Voice memos (optional)

Open the microphone button in the header, hold the record button, speak, release.
For anything longer than a sentence, swipe up while holding: the recording locks
and keeps running with your thumb off the button, and the next tap stops it. The
recording is transcribed, cleaned up by whichever chat model you have selected —
filler removed, bullet lists and tables where the content calls for them, nothing
added and nothing dropped — and shown to you for review. Save, and it becomes one
Markdown file per memo in `PaperlessBrain Memos/` inside your vault, named
`2026-08-08 1432 Topic.md`, searchable in chat like any other note.

The dialog also takes an **existing audio file** — wav, m4a, mp3, ogg, flac —
which is the way in for a recording made on your phone's own voice recorder or a
call you already captured. And with no microphone at all (or no HTTPS) you can
simply type the memo. All three routes end in the same review-then-file step.

It is **not** a chat feature. Pressing the button is what tells the app you mean
a memo, so nothing has to guess whether "erinner mich dran…" was meant as a
memory, a deadline or a note to yourself.

**You need a transcription service.** PaperlessBrain doesn't ship one — no
bundled model, no extra dependency. Anything with an OpenAI-compatible
`/v1/audio/transcriptions` endpoint works:

| Option | Notes |
|---|---|
| [hwdsl2/docker-whisper](https://github.com/hwdsl2/docker-whisper) | faster-whisper in one container, API key generated for you, CPU and CUDA images |
| [Speaches](https://github.com/speaches-ai/speaches) | loads models on demand, also does TTS; no auth by default |
| [whisper.cpp](https://github.com/ggml-org/whisper.cpp) | no container needed; run `whisper-server --inference-path /v1/audio/transcriptions --convert` |
| Groq / OpenAI | hosted; your dictation leaves the house |

```bash
WHISPER_URL=http://127.0.0.1:9000/v1    # empty = feature hidden everywhere
WHISPER_API_KEY=
WHISPER_MODEL=whisper-1
WHISPER_LANGUAGE=de                     # empty = auto-detect
```

Then switch it on in **Settings > Voice memos** (on by default once the URL is
set). When it's configured, the chat mic switches to the same service too —
noticeably better than the browser's built-in speech recognition, at the cost of
no live preview while you talk.

### Conversation mode

The dialog has a **Memo / Conversation** switch. In conversation mode the
transcript comes back as speaker turns:

```markdown
**Speaker 1:** The quote lands at 14,200 including the worktop.

**Speaker 2:** And the deadline?

**Speaker 1:** End of March.
```

Three things worth knowing before you rely on it:

- **Your transcription service has to do the diarization.** PaperlessBrain asks
  for `response_format=verbose_json` and reads the per-segment `speaker` field;
  a service without a diarizer returns no such field and you get an ordinary
  unlabelled transcript, no error. For `hwdsl2/whisper-server` set
  `WHISPER_DIARIZATION=true` (it bundles a sherpa-onnx diarizer, no GPU needed).
- **Speakers are numbered, never named.** Diarization is unsupervised: it can
  tell two voices apart but has no idea whose they are. You get `Speaker 1` /
  `Speaker 2`, renumbered by who speaks first, and you rename them yourself in
  the review step if you want real names.
- **Attribution is segment-level.** A Whisper segment spanning a speaker change
  is assigned wholesale to whoever talked most in it, so quick interjections and
  cross-talk land on the wrong person. Good for "who said roughly what", not a
  verbatim record.

Conversation mode also runs a different rewrite prompt — one that preserves turn
order and never merges speakers — and uses the larger `CONVERSATION_MAX_*` caps,
since a meeting is not a memo. If only one voice is detected, the labels are
dropped and you get ordinary prose.

CPU is fine. On 8 cores with `large-v3-turbo`, 30 seconds of German speech
transcribes in about 6 seconds. Don't put it on a machine the
[power management](#power-management-optional) watchdog shuts down — waiting for
a wake-on-LAN boot defeats the point of quick capture.

Two things to know:

- **Recording needs HTTPS.** Browsers only grant microphone access over HTTPS or
  on `localhost`. Over plain `http://192.168.x.x:8080` the record button is
  disabled and says so. You can still type a memo.
- **Silence produces text.** Whisper answers non-speech with a confident stock
  phrase rather than nothing, so an accidental press would otherwise file a
  convincing memo about nothing. Recordings that are too short, or that come
  back as one of the known filler phrases, are rejected before anything is
  written.

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

## Extraction rules

Ingestion prompts are keyed to your Paperless **document type** names. Because
those names are whatever *you* called them, the rules ship as selectable
profiles in `config/extraction_rules/`:

| `EXTRACTION_PROFILE` | Contents |
|---|---|
| `en` (default) | ~13 common international types — Invoice, Receipt, Contract, Bank Statement, Payslip, Insurance Policy, Tax Assessment, Notice, Certificate, Letter, Report, Rental Agreement |
| `de` | ~46 types for the German legal/administrative domain |

Any document type without its own entry falls through to `_default`, which still
produces usable extraction — it just lacks type-specific guidance. **Nothing
breaks if your types don't match**; the results are simply more generic.

To tailor it, edit the profile module and add entries keyed by your exact
Paperless document-type name:

```python
# config/extraction_rules/en.py
RULES["Warranty"] = {
    "prompt": BASE_INSTRUCTIONS + """
Document type: Warranty certificate.
Pay particular attention to:
- Product, serial number and purchase date
- Warranty period and expiry date
- What is covered and what voids the warranty
""",
}
```

To add a whole profile, drop `<code>.py` next to the others exporting a `RULES`
dict and add the code to `AVAILABLE_PROFILES` in `__init__.py`. It then appears
in the profile selector automatically.

The profile follows **the names of your Paperless document types**, not the
language of the documents. An English invoice filed as `Rechnung` still matches
the `de` rules — so a mixed-language archive needs no special handling.

`ARCHIVE_LANGUAGE` separately controls the language of generated summaries;
extracted page text always keeps the document's original language, whatever it
is. Both are set in **Settings > Processing**, with the `.env` values as
fallback.

## Languages (i18n)

UI ships in **English and German**; each user picks their language in Settings
(chat answers follow it automatically). Adding a language:

1. Add the code to `SUPPORTED_LANGUAGES` in `i18n.py` (e.g. `"fr": "Français"`).
2. `pybabel init -i locales/messages.pot -d locales -l fr`
3. Translate `locales/fr/LC_MESSAGES/messages.po`.
4. `pybabel compile -d locales`

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

## Contributing

Issues and PRs welcome — this is an early alpha extracted from a personal
homelab project, so expect rough edges. Please open an issue before large
changes. CI runs the test suite on every PR.

## Acknowledgements

**[NiceGUI](https://nicegui.io/)** ([zauberzeug/nicegui](https://github.com/zauberzeug/nicegui))
is the entire frontend. Every page, dialog, the streaming chat view and the
kanban board are plain Python — no JavaScript build step, no separate frontend
service, no API layer to keep in sync. A one-person project keeps a UI this
large maintainable only because NiceGUI removed that whole category of work.
The reactive `@ui.refreshable` model and the async event loop shared with
FastAPI are what make token-by-token streaming into the browser a few lines
instead of a websocket protocol. Thank you, Zauberzeug.

**[Paperless-ngx](https://docs.paperless-ngx.com/)**
([paperless-ngx/paperless-ngx](https://github.com/paperless-ngx/paperless-ngx))
is the archive this is built on — it stores the documents, owns the users and
permissions, and is the reason this project can concentrate on reading and
reasoning instead of document management. PaperlessBrain adds to it; it does
not replace it.

Also standing on: [FastAPI](https://fastapi.tiangolo.com/),
[ChromaDB](https://www.trychroma.com/),
[sentence-transformers](https://sbert.net/) with
[intfloat/multilingual-e5-large-instruct](https://huggingface.co/intfloat/multilingual-e5-large-instruct),
[Ollama](https://ollama.com/), [pypdfium2](https://github.com/pypdfium2-team/pypdfium2),
[WeasyPrint](https://weasyprint.org/) and [SearXNG](https://searxng.org/).

## License

[MIT](LICENSE) — use it, fork it, ship it, sell it. Attribution is the only
condition.

All runtime dependencies are permissively licensed (MIT / BSD / Apache-2.0).
PDF rendering uses [pypdfium2](https://github.com/pypdfium2-team/pypdfium2)
(BSD-3/Apache-2.0) and PDF generation uses
[WeasyPrint](https://weasyprint.org/) (BSD-3).
