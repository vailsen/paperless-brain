# Proactive System — Phase 0 Diagnostics

Read-only inspection of the codebase on `main` at v0.11.0 (2026-10-26), against
`docs/proactive-architecture.md` and `docs/proactive-tasks.md`. Items marked
**[dev machine]** need the real deployment or data and cannot be settled from the
code alone.

## Verdict: no-go as specified → Phase 0.5 first

Two findings block Phase 1 as written:

1. **A background process cannot act as a user** (D1). User Paperless tokens exist
   only sealed in the browser session and in RAM; by design nothing persists them.
   Every user-scoped job in the spec ("runs with that user's Paperless token") is
   impossible for a separate worker until the owner decides how users grant
   background access.
2. **Retrieval is not permission-filtered at the data layer, and one path leaks**
   (D1). The web-search archive hint returns snippets of every user's documents.

Both go into a **Phase 0.5** (below). Further findings that change later phases are
listed per item and collected in §8.

---

## D1 Permissions

**How retrieval is restricted today.** Not in the data layer. Chroma holds every
user's chunks without an owner field; queries run over all of them, and results are
narrowed afterwards by asking Paperless for the hit IDs with the *user's* token:

- `pipelines/search.py` → `search()`: `chroma.query(...)` unfiltered, then
  `pl.list_documents(ids=…)` with the user client; documents Paperless does not
  return are dropped (`if doc is None: continue`). Same for `search_exact`
  (candidate IDs come from the user client) and `pipelines/similar.py`.
- Document tools (`get_document_details`, `get_document_page_text`,
  `get_document_table`, …) check `list_documents(ids=[doc_id])` with
  `_user_paperless()` before reading the sidecar.
- Deadlines on the dashboard and in `get_actions` go through
  `sidecar_service.filter_visible_actions(…, user client)`.
- Brain and vault hints filter by the `user` metadata field.

The user client is `chat_service._user_paperless()`, built from the
`_current_token` context variable, which `app_ui/pages/chat.py` sets from the session
per turn (Werkbank sets it per run). It fails closed (`NoUserContext`).

**Leak — `_web_search_archive_hint`** (`services/chat_service.py`, ~l. 2104): every
`web_search` call runs `chroma.query(query_texts=[query], n_results=4)` with no
permission filter and puts `Document #id` plus a 160-character chunk snippet into the
tool result. User A's web search can show snippets of user B's private documents.
The Werkbank web researcher uses the same tool. **Fix in Phase 0.5.**

**Quality side effect (not a leak):** because filtering happens after the top-k, a
user with few readable documents among many gets fewer results than requested — the
top 100 chunks can be mostly other users' documents.

**Can a process without a session obtain a user's token? No.**

- `services/session_auth.py`: the token is sealed (AES-GCM, key from
  `STORAGE_SECRET`) into `app.storage.user`, i.e. the browser-cookie-keyed session.
- `werkbank/v2/pipeline.py` keeps it in RAM only ("must never be written"); runs
  survive the tab, not a restart.
- `services/credential_store.py` encrypts each user's credentials (model registry
  with API keys and base URLs, IMAP, CalDAV) **with the user's token**. So without the
  token a worker can also not read the user's chat model — which the spec's model
  resolution falls back to.
- Login (`app_ui/pages/login.py`) exchanges username + password at Paperless
  `/api/token/`. Paperless returns the same stored token every time until it is
  regenerated in Paperless.

**Owner decision needed** (Phase 0.5). Options:

| Option | How | Trade-off |
|---|---|---|
| **A — background access opt-in (recommended)** | In proactive settings the user clicks "Allow background access"; the app stores the *current session token* sealed server-side (`data/background_tokens/{user}.enc`, key from a new `PROACTIVE_SECRET`, not `STORAGE_SECRET`). Revoke = delete file. | Keeps the invariant "every job runs with the user's token". A token regenerated in Paperless stops working → worker marks the user `needs_reauth` and notifies them once. Also unlocks the user's model registry for the chat-model fallback. |
| B — superuser + permission check in code | Worker uses the superuser token and evaluates `owner`/`permissions` (`?full_perms=true`) itself. | Violates the CLAUDE.md invariant; re-implements Paperless' permission model (groups, inherited perms) — fragile. Not recommended. |
| C — users create a dedicated Paperless token | Same as A, but the user pastes a token from Paperless. | Paperless has one token per user, so this is the same token; only more steps. |

