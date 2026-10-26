# Proactive System — Architecture

Status: design complete, implementation not started.
Scope: notifications (Telegram + email), deadline reminders, document triage, topic dossiers, relevance learning.
Dependencies: Werkbank (only for dossiers / deep-dives, phase 8+). Everything before that runs without Werkbank.

---

## 1. Goals and non-goals

**Goals**
- Inform users about things in their documents when they are not using the app: upcoming actionable deadlines, and optimization insights (contracts, insurance, retirement savings, depot, …).
- Follow topics across years (e.g. a Bausparvertrag with dozens of letters) via a rolling dossier per topic.
- Learn per user which topics matter, push only those; hold back the rest and surface them in quiet periods.
- Let users react (Interessant / Später / Nie wieder) via Telegram buttons, email reply or the app; "Interessant" triggers a deep-dive with a concrete proposal.

**Non-goals**
- No predefined topic taxonomy. The LLM assigns topic labels and weighs relevance.
- The proactive system never writes to Paperless-ngx (Layer 1). All output lives in the user's vault folder and in a disposable operational DB.
- No adaptive scan-frequency heuristics. Work volume is driven by events and a queue (see §5).
- No n8n / external workflow engine, no LiteLLM.

---

## 2. Feature switch

Master switch in `.env`. When `PROACTIVE_ENABLED=false`:
- the worker process exits immediately after start (log line, exit code 0),
- no scheduler, no pollers, no Telegram polling for proactive features,
- findings UI page and proactive settings are not registered,
- findings/dossiers are not embedded and no proactive tools are registered for the agent.

**Split of responsibilities — no admin role:**
- `.env` (operator): master switch, server-side channel credentials (bot token, system mailbox), host power/WoL, sweep windows + doc budget, model allowlist and cloud limits. Changed rarely, needs a restart.
- User settings UI (per user): channels (Telegram / E-Mail / beide), addresses + linking, full-text opt-in, deadline offsets, proactive model (from the allowlist), opt-in to the proactive system at all.

A channel is *available* if its server config is present (`TELEGRAM_BOT_TOKEN` set, SMTP+IMAP set). Whether it's *used* is purely the user's choice. No per-channel `.env` switches.

```env
# --- Proactive system ---
PROACTIVE_ENABLED=false

PROACTIVE_DB_PATH=/var/lib/paperlessbrain/proactive.db
PROACTIVE_FINDINGS_ROOT=/mnt/vaults/{user}/PaperlessBrain Findings
PROACTIVE_APP_BASE_URL=https://brain.example.lan     # links in notifications
PROACTIVE_ANALYSIS_VERSION=1                         # bump → backlog re-sweep + dossier rebuild

PROACTIVE_DEADLINE_OFFSETS_DAYS=30,7,1               # default, user-overridable
PROACTIVE_DAILY_RUN_TIME=07:00                        # deadline reminders + digest check
PROACTIVE_NEWDOC_POLL_SECONDS=300

# Inference & host power (operator, §3.1)
PROACTIVE_WOL_AUTO=true                               # reuses the existing WoL mechanism
PROACTIVE_SWEEP_WINDOWS=01:00-05:00/200               # window/max-docs; comma-separated for several
PROACTIVE_MODEL_FIXED=                                # set → all users use this model, user choice disabled (no swaps)
PROACTIVE_MODEL_ALLOWLIST=                            # comma list; empty = all models of the active backend
PROACTIVE_MODEL_DEFAULT=                              # empty = chat model
PROACTIVE_CLOUD_ALLOWED=false                         # false → proactive jobs never run on a cloud backend
PROACTIVE_CLOUD_MAX_DOCS_PER_USER_DAY=20              # only if cloud allowed and active
PROACTIVE_MAX_PUSHES_PER_WEEK=3                       # default per user; user may change it in settings
PROACTIVE_MAX_PUSHES_PER_WEEK_LIMIT=14                # operator ceiling for the user setting
PROACTIVE_PUSH_THRESHOLD=0.5
PROACTIVE_QUIET_DAYS=10                               # no push for N days → digest from held queue
PROACTIVE_DIGEST_HELD_ITEMS=2

# Telegram (dedicated bot for PaperlessBrain, see §7.1)
TELEGRAM_BOT_TOKEN=

# Email — plain SMTP/IMAP
SMTP_HOST=
SMTP_PORT=587
SMTP_SECURITY=starttls          # starttls | ssl | none
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM="PaperlessBrain <brain@example.com>"
IMAP_HOST=
IMAP_PORT=993
IMAP_SECURITY=ssl               # ssl | starttls | none
IMAP_USER=
IMAP_PASSWORD=
IMAP_FOLDER=INBOX
IMAP_PROCESSED_FOLDER=PaperlessBrain/Processed
IMAP_POLL_SECONDS=120
```

