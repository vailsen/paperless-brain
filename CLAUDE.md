# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**PaperlessBrain** — AI-powered frontend for [Paperless-ngx](https://docs.paperless-ngx.com/). Built with NiceGUI + FastAPI. Lets users chat with their document archive (invoices, contracts, letters) via Claude or Ollama LLMs, with full tool-call agentic loop.

## Running

```bash
# Local dev
python main.py
# App runs at http://0.0.0.0:8080

# Deploy: tag a release, CI publishes the image, the server pulls it
git tag vX.Y.Z && git push origin main --tags   # .github/workflows/release.yml
ssh <server> "cd /root/paperlessbrain && docker compose pull && docker compose up -d"
```

## Dependencies

Single source of truth: `pyproject.toml` (direct deps only, range pins;
name `paperless-brain`, version from `config/version.py` via hatchling).
The old `requirements*.txt` files are gone.

```bash
# GPU machine (dev)
pip install -e ".[crawl,i18n]"

# CPU-only (LXC/Docker) — +cpu torch wheel outranks the CUDA build on PyPI
pip install --extra-index-url https://download.pytorch.org/whl/cpu -e ".[crawl,i18n]"
playwright install chromium   # only needed with the [crawl] extra

# Docker
docker build -t paperless-brain .                    # full
docker build --build-arg LEAN=1 -t paperless-brain . # no crawl4ai/chromium
docker compose up -d
```

Extras: `[crawl]` = crawl4ai + headless Chromium (JS-heavy page fetching —
app degrades to trafilatura-only without it); `[i18n]` = pybabel workflow.

## Configuration

All settings via `.env` file (loaded by `config/settings.py` via pydantic-settings). Required keys:

- `APP_PATH` — absolute path to repo root (with trailing slash)
- `PAPERLESS_URL`, `PAPERLESS_SUPERUSER_TOKEN` — Paperless-ngx API
- `IGNORE_INBOX_TAG_AT_SYNC` — tag name to skip during sync
- `EMBEDDING_MODEL`, `CHROMA_PATH`, `CHROMA_COLLECTION`, `EXTRACTION_SIDECAR_PATH`, `THUMB_PATH`
- `OLLAMA_SERVER`, `OLLAMA_INGEST_MODEL` — *optional*, vision LLM for ingestion
  (both also settable in Settings > Processing, which wins). Chat and Werkbank
  never read them — those use per-user models with their own `base_url`.
  `OLLAMA_SERVER` additionally names the host the WoL/shutdown buttons control;
  without it that is inferred from the first local-lane model in the registry.
- No provider API key belongs in `.env`. Every chat/research/vision model is managed
  per user in the UI (Settings > AI Models, `services/model_registry.py`) with its own
  backend (`anthropic` / `openai_compatible`), base URL, key and model id — which is
  what makes AI gateways (OpenRouter, Requesty, LiteLLM, Portkey …) work with no code.
- `SEARXNG_HOST` — self-hosted SearXNG instance
- `STORAGE_SECRET` — NiceGUI session secret

## Architecture

### Data flow

1. **Sync** (`pipelines/paperless_db_sync.py`): compares Paperless-ngx doc list against ChromaDB; calls `ingest_document` for new docs, `delete_document` for removed ones.
2. **Ingest** (`pipelines/ingest.py`): downloads PDF → extracts pages as images via pypdfium2 → sends each page to Ollama vision model → stores extracted JSON sidecar + ChromaDB embeddings.
3. **Sidecar** (`services/sidecar_service.py`): per-document JSON files at `{EXTRACTION_SIDECAR_PATH}/{doc_id}.json` — contain full summary, page texts, actions/deadlines, cross-references, tables.
4. **Cross-ref index** (`services/cross_ref_index.py`): built from all sidecars at startup; maps invoice numbers / file references to document IDs for the `get_related_documents` tool.
5. **Chat** (`services/chat_service.py`): agentic loop with `MAX_ITERATIONS=16`. Two backends: `ClaudeChatBackend` (Anthropic API streaming) and `OllamaChatBackend` (httpx POST). Both emit the same `ChatEvent` union type. Tools are defined in `TOOL_DEFINITIONS` (list of dicts, Claude format); converted to Ollama format via `_to_ollama_tools()`.

