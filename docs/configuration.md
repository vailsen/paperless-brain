# Configuration

Every `.env` key, and the model registry behind them. The
[README](../README.md) has the short version needed to get running.

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
| `WHISPER_URL` | | empty | OpenAI-compatible transcription endpoint incl. `/v1`. **Empty = voice memos hidden.** See [Voice memos](voice-memos.md) |
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