All of these are read once into a typed settings object (`config/proactive.py`); nothing else reads `os.environ` directly.

---

## 3. Process layout

```
┌──────────────────────┐        ┌───────────────────────────────────────────┐
│ NiceGUI app          │        │ paperlessbrain-worker  (same codebase,    │
│ - findings page      │        │  separate entrypoint, systemd/container)  │
│ - proactive settings │──SQLite─│ - tick loop (scheduler)                   │
│ - reaction endpoint  │  (jobs) │ - job runner                              │
└──────────────────────┘        │ - Telegram bot (sole poller of the token) │
                                │ - IMAP poller                             │
                                │ - Paperless new-doc poller                │
                                └───────────────────────────────────────────┘
```

- **One worker process.** Owns all background work and all Telegram I/O.
- **Telegram constraint:** only one process may call `getUpdates` per bot token (a second poller gets HTTP 409 Conflict). The worker is therefore the only Telegram poller. (The former Hermes bot no longer exists — its LXC was removed on 2026-10-26 — so there are no handlers to migrate; see `docs/proactive-diagnostics.md` D3.) The app enqueues outbound messages as jobs, it never talks to Telegram directly.
- **Scheduler:** own asyncio tick loop (60 s), no APScheduler. Recurring tasks are rows in `schedules` with `next_run_at`; jobs are rows in `jobs`. ~100 lines, no extra dependency.
- **LLM gate:** jobs carry `needs_llm`. Before running a batch of LLM jobs the worker wakes the host if allowed (§3.1) and sends a warmup ping; if inference is unreachable after timeout → jobs stay queued with exponential backoff. Deadline reminders from the analysis cache do **not** need the LLM and run even when the workstation sleeps.

### 3.1 Inference & host power

**Wake + shutdown: reuse the existing mechanism.** The worker calls the same code path as the existing WoL button. Shutdown stays with the existing idle rule (30 min without inference → host off). The worker adds no shutdown logic of its own; it simply stops sending requests when the window's work is done, and the existing rule takes over. `PROACTIVE_WOL_AUTO=false` → LLM jobs only run when the host is already reachable.

Inline caveat (verify in D6): this only works if the existing idle timer counts *all* inference requests, including the worker's. Otherwise the host may shut down mid-sweep.

**Sweep windows + doc budget:** backlog + topic revisit only inside a window; a run stops at window end **or** at the doc budget, whichever comes first. The budget is distributed round-robin across opted-in users (1 doc per user per round), so one user with 3,000 backlog docs can't starve the others. New-doc triage and deadline jobs don't count against the budget.