### Key singletons (`services/clients.py`)

Module-level singletons created at import time — one `PaperlessClient`, one `ChromaClient` for documents, one `ChromaClient` for brain facts (collection `"brain"`), `BrainService`, `SidecarService`, `CrossRefIndex`, `ThumbnailService`, `OllamaVisionClient`.

`get_session_paperless()` returns a user-scoped `PaperlessClient` using the session token from NiceGUI storage (falls back to admin token outside browser context).

### Brain / long-term memory

`services/brain_service.py` + `BrainService` stores user facts in the `"brain"` ChromaDB collection. Per-user isolation via `user` metadata field; `common=True` facts visible to all users. The chat `search` tool automatically queries brain for hints and prepends them to search results.

### UI

- NiceGUI 3.x, pages in `app_ui/pages/` (each imports `page_layout` + `require_auth` from `app_ui/layout.py`)
- Pages: `login`, `dashboard`, `browser` (document grid), `chat`, `brain` (fact management), `settings`
- Dialogs: `app_ui/document_dialog.py`, `app_ui/cluster_dialog.py`
- **NiceGUI pattern**: use `@ui.refreshable` for dynamic content in plain columns. `QTabPanels`/`element.clear()` silently fails.
- Chat events stream via `AsyncGenerator[ChatEvent, None]` — UI iterates with `async for` and updates reactively.

### Chat tool results that trigger UI dialogs

Three tools don't execute server-side — they return a special event that the UI page (`app_ui/pages/chat.py`) intercepts to open a dialog:
- `trigger_docx_generation` → `DocxRequestEvent` → DIN-5008 letter dialog
- `create_email` → `EmailRequestEvent` → email template dialog
- `generate_chat_pdf` → `PdfSaveRequestEvent` → save-to-Paperless dialog

### Credential store

IMAP and CalDAV credentials per user stored via `services/credential_store.py`, encrypted with the user's session token. Chat tools load them at call time.

### Ollama watchdog

`services/ollama_watchdog.py`: sends Wake-on-LAN to the Ollama server MAC on first use, shuts it down after `OLLAMA_IDLE_SHUTDOWN_MINUTES` of inactivity. `touch()` resets the idle timer.

## Deployment target

LXC container (see `.deploy.env` for target host), remote path from `DEPLOY_REMOTE_DIR`, systemd service `paperlessbrain`. Server uses Python 3.14.

---

## Feature: KI-Werkbank (autonomes Agenten-Modul)

Vollständige Spezifikation: `docs/werkbank-architecture.md`
Bauplan: `docs/werkbank-tasks.md`

### Nicht verhandelbare Prinzipien

- **Der Orchestrator ist deterministischer Python-Code, kein LLM.** Steuerung
  (Reihenfolge, Status, Retries, Fehler) ist Python; kognitive Arbeit nur innerhalb
  der Rollen. Das LLM dirigiert sich nie selbst.
- **Scoped Prompts + Tool-Subsets pro Rolle.** Kein Agent bekommt alle Tools —
  Tool-Überladung degradiert lokale Modelle.
- **Isolierte Kontexte + Kompaktierung.** Nachgelagerte Sub-Tasks sehen nur die
  kompaktierten Summaries der Abhängigkeiten, nie rohe Tool-Outputs.
- **Review-vor-Persist.** Ergebnisse landen erst nach manueller Freigabe in
  Paperless. Niemals Web-/abgeleitete Inhalte automatisch in den `documents`-Index —
  das würde die Verlässlichkeit des Dokumenten-Gehirns untergraben.