## D2 Deadlines and the "218 overdue"

**Where.** Per page, the vision model emits
`actions: [{description, deadline (ISO date), deadline_certain}]`
(`config/extraction_rules/base.py`, rule 3). `pipelines/ingest.py` collects them per
document, `services/action_dedupe.py` collapses one entry per date per document,
`services/sidecar_service.create_index_file()` aggregates all sidecars into
`index.json`, `services/action_review.py` drops LLM-judged junk/duplicates. The
dashboard (`app_ui/pages/dashboard.py`, ~l. 1550) counts every action with
`deadline < today` as **overdue**; its own comment says overdue outnumbers upcoming
about 10:1.

**Root cause (from the code).** There is no notion of "was this ever mine to act on"
or "already done":

1. **Archive ≠ inbox.** Most documents are scanned long after the fact. An invoice
   "pay by 2019-03-01" ingested in 2026 is a past date the moment it is extracted, and
   it stays "overdue" forever.
2. **No `actionable` / `kind`.** The prompt tries to exclude informational dates, but
   the schema has no field for it, so everything with a date is a deadline.
3. **No done state.** Only per-user "hide" on the dashboard (`hidden_actions`).
4. **Year guessing.** Dates without a year are derived from context
   (`deadline_certain=false`), still counted.

The spec's §9 rule (only `actionable == true and date >= today` produces a finding,
plus a verbatim quote check) addresses 1–2 directly. **[dev machine]** confirm the
218 against `index.json`: how many are `< ingestion date` (case 1) vs. later.

**Overlap to decide:** the existing per-page `actions` pipeline and the spec's
`doc_analysis` deadlines would be two extractions of the same thing. Phase 6 should
either replace the dashboard source with `doc_analysis` or derive one from the other —
not run both.

## D3 Telegram

**There is no Telegram code in this repository** — no bot, no dependency, two mentions
in i18n docs only. The Hermes bot ran in its own LXC, which the owner has **deleted**
(2026-10-26). Consequences:

- No existing poller, no 409 conflict to migrate around: the worker is the sole
  poller from day one, with a **new bot token** (`TELEGRAM_BOT_TOKEN`).
- Phase 3's "move Hermes polling into the worker" and the acceptance item "existing
  Hermes features still work" no longer apply (adjusted in `docs/proactive-tasks.md`).
- New dependency: `python-telegram-bot` (or plain `httpx` against the Bot API —
  smaller; decide in Phase 3).

## D4 IMAP

`services/imap_service.py` (used by the `search_emails` tool and the Werkbank
`comms_researcher`):

- **Session-free:** `search_emails(host, port, username, password, …)` takes explicit
  credentials. The per-user credentials come from the token-encrypted credential store
  in `chat_service._tool_search_emails`; the proactive system uses the system mailbox
  from `.env`, so that is no obstacle.
- Blocking `imaplib` runs in `asyncio.to_thread` — the executor pattern the spec asks
  for.
- Reusable: modified-UTF-7 folder names (`imap_utf7_encode/decode`, `folder_to_wire`),
  header/charset decoding (`_decode_header`, `_decode_part`, `_html_to_text`).
- **Missing for Phase 5:** the module is read-only (`select(..., readonly=True)`).
  No `UNSEEN` search helper, no fetch of `In-Reply-To`/`References`, no `CREATE`, no
  `MOVE` (or `COPY` + `\Deleted` + `EXPUNGE` fallback). Add these in a separate
  module so the read-only guarantee of the chat tool stays visible.

## D5 Vault writes

- **Paths:** `vault/paths.py` — `vault_path(user) = VAULT_ROOT/{user}`, subfolders from
  settings (`PaperlessBrain Memory`, `PaperlessBrain Memos`). `ensure_vault_dir`,
  `align_vault_perms` (group-writable, owner of the parent), `atomic_write_text`
  (temp + rename, preserves owner). Reuse for `PaperlessBrain Findings`.
- **App-written files and change detection:** memos/brain facts are written →
  embedded → **committed inline** under the per-user lock, so the next
  `sync_user()` diff does not see them again — that is how a write-then-detect loop is
  avoided. Files written by the note editor are *not* committed and are picked up by
  the next sync (intended).