**Per-user model:**
- **Fixed model (optional):** if `PROACTIVE_MODEL_FIXED` is set, it overrides everything: all proactive jobs use it, the model dropdown in user settings is shown read-only with the fixed name, and stored user choices are ignored (kept, not deleted, so they return when the fixed model is removed). Allowlist and default are ignored while it's set. Startup fails with a clear error if the fixed model isn't offered by the active backend.
- Otherwise users pick their proactive model in their settings from `PROACTIVE_MODEL_ALLOWLIST` (or all models of the active backend if empty). Default `PROACTIVE_MODEL_DEFAULT`, else the chat model.
- Same backend, only the model name differs, so the "one backend at a time" invariant holds.
- Inline caveat: 40 users × different models = llama-swap swapping VRAM constantly. The job runner therefore **groups LLM jobs by model** inside a window (all jobs for model A, then model B): one swap per model per window instead of one per job.
- **Cloud:** if the active backend is a cloud provider and `PROACTIVE_CLOUD_ALLOWED=false`, no LLM jobs run (deadline reminders from the cache still do). If allowed, each user is capped at `PROACTIVE_CLOUD_MAX_DOCS_PER_USER_DAY`. Inline caveat: with cloud allowed, the backlog sweep sends every user's documents to the provider; the user settings page must say so when a cloud backend is active.

---

## 4. Persistence

Two stores, with different roles:

| Store | Content | Reconstructable? |
|---|---|---|
| Vault: `PaperlessBrain Findings/` (md, per user) | findings, dossiers, `_interests.md` | user-facing truth for status/reactions; content reconstructable from Paperless |
| `proactive.db` (SQLite) | job queue, schedules, sweep cursors, doc analysis cache, channel links, link codes | yes, except channel links (users re-link) |

Rule: **notification dedupe is derived from findings frontmatter** (`surfaced_at`, `reminders_sent`), not from SQLite. Losing the DB causes a re-sweep, never a notification storm.

### 4.1 SQLite schema (sketch)

```sql
user_prefs(user, proactive_optin, channels, model, deadline_offsets, max_pushes_per_week, updated_at)  -- channels: telegram|email|both
window_log(window_start, model, docs_processed, woke_host, stop_reason)
jobs(id, kind, user, payload_json, needs_llm, status, attempts, run_after, created_at, last_error)
schedules(name, next_run_at, interval_spec)
sweep_cursor(user, analysis_version, last_doc_id, finished_at)
newdoc_cursor(id=1, last_doc_id)
doc_analysis(doc_id, analysis_version, summary, deadlines_json, topic_hint, content_hash, created_at)
channel_link(user, channel, address, full_text_optin, created_at)   -- channel: telegram|email
link_code(code_hash, user, expires_at, used_at)
```

`doc_analysis` is **user-independent** (facts of the document) and computed once per document per version. Everything user-specific (relevance, priority, dossier, finding) is computed per user after a permission check (§6).

### 4.2 Vault layout

```
/mnt/vaults/{user}/PaperlessBrain Findings/
├── _interests.md                 # code-maintained relevance stats; user may edit `muted`
├── Hinweise/
│   └── 2026-10-26 Kfz-Versicherung Wechselfrist.md
└── Dossiers/
    └── bausparvertrag.md
```

### 4.3 Finding file

```yaml
---
psage_id: 3f1c…                 # stable identity, existing convention
finding_id: f7k3q9x             # 7 chars base32, used in callbacks/links/Message-ID
kind: deadline | insight
status: new | surfaced | held | interested | researched | done | dismissed | snoozed
topic: kfz-versicherung         # LLM label, normalized slug
urgency: low | medium | high | critical
priority: 0.62                  # computed by code (§8)
advice: true                    # contains a concrete recommendation → disclaimer
doc_ids: [1234, 1301]
evidence_hash: 9ab2…            # sha256(sorted doc_ids + normalized key facts)
analysis_version: 1
created: 2026-10-26T07:00:00+01:00
surfaced_at:
snoozed_until:
deadline: 2026-11-30            # kind=deadline only
reminders_sent: []              # kind=deadline only, e.g. [30, 7]
---
## Zusammenfassung
## Belege            (verbatim quotes, each with doc_id + Paperless link)
## Quellen           (web sources, URL + retrieval date)
## Empfehlung        (only if advice: true)
> Keine Finanz- oder Anlageberatung. Prüfe die Empfehlung selbst.
```