- **User-Scoping auf JEDER Repository-Query.** `user_id` liegt auf allen drei
  Tabellen (auch denormalisiert auf `agent_subtasks` als Defense-in-Depth).

### Architektur-Entscheidungen mit Begründung

- **Persistenz = SQLite (WAL), nicht Chroma.** Kanban-State ist strukturierte
  relationale Daten ohne Ähnlichkeitsbezug. Chroma bleibt für Vektoren.
- **Nebenläufigkeit = per-Backend-Lanes.** Lokale Ollama-Instanz = `Semaphore(1)`;
  Claude-API = `Semaphore(N)`. Limitierende Ressource ist die GPU, nicht "ein Task
  global". v1: Nebenläufigkeit nur zwischen Tasks.
- **Sub-Tasks = DAG via `depends_on`.** Subsumiert die lineare Kette. Ready-Set-Walk:
  ausführbar, wenn alle Abhängigkeiten `DONE`.
- **System-Rollen (Planner/Splitter/Critic/Synthesizer) vs. Worker-Archetypen.**
  Erstere: Prompts in Settings, fest. Letztere: Nutzer-CRUD in SQLite, Defaults
  `retriever` + `researcher`.
- **Splitter-Robustheit = Generierungs-Constraint + Validierung.** Ollama `format`
  mit JSON-Schema (bzw. Claude Tool-Use) erzwingt valide Struktur; danach 8-stufige
  semantische Validierung; Retry-mit-Feedback; Einzel-Sub-Task-Fallback.
- **Failed-Policy = weitermachen.** Gescheiterter Sub-Task → Platzhalter, Synthesizer
  vermerkt die Lücke. Kein Gesamt-Abbruch.
- **Stop/Resume durch Persistenz.** Jedes Sub-Task-Ergebnis wird sofort gespeichert;
  Stop pausiert nach aktuellem Sub-Task, Resume macht beim ersten nicht-`DONE` weiter.

### Projektspezifische Fakten (gelten ggf. projektweit)

- Embedding-Modell: `intfloat/multilingual-e5-large-instruct` — **immer mit
  Instruct-Prefixes** verwenden (Query- vs. Passage-Prefix), sonst sinkt die
  Retrieval-Qualität und Distanzen werden untrennscharf.
- Lokale Inferenz über Ollama; per-User Claude-API-Key liegt verschlüsselt pro User
  (vorhandene Datei), nie Klartext in SQLite.
- Bestehende Komponenten anbinden, nicht neu bauen: LLM-Client, Tool-Registry,
  Chroma-Zugriff, Paperless-Client, Embedding-Service, Settings, Auth/User-Kontext.

# CLAUDE.md — Vault-Backed Memory section

> Append to the project `CLAUDE.md`. These are hard invariants for the vault/memory
> subsystem. Full rationale in `vault-architecture.md`; task breakdown in `vault-tasks.md`.

## Source of truth

- **Markdown files on disk are the source of truth for memory.** ChromaDB is only the index.
  Never treat a Chroma entry as authoritative content — it can always be rebuilt from the `.md`.
- This mirrors the document invariant: Chroma holds the index, the external store holds content.

## Two collections — never merge

- `brain` = agent-curated atomic facts. 1 fact = 1 `.md` = 1 embedding (no chunking).
  Lives in `<vault>/PaperSage Memory/`.
- `vault` = the user's own notes (knowledge base). 1 file = N chunks (markdown-aware chunking).
  Everything in the vault *outside* the brain subfolder.