- **Conflict detection is by sha256, not mtime** (`vault/notes.save_note`: compare
  hash under the lock, three-way merge on mismatch). The spec's "mtime check" should
  use this existing mechanism.
- **Identity key:** the spec's `psage_id` is the legacy name; the current key is
  `pbrain_id` (`vault/frontmatter.py`, `ID_KEY`, legacy accepted on read).

**Problems for a separate worker process:**

1. **Locks are per process.** `vault/locks.py` is a dict of `asyncio.Lock`. A worker
   writing into a vault (or committing) is not serialised against the app's
   `sync_user()` (`git add -A` … `commit`) — a worker write in that window is committed
   without being embedded, and two `git` processes collide on `index.lock`. Needs a
   cross-process lock (e.g. `fcntl.flock` on a per-user lock file next to the git dir)
   used by both app and worker, before Phase 4.
2. **Findings would be embedded immediately.** Everything outside the brain subfolder
   goes into the `vault` collection on the next sync — including `held`/`dismissed`
   findings and `_interests.md`, which Phase 9 says must never be retrieved. The
   exclusion has to exist from Phase 4 on. Existing lever: the `dont_ingest: true`
   frontmatter flag (sync drops the note's chunks); or exclude the findings folder by
   path until Phase 9.

## D6 Inference and power

`services/ollama_watchdog.py` (66 lines), started in `main.py` only if
`OLLAMA_SSH_USER` is set:

- **Activity** = `touch()`, called by vision ingestion (`services/vision.py`), the
  Ollama chat backend (`chat_service`, ~l. 3749) and the Werkbank local lane
  (`werkbank/llm_lane.py`). UI sessions alone do not count. Cloud/API-lane calls do
  not count.
- **Shutdown** only if the host was woken through the button (`on_wol()` sets
  `_wol_active`): a host that was already on is never shut down. After
  `OLLAMA_IDLE_SHUTDOWN_MINUTES` (30) without `touch()` → `ssh … sudo shutdown -h now`.
- **State lives in module globals of the app process.** A separate worker's
  `touch()`/`on_wol()` would update its own copy:
  - worker wakes the host → the app never learns it → no auto-shutdown at all;
  - app woke the host, worker sweeps → the app sees no activity → **shutdown
    mid-sweep** (the spec's caveat, confirmed).
  - Also today: an app restart forgets `_wol_active` → a woken host stays on.
  **Fix (Phase 2):** move `_wol_active` / `_last_activity` / `_wol_host` into a
  small shared state file (e.g. `data/power_state.json`, atomic writes) read by the
  app's watchdog and written by both processes. The worker still adds no shutdown
  logic.
- **WoL path:** inline in the dashboard (`wake_ollama()` closure, ~l. 1294): builds
  the magic packet from `OLLAMA_HOST_LAN_MAC_ADDRESS_WOL` and a broadcast address from
  `_wol_broadcast(username, token)` — which falls back to the **user's model registry**
  (needs the token) when `OLLAMA_SERVER` is unset. Extract into a service function; for
  the worker, require `OLLAMA_SERVER` (operator setting) so no token is needed.
- **`/v1/models`:** not used anywhere. The only model-list call is Ollama's
  `/api/tags` (`chat_service`, ~l. 82). Ollama and llama-swap both serve
  `/v1/models`. **[dev machine]** is llama-swap in front of the GPU host?

**Design gap:** the spec assumes one "active backend" with model names differing per
user. This app has no single backend — every user has their own model registry
(backend, base URL, key) in the token-encrypted store. Recommended: define the
proactive backend explicitly in `.env` (`PROACTIVE_LLM_BASE_URL`,
`PROACTIVE_LLM_API_KEY`, backend type) and let `PROACTIVE_MODEL_*` and the per-user
choice pick model names on that backend. "Cloud" is then a property of that one
endpoint, and the allowlist can be checked against its `/v1/models`.

## D7 Werkbank

v2 is live; all seven phases of `docs/werkbank-tasks.md` are marked done
(BRIEFER, PLANNER/PLAN_CRITIC, runners, FACT_CRITIC, scheduler, CONTRADICTION_CHECKER/
WRITER, UI). The spec's dependency ("Werkbank phases 1–3") is satisfied; Phase 8 is not
blocked by the Werkbank. Caveat: Werkbank runs hold the user token in RAM only — a
Werkbank task started by the *worker* needs the background token from Phase 0.5.

---

## 8. Other findings that change later phases

1. **Second process, second embedding model.** Importing `services.clients` loads
   the ~2.2 GB embedding model and opens Chroma. A worker doing the same doubles RAM,
   and Chroma's embedded `PersistentClient` is not meant to be written from two
   processes. Recommendation: the worker never imports `services.clients`; anything
   needing Chroma (triage's "similar documents", Phase 9 embedding) goes through a
   small internal HTTP API on the app, or the worker only writes files and the app's
   sync embeds them.
2. **Two preference stores.** User settings now live in `app.storage.general`
   (`services/user_prefs.py`); the spec adds `user_prefs` in `proactive.db`. The
   worker cannot safely read NiceGUI's storage file. Keep proactive settings in
   `proactive.db` (as the spec says) and name the table `proactive_prefs` to avoid
   confusion with `services/user_prefs.py`.
3. **DB location.** `PROACTIVE_DB_PATH=/var/lib/…` is outside the Docker volumes.
   Default it to `data/proactive.db` (the `./data` volume both containers mount).
4. **Deployment.** `docker-compose.yml` has one service. The worker is a second
   service from the same image (different command), sharing `./data`, `./vaults` and
   — for Phase 2's power state — the same data folder.
5. **Language.** Notification templates through the existing translator need the
   user's language; it lives in `app.storage.general` (see 2). Copy it into
   `proactive_prefs` when the user opts in or changes it.

## Phase 0.5 — proposed (before Phase 1)

1. **Fix the archive-hint leak:** filter `_web_search_archive_hint` hits through the
   user client (`list_documents(ids=…)`) before any snippet is emitted; without a user
   context, emit nothing. Test: a document the user cannot read never appears.
2. **Owner decision on background access** (D1 table) and, for option A: sealed
   per-user token store + opt-in/revoke in settings + `needs_reauth` handling.
3. **Cross-process vault lock** (D5.1) used by app and worker.
4. **Optional, recommended:** owner field in Chroma document metadata, so retrieval
   can be filtered before top-k (fixes the recall side effect; needs a re-ingest or a
   metadata backfill from Paperless).

## Decisions (owner, 2026-10-26)

1. **Background access: option A.** Implemented in Phase 0.5
   (`services/background_access.py`): sealed per-user grant in
   `data/background_access/`, key derived from `STORAGE_SECRET` with its own HKDF
   label and bound to the username (no separate `PROACTIVE_SECRET` — same host, same
   threat model as the session token). Login refreshes an existing grant; a 401 marks
   it `needs_reauth`. The opt-in/revoke UI comes with the proactive settings in
   Phase 1 (they only exist when `PROACTIVE_ENABLED=true`).
2. **Model choice:** the operator decides in `.env` whether users may choose at all
   (`PROACTIVE_MODEL_FIXED`), from which models (`PROACTIVE_MODEL_ALLOWLIST`) and
   whether cloud models are allowed (`PROACTIVE_CLOUD_ALLOWED`). With option A the
   worker can read each user's own model registry, so the choice is from the user's
   own models, filtered by those knobs; "cloud" = a model whose service is not on
   the local lane. The fixed/default model runs on `OLLAMA_SERVER`. No separate
   proactive endpoint variable.
3. **Inference:** one Ollama instance, no llama-swap (multi-GPU maybe later). Model
   listing via Ollama (`/api/tags` or `/v1/models`); every model change is an unload
   + load, so grouping jobs by model stays.
4. **Deadlines:** open until Phase 6. Recommendation: `doc_analysis` as a text-only
   pass over the sidecar text (no new vision run), and the dashboard switches to it,
   so there is one deadline source.

## Phase 0.5 — status

- [x] Archive-hint leak fixed: hits are narrowed through the user's own Paperless
  client before any snippet is shown; no user context → no hint
  (`tests/test_web_search_archive_hint.py`, which fails on the old code).
- [x] Cross-process vault lock: `vault/locks.py` = `asyncio.Lock` + `flock` on
  `data/vault_locks/{user}.lock`; every existing `async with get_user_lock(…)`
  caller is covered unchanged (`tests/test_vault_locks.py`, incl. a real second
  process and a cancelled waiter).
- [x] Background access store (decision 1).
- [ ] Optional: owner field in Chroma metadata for pre-top-k filtering — not done;
  revisit if recall for users with few readable documents becomes a problem.