State machine:

```
new ──(priority ≥ threshold, under cap)──> surfaced ──Interessant──> interested ──deep-dive──> researched ──> done
 │                                           │──Später──> snoozed (snoozed_until) ──> surfaced
 └──(below threshold / over cap)──> held      └──Nie wieder──> dismissed (+ topic muted)
held ──(quiet period digest)──> surfaced
```

Re-surfacing a `dismissed` finding happens only if `evidence_hash` changes (new document or new facts).

Users may edit `status` in Obsidian; the existing git-based vault change detection picks it up and the worker re-reads the ledger. Writes use the same mtime check as the in-app editor; on conflict the worker re-reads and retries once, never overwrites user edits.

### 4.4 Dossier file

```yaml
---
psage_id: …
topic: bausparvertrag
doc_ids: [...]
last_doc_id: 1301
analysis_version: 1
revisit_after: 2027-01-15
---
## Aktueller Stand        (balance, conditions, open obligations)
## Zeitleiste             (date — event — doc_id)
## Offene Fragen
```

Built once by a Werkbank task (full thread read with triage), then updated incrementally: new doc + existing dossier → updated dossier. Bumping `PROACTIVE_ANALYSIS_VERSION` triggers a rebuild.

---

## 5. Work triggers (two loops)

### Loop 1 — documents
- **New documents:** poll Paperless `GET /api/documents/?id__gt={cursor}&ordering=id` every `PROACTIVE_NEWDOC_POLL_SECONDS`. Chosen over the Paperless webhook workflow action: no inbound endpoint, no shared secret, survives worker downtime by construction. Webhook can be added later as a latency optimization.
- **Backlog sweep:** inside the configured sweep windows, within the doc budget (§3.1), cursor keyed by `(user, analysis_version)`. Finished cursor = done until version bump.

Per document: `doc_analysis` (once) → for each opted-in user with read access: triage → maybe finding / dossier update.

### Loop 2 — topics
- Nightly: dossiers with `revisit_after <= today` get a revisit job (web context may change without new documents).
- Interval by interest weight: high 30 d, normal 90 d, low 180 d, muted never.

---

## 6. Permissions

- Every user-specific job runs **with that user's Paperless token**. The worker never uses the ingestion superuser token for user-facing analysis.
- Access check is authoritative from Layer 1: `GET /api/documents/{id}/` with the user's token; 404/403 → skip user.
- Chroma retrieval inside proactive jobs filters by the user's permitted document IDs (same mechanism as interactive RAG — verify in D1).
- A finding belongs to exactly one user. Shared documents → one finding per user.
- Notifications only go to channel addresses linked by that user.

---

## 7. Notifier

```python
class Channel(Protocol):
    name: str
    async def send(self, user: str, msg: Notification) -> None: ...

@dataclass
class Notification:
    finding_id: str
    title: str          # localized
    urgency: str
    body_short: str     # title + urgency + link
    body_full: str      # only if channel_link.full_text_optin
    actions: list[str]  # ["interested", "later", "never"] or ["done", "wrong"] for deadlines
```

**Channel selection (user settings):** `Telegram` / `E-Mail` / `beide`. Only channels that are available (server config present) and linked by the user can be selected. With `beide`, every notification goes to both; a reaction on either channel updates the same finding, and the second reaction is ignored if the status already moved.

Default payload: **title + urgency + app link**. Full text only with per-channel opt-in (Telegram and email both leave the premises; Telegram bot chats are not end-to-end encrypted).

Templates use the existing gettext translator with the user's language.