- Route by path prefix: under `PaperSage Memory/` → `brain`, else → `vault`.
- Separate tools: `brain_search` (recall what the agent knows) vs `vault_search` (search the
  user's notes). Do not collapse them.

## Identity

- `pbrain_id` (UUID in frontmatter) is the **stable identity**, not the path. It survives
  renames/moves. Brain entries are keyed by `pbrain_id`; vault by `pbrain_id:chunk_index`.
  The legacy key `psage_id` is still accepted on read and migrated to `pbrain_id` on write
  (`vault/frontmatter.py`: `ID_KEY`, `get_id()`, `ensure_pbrain_id()`); a one-time reindex
  in `vault/sync.py` (`_reindex_user`, marker-guarded) rewrites the key + re-embeds.
- **Always store `path` in Chroma metadata too** — deletions are driven by git's `D <path>`
  and the file's frontmatter is already gone, so delete via `where path == <path>`.
- A brain file lacking `pbrain_id` → assign one and write it back to frontmatter (idempotent).

## Change detection = git

- Git is the change detector, the manifest, and the audit trail. **Do not write custom hashing
  and do not add a SQLite manifest** for sync.
- Use plain `git` via `subprocess` (`git -C <vault>`). No GitPython.
- `.gitignore` ignores everything except `*.md` (keep Obsidian attachments out of the repo).
- `.git/` must be excluded from Remotely Save (deployment note in settings help text).
- Sync cycle: `diff --name-status HEAD` → route → embed/delete → **commit last**. The commit is
  the "processed up to here" bookmark; crashing before it just re-processes idempotently.

## Sync triggering

- **On user interaction only. No background scheduler. No time-based sync.**
- Run `sync_user(username)` at the start of a turn before any brain/vault retrieval.
- Guard with a **per-user `asyncio.Lock`** + **at most once per turn** (per-turn flag / short
  cooldown). Also expose a manual "Sync now" dashboard button.
- **Agent brain writes are synchronous** (write `.md` → embed → commit inline) so the agent
  sees its own new memory immediately. Brain writes take the same per-user lock.

## Topology

- One authoritative copy via the existing per-user bind mount `/mnt/vaults/<username>`.
  **Do not sync a local copy** (that is bidirectional sync — forbidden complexity class).
- The WebDAV endpoint Remotely Save targets **must serve the same directory** PaperSage mounts.
- Atomic writes (temp + rename) within the mount. Ignore `*.conflict.md`.

## Tools & UI

- Brain **read** tools (`brain_search`) stay byte-identical. New `vault_search` mirrors them.
- Brain **write** tools keep their signatures but are re-implemented on the file+git+embed
  primitive (no direct Chroma upsert).
- Remove the old Chroma-direct memory UI; replace with a slim **file-backed** brain viewer
  (brain only). The vault knowledge base is managed in Obsidian, not in the UI.

## Embedding

- Reuse the existing `multilingual-e5-large-instruct` helper; it already handles the instruct
  prefixes. Passages embedded as-is; `*_search` queries use the instruct query prefix. Do not
  reinvent the embedding path.

## Do-not list

- ❌ SQLite manifest / custom hashing for sync (git already does this).
- ❌ Bidirectional sync / local working copy.
- ❌ Merging `brain` and `vault` collections.
- ❌ Time-based / scheduled sync.
- ❌ GitPython or other new heavy deps where `subprocess` + PyYAML suffice.
- ❌ Removing the memory UI entirely.

# Localization (i18n)

> Append this section to `CLAUDE.md`. It is the authoritative spec for adding
> multi-language support to PaperSage. Work it **one phase at a time** with
> `/clear` between phases. Do not deviate from the invariants or the do-not list.

## Goal

English is the source language (since the open-source language inversion,
2026-07-18); German is a first-class translation. English is the default and
the fallback. The user picks their language in user settings; the choice
persists in server-side user storage.

## Mechanism (fixed decisions — do not re-litigate)

- **gettext** is the translation mechanism. The English source string **is** the
  `msgid`. This means English needs **no** catalog file: when no translation is
  found, gettext returns the `msgid`, which is already the correct English text.
- **pybabel** drives the extract → translate → compile workflow.
- The **mapping file** the human (or Claude Code) edits is the per-language
  `.po` file, e.g. `locales/de/LC_MESSAGES/messages.po`. English `msgid` on top,
  German `msgstr` below. That `.po` is the single place where the en→de mapping
  lives.
- Module-level constants that feed `_()` at render time (e.g. `NAV_ITEMS`) are
  marked with `N_()` from `i18n.py` — a no-op that pybabel extracts (it is in
  Babel's default keywords).
- Language preference is stored in **`app.storage.user["language"]`** — the same
  encrypted server-side user storage every other setting uses. **No new settings
  table, no new persistence layer.** This is consistent with the
  minimal-persistence principle.
- The translator is resolved **per page / per render** from the current user's
  language. There is **no global `_`**. NiceGUI runs a single async worker; a
  global translator installed via `gettext.install()` would leak one user's
  language into every other concurrent session (race condition). This is the
  single most important invariant in this document.

## File layout

```
project_root/
├── babel.cfg                                  # extraction config (one line)
├── i18n.py                                    # translator + language registry
└── locales/
    ├── messages.pot                           # template (all msgids, generated)
    └── de/
        └── LC_MESSAGES/
            ├── messages.po                    # DE mapping — edit this
            └── messages.mo                    # compiled — runtime reads this
```

There is deliberately **no `locales/en/`** directory. English is the `msgid`
fallback.

`i18n.py` may live at the project root or under `config/`; adjust `LOCALES_DIR`
accordingly. Keep it to **one** module so the language registry has a single
source of truth.

## Reference implementation

### `babel.cfg`

```ini
[python: **.py]
```

(Dot-prefixed directories — `.venv`, `.claude`, `.nicegui` — are skipped by
pybabel automatically. Do not add `--ignore-dirs`; it is buggy with multiple
patterns. If a normal source folder must be excluded later, add an
`[ignore: folder/**]` line **above** the `[python: ...]` line.)

### `i18n.py`

```python
import gettext
from pathlib import Path
from nicegui import app

LOCALES_DIR = Path(__file__).parent / "locales"   # adjust if i18n.py is not at root
DEFAULT_LANG = "en"

# SINGLE SOURCE OF TRUTH for available languages: code -> display name.
# To add a language: add one entry here, then run the pybabel init/translate/
# compile cycle for that code. Nothing else changes.
SUPPORTED_LANGUAGES: dict[str, str] = {
    "en": "English",
    "de": "Deutsch",
}

def get_translator():
    """Return the gettext callable for the current user's language.

    MUST be called inside a page function or event handler — it reads
    app.storage.user, which only exists once a client/session is connected.
    """
    lang = app.storage.user.get("language", DEFAULT_LANG)
    if lang == DEFAULT_LANG:
        return gettext.NullTranslations().gettext        # msgid IS English
    try:
        return gettext.translation("messages", LOCALES_DIR, languages=[lang]).gettext
    except FileNotFoundError:
        return gettext.NullTranslations().gettext        # graceful fallback to English
```

### Usage in a page

```python
@ui.page("/documents")
def documents_page():
    _ = get_translator()                  # first line of every page builder
    ui.label(_("Upload document"))
    ui.button(_("Save"))
    ui.notify(_("{n} documents loaded").format(n=count))   # NEVER _(f"...")
```

For reusable component/helper functions that build UI, either call
`get_translator()` inside them (they run within the page context) or accept the
translator as a parameter. Never cache `_` at module level.

### Language selector in user settings

Render this **first, at the top** of the settings page.

```python
from i18n import SUPPORTED_LANGUAGES
from nicegui import app, ui

def language_setting():
    current = app.storage.user.get("language", DEFAULT_LANG)

    def on_change(e):
        app.storage.user["language"] = e.value
        ui.navigate.reload()              # re-render everything with new language

    ui.select(
        options=SUPPORTED_LANGUAGES,      # {'en': 'English', 'de': 'Deutsch'}
        value=current,
        label="Sprache / Language",
        on_change=on_change,
    )
```

**Switching language = write storage + reload the page.** Do not attempt to
live-update already-rendered labels reactively; a reload is correct, simple, and
re-runs every `get_translator()` cleanly.

## Workflow commands

```bash
# 1. Extract all _( ) strings from the codebase into the template
pybabel extract -F babel.cfg -o locales/messages.pot .

# 2. Create a language catalog (run ONCE per language, e.g. German)
pybabel init -i locales/messages.pot -d locales -l de

# 2b. On later runs, UPDATE the existing catalogs instead of init
pybabel update -i locales/messages.pot -d locales

# 3. (translate: fill every msgstr in locales/de/LC_MESSAGES/messages.po)

# 4. Compile .po -> .mo so the runtime can read it
pybabel compile -d locales
```

The loop is: wrap more strings → `extract` → `update` → translate the new
entries → `compile`. Repeatable any number of times; only new/changed entries
need attention.

## What counts as translatable

**Wrap** (human sees it in the UI): button captions, labels, menu items, tab
titles, table headers, tooltips, placeholder text, dialog titles/bodies,
`ui.notify` messages, validation/error text **shown to the user**, empty-state
text. These live mostly in `app_ui`, but also wherever `services/`, `pipelines/`,
or `werkbank/` produce a message that surfaces in the UI or in a user-facing
Telegram/Hermes reply.

## Do NOT

- **Do NOT** use `gettext.install()` or any global/module-level `_`. Resolve per
  page via `get_translator()`.
- **Do NOT** wrap: log messages, exception text meant only for logs, internal
  dict/enum keys, config keys, Paperless-ngx custom-field names, API identifiers,
  `pbrain_id`/frontmatter keys, or anything not rendered to a human.
- **Do NOT** put an f-string inside `_()`. Use
  `_("{n} items").format(n=n)` — never `_(f"{n} items")`. f-strings produce a
  different literal per call and nothing becomes translatable.
- **Do NOT** wrap strings evaluated at **import time** (module-level constants,
  class attributes, default arg values). There is no user context then. Move the
  `_()` call to the point of render. If a module-level dict maps state→German
  label, store the keys and translate at render instead.
- **Do NOT** create a `locales/en/` catalog. English is the `msgid`.
- **Do NOT** add a settings table or any new persistence for the language. It
  lives in `app.storage.user`.
- **Do NOT** edit `.mo` files by hand. Edit the `.po`, then `compile`.
- **Do NOT** live-update the UI on language change; set storage and
  `ui.navigate.reload()`.
- **Do NOT** translate placeholders (`{n}`, `{name}`), proper nouns (PaperSage,
  Paperless-ngx, Obsidian, Telegram, ChromaDB), or technical tokens.

## Auto-translation guidance (Phase 2)

When filling English `msgstr` values:

- Translate naturally for a professional document-management UI, not literally.
- Buttons/actions: imperative English ("Save", "Upload", "Delete").
- Keep terminology consistent across the whole catalog (always "Document" for
  "Dokument", always "Settings" for "Einstellungen", etc.).
- Preserve every `{placeholder}` exactly, including position adjustments natural
  to English word order.
- Leave proper nouns and technical tokens unchanged.
- Resolve any `fuzzy` flags pybabel adds; do not ship fuzzy entries unreviewed.

## Plurals (deferred)

Singular/plural ("1 Dokument" vs. "3 Dokumente") needs `ngettext`, not plain
`gettext`. For now, wrap such strings as ordinary `_()` calls and leave a
`# TODO i18n-plural` comment at each site. A later phase introduces `ngettext`
and the `Plural-Forms` header. Do not build the plural mechanism now.

## Adding a language later

1. Add the entry to `SUPPORTED_LANGUAGES` in `i18n.py` (e.g. `"fr": "Français"`).
2. `pybabel init -i locales/messages.pot -d locales -l fr`
3. Translate `locales/fr/LC_MESSAGES/messages.po`.
4. `pybabel compile -d locales`

No code changes beyond step 1. The selector picks it up automatically.

---

## Phased tasks

### Phase 0 — Scaffold the mechanism on ONE page (end-to-end)

Prove the full loop works before touching the rest of the app.

- Create `babel.cfg`, `i18n.py`, and the `locales/` directory.
- Confirm `ui.run(..., storage_secret=...)` is set (required for
  `app.storage.user`). If absent, add it.
- Add `language_setting()` to the top of the user settings page.
- Wrap **only** the strings on one representative page (e.g. the documents page).
- Run: extract → init en → translate just those few strings → compile.

**Acceptance:**
- Selecting English in settings reloads and flips that one page to English.
- Selecting German shows the original German text with no `.mo` needed.
- A fresh session with no stored preference defaults to German.
- No other page is affected yet.

### Phase 1 — Full string sweep

- Find and wrap every UI-visible string across `app_ui`, plus user-facing
  messages emitted from `services`, `pipelines`, and `werkbank`.
- Apply the do-not list rigorously (no logs, keys, import-time constants).
- Add `# TODO i18n-plural` at any singular/plural site.

**Acceptance:**
- `pybabel extract` collects all wrapped strings into `messages.pot`.
- No hardcoded German remains in any rendered UI surface.
- The app still runs fully in German (fallback path) with no regressions.

### Phase 2 — Translate and compile

- `pybabel update -i locales/messages.pot -d locales` to fold in all new strings.
- Fill **every** English `msgstr` per the auto-translation guidance.
- Resolve all `fuzzy` flags.
- `pybabel compile -d locales`.

**Acceptance:**
- The entire app switches de↔en via the settings selector.
- No empty `msgstr` remains (an empty one would render German in the English UI).
- All `{placeholders}` are intact; proper nouns untouched.
- No fuzzy entries ship unreviewed.

### Phase 3 — Plurals & further languages (later, optional)

- Introduce `ngettext` at the `# TODO i18n-plural` sites.
- Document/exercise the "Adding a language later" steps once with a third locale.

**Acceptance:**
- Count-dependent strings read correctly for n=1 and n>1 in both languages.
- A third language can be added with only the four documented steps.

---

# UI visual discipline

Hard invariants for anything rendered to a user. Full rationale and the phased
work that established them: `docs/ui-polish-tasks.md`.

- **Colour encodes state, not identity — with one exception.** Tag chips inherit hue from
  Paperless because the user assigned it and it enables cross-document grouping. Everything
  else in the UI (icons, borders, buttons, badges) is neutral unless it represents a state the
  user must react to.
- **Tag row = Paperless data only.** Anything PaperSage generates (action flags, ingestion
  status, confidence) renders outside the tag row with its own treatment.
- **One accent meaning.** Purple = the user's own input and the active selection. Nothing else.
- **Icons inherit text colour.** No coloured icons anywhere. Active state is the only
  differentiator in navigation.
- **Sentence case everywhere.** No ALL CAPS labels, no emoji in UI chrome.

Practical consequences:

- Tag chips go through `app_ui/tag_style.py` (`render_tag_chips` / `tag_chip`), never a raw
  `ui.badge(tag, color=...)`. The helper preserves hue and clamps saturation and lightness so
  no tag can outshout another, caps cards at 4 chips plus a muted `+N`, and sorts
  alphabetically so the visible four are stable across renders.
- Theme tokens live in `app_ui/theme.py`: `--c-text` / `--c-text-2` / `--c-text-muted` for the
  neutral ramp, `--c-accent` for the one accent meaning, `--c-warn` / `--c-warn-bg` for
  "you must react to this". Reach for a token, not a hex.
- Card action icons use the `card-action-btn` class (muted at rest → `--c-text-2` on card
  hover → `--c-text` on icon hover), and `is-pinned` for the active-selection accent.