### 7.1 Telegram
- A dedicated bot for PaperlessBrain (`TELEGRAM_BOT_TOKEN`), running inside the worker. The Hermes bot this originally reused no longer exists (diagnostics D3).
- **Linking:** user generates an 8-char code in the app (10 min TTL, single use, stored hashed) → sends `/start <code>` → `channel_link(user, telegram, chat_id)`. `/stop` unlinks. This mapping is the allowlist.
- **Buttons:** inline keyboard, `callback_data = "f:{finding_id}:{action}"` (≤ 64 bytes). On callback: verify `chat_id` is linked to the finding's owner, else ignore.

### 7.2 Email (SMTP + IMAP)
- **One system mailbox** (credentials in `.env`) sends all notifications and receives all replies. Users only enter their own address in settings and confirm it with a verification code.
- **Outbound:** `aiosmtplib`, plain SMTP with STARTTLS/SSL per config. Multipart text + HTML. Action links point to `{APP_BASE_URL}/findings/{finding_id}?action=…` (behind Authelia).
- `Message-ID: <{finding_id}.{random}@{from-domain}>` so replies are attributable.
- **Inbound:** IMAP poll every `IMAP_POLL_SECONDS` on `IMAP_FOLDER`, `UNSEEN` only. Reuse the existing IMAP helper from `comms_researcher` (UTF-7 folder names, charset handling); run blocking `imaplib` in an executor.
  - Match via `In-Reply-To` / `References` → `finding_id`.
  - Sender must equal the user's linked address.
  - Parse first non-quoted, non-empty line: `1|ja|interessant` → interested, `2|später` → later, `3|nein|nie` → never. Anything else → ignore, log.
  - Move processed mails to `IMAP_PROCESSED_FOLDER`.
- **Caveat:** `From` is spoofable. Email replies may only trigger low-risk actions (status change, enqueue deep-dive). Results are only ever sent to the linked address, never to a reply-to.

---

## 8. Relevance learning

`_interests.md` is written by code; the LLM reads it, never writes it.

```yaml
---
psage_id: …
muted: [handyvertrag]          # user-editable
---
| topic | surfaced | interested | dismissed | never | last_reaction |
|---|---|---|---|---|---|
| kfz-versicherung | 3 | 2 | 0 | 0 | 2026-10-20 |
```

Topic labels: the triage prompt receives the list of existing labels and must reuse one when it fits; labels are normalized to lowercase slugs. Manual merges = edit the table.

Scoring (code, tune empirically):
```
urgency_score = {low: .25, medium: .5, high: .75, critical: 1.0}
weight        = clamp(2 * (interested + 1) / (interested + dismissed + 2), 0.2, 2.0)   # unknown topic → 1.0
priority      = urgency_score * weight
muted topic   → held unless urgency == critical
push if priority >= PROACTIVE_PUSH_THRESHOLD and pushes_this_week < user_max_pushes_per_week
else status = held
```

**Weekly message limit (user setting "Max. Nachrichten pro Woche"):** range 0 to `PROACTIVE_MAX_PUSHES_PER_WEEK_LIMIT`, default `PROACTIVE_MAX_PUSHES_PER_WEEK`. Counting rules:
- Rolling 7 days, per user, counted once per notification, not per channel (`beide` = 1).
- A digest counts as 1, however many items it contains.
- Deep-dive results the user asked for ("Interessant") don't count.
- Critical deadline reminders don't count and are never blocked. Inline caveat: with 0, the user still gets critical deadline reminders; the settings page says so next to the field.
- Everything over the limit becomes `held`, not dropped.

**Quiet-period digest:** if a user had no push for `PROACTIVE_QUIET_DAYS`, the daily run sends one digest with the top `PROACTIVE_DIGEST_HELD_ITEMS` held findings (highest priority first). This is also the exploration path that lets low-weight topics recover.

Deadline findings with urgency `critical` (≤ 1 day, or legally binding windows such as cancellation periods) bypass the weekly cap.

---

## 9. Deadlines

- Extracted in `doc_analysis` per document: list of `{date, kind, actionable, quote}`.
  `kind ∈ {cancellation_window, payment, objection, application, renewal, informational}`.
- Only `actionable == true` and `date >= today` produce a deadline finding. Past dates never produce reminders (fixes the "218 overdue" class of bug).
- `quote` must appear verbatim in the document text (normalized whitespace match) or the deadline is discarded.
- Reminders at the user's offsets (default 30/7/1 days); `reminders_sent` in frontmatter prevents duplicates.
- Deadline buttons: `Erledigt` (→ done) / `Falsch erkannt` (→ dismissed; logged for prompt tuning).

---

## 10. Triage, advice, and web

**Triage** (per user, per relevant document): input = doc_analysis + related dossier (if any) + `_interests.md` + a few semantically similar docs. Output (JSON schema): `topic`, `urgency`, `worth_finding: bool`, `summary`, `evidence_quotes[]`, `needs_web: bool`, `dossier_update: bool`.

**Advice:** the agent may give concrete recommendations. Deterministic validator before a finding is written:
- ≥ 1 evidence quote that appears verbatim in a referenced document,
- ≥ 1 web source URL that was actually fetched in this run (not model-recalled),
- otherwise the recommendation section is dropped and `advice: false` (the finding stays as flag + facts).
- `advice: true` → disclaimer is appended by code, not by the LLM.

**Web query guard** (code, in the search tool wrapper, applies to all proactive web calls):
reject queries containing IBANs, email addresses, digit runs ≥ 6, currency amounts, full dates, the user's first/last name, or patterns like `Nr.`/`Vertragsnummer`/`Policennummer` followed by an identifier. Rejection returns a tool error ("query rejected: contains personal identifiers — rephrase generically") and is logged. The system prompt additionally instructs generic queries (category + product + year), but the guard is the guarantee.

**Deep-dive** ("Interessant"): `run_deep_dive(finding)` interface. Phase 7: implemented with the existing RAG agent (web + doc tools, guard active). Phase 8: swapped for a Werkbank task. Result is written into the finding (`status: researched`) and a notification is sent.

---

## 11. Retrieval integration

- Findings (status `surfaced | interested | researched | done`) and dossiers are embedded into the `vault` collection with metadata `source_type = finding | dossier`, trust level set by code = low.
- `dismissed`, `held`, `snoozed` and `_interests.md` are excluded at ingestion time (frontmatter filter).
- RAG system prompt rule: findings/dossiers may guide which documents to look at, but answers must cite the underlying Paperless documents, never a finding or dossier as evidence. Prevents circular citation of the agent's own conclusions.

---

## 12. Rejected alternatives

| Alternative | Reason |
|---|---|
| Adaptive scan frequency | Event polling + queue + versioned cursor give the same effect without tuning a heuristic |
| Paperless webhook as primary trigger | Needs inbound endpoint + secret; polling with id cursor is simpler and downtime-safe |
| APScheduler / Celery / arq | Single worker, low job rate; own tick loop + SQLite is sufficient |
| Recipient-only permission filtering | Agent context could mix documents the recipient can't read |
| Predefined topic catalog | User decision: LLM weighs relevance; learned interests replace it |
| LLM-maintained interest profile | Drifts; code keeps counts, LLM only reads them |
| Writing deadlines into Paperless custom fields | Proactive system stays read-only on Layer 1 |
| Parsing free-text email replies with an LLM | Keyword parsing is deterministic and sufficient |
| Web-leak rules in prompt only | Soft; code guard is the guarantee |
| Admin role + admin settings UI | Needs roles, auth checks and settings persistence for values changed a few times a year; `.env` covers it. Revisit with LLDAP groups |
| Per-channel `.env` switches | Availability follows from server config; usage is the user's choice |
| Own shutdown logic (SSH, idle checks) | Existing WoL/idle-shutdown mechanism already does it |
