# Proactive System — Implementation Tasks

Reference: `docs/proactive-architecture.md`. Invariants: the "Proactive system" section of `CLAUDE.md`.
Execute one or two phases at a time, `/clear` between phases. Every phase ends with its acceptance criteria verified end-to-end, not just unit tests.

---

## Phase 0 — Diagnostics (read-only, no code changes)

Output: `docs/proactive-diagnostics.md` with findings per item and a go/no-go note.

- **D1 Permissions:** How does `search_documents` / Chroma retrieval restrict results per user: data-layer filter by permitted doc IDs, or session context? Can a process without an Authelia session obtain a user's Paperless token? Where are per-user tokens stored?
- **D2 Deadlines:** Locate existing deadline extraction. Document the root cause of the "218 overdue" artifact and which fields/dates it uses.
- **D3 Telegram:** Where does the Hermes bot currently poll (process, entrypoint)? List its handlers and its chat-ID allowlist mechanism.
- **D4 IMAP:** Locate the `comms_researcher` IMAP helper; confirm it's reusable without a user session (credentials source).
- **D5 Vault writes:** How does the app write `PaperlessBrain Memos` today (path resolution per user, file permissions, mtime handling)? Does git change detection re-embed app-written files, and how is a write-then-detect loop avoided?
- **D6 Inference & power:** Does the backend expose `/v1/models`? Is llama-swap in use? Locate the existing WoL button code path and the 30-min idle shutdown: what counts as activity (all inference requests? only UI sessions?), and does it shut down a host that was already on before WoL? Can the worker call the WoL path without a UI session?
- **D7 Werkbank:** Implementation status. Phases 8+ are blocked until Werkbank phase 1–3 exist.

**Go/no-go:** if D1 shows session-only filtering, add a "Phase 0.5: data-layer permission filter" before anything else.

**Result (2026-10-26):** no-go as specified — see `docs/proactive-diagnostics.md`. Phase 0.5 there: fix the web-search archive-hint leak, decide how users grant background access (the worker cannot obtain a user's token today), cross-process vault lock.

## Phase 0.5 — Permission and process prerequisites ✅ done (2026-10-26)

- Web-search archive hint filtered by the user's Paperless access.
- `vault/locks.py`: cross-process lock (`flock`), same API.
- `services/background_access.py`: per-user grant of the Paperless token (option A); refreshed at login, `needs_reauth` on 401.

Carried into Phase 1: the opt-in / revoke control for background access in the proactive settings; worker jobs get a user's token only via `background_access.token_for(user)` and skip the user when it is None.

---

## Phase 1 — Feature switch, config, worker skeleton ✅ done (2026-10-26)

Built: `config/proactive.py` (also: `config/settings.py` now ignores unknown keys —
without that the app refused to start once a `PROACTIVE_*` key was set),
`proactive/` (`db.py`, `jobs.py`, `scheduler.py`, `worker.py`, `prefs.py`,
`tasks/`), entrypoint `paperlessbrain-worker`, compose service
`paperless-brain-worker` (`restart: on-failure`, heartbeat-file health check),
`deploy/paperlessbrain-worker.service`, Settings > Proactive (background access +
opt-in). Tests: `tests/test_proactive_worker.py`. `jobs.Defer` = "not now" (host
asleep, no grant): re-queued without spending an attempt.

- `config/proactive.py`: typed settings from `.env` (all keys in architecture §2), validation; channel availability derived from config presence.
- Worker entrypoint `paperlessbrain-worker` (same package). Exits with a log line if `PROACTIVE_ENABLED=false`.
- `proactive.db` with migrations for all tables in architecture §4.1.
- Tick loop (60 s): `schedules` → enqueue `jobs`; job runner with retries + exponential backoff; graceful shutdown.
- App side: register findings page / proactive settings / reaction endpoint only if enabled.
- Proactive settings: "Allow background access" (grant with the session token) / revoke, with status (`none` / `active` / `needs_reauth`). Opting in to the proactive system requires an active grant.
- systemd unit (or compose service) for the worker.

**Acceptance**
- [ ] `PROACTIVE_ENABLED=false`: worker exits 0; app shows no proactive UI; no DB file created.
- [ ] `PROACTIVE_ENABLED=true`: a dummy schedule fires a no-op job every minute, visible in logs; kill -TERM finishes the running job and exits cleanly.
- [ ] A job raising an exception is retried with backoff and ends `failed` after N attempts with `last_error` set.

---

## Phase 2 — Inference gate, windows, per-user model ✅ done (2026-10-26)

Built: shared power state `data/power_state.json` (`services/ollama_watchdog.py`,
replaces module globals — D6), `services/wol.py` (one wake path for the dashboard
and the worker), `proactive/gate.py` (probe → WoL → wait; backoff; asked only when a
local LLM job is due), `proactive/models.py` (`resolve_model`, the one resolution
function), `proactive/windows.py` (windows, budget, `window_log`, cloud cap),
`proactive/tasks/sweep.py` (round-robin backlog sweep; the per-document processor
plugs in from Phase 6), job grouping by model (`jobs.model_key`, `lane`), model
dropdown in Settings > Proactive. Tests: `tests/test_proactive_gate.py`.
"[Verify in] backend logs" items are covered by fakes; on the real host the
`window_log` rows per window = models used (swaps + 1).

- LLM gate: wake via the existing WoL code path (if `PROACTIVE_WOL_AUTO`) → warmup ping → run `needs_llm` jobs; unreachable → backoff, jobs stay queued. No own shutdown logic; the existing idle rule handles it (fix per D6 if worker requests don't count as activity).
- Sweep windows + doc budget, round-robin across users; `window_log`.
- `user_prefs` table; user settings: proactive opt-in, model dropdown (allowlist ∩ `/v1/models`; read-only when `PROACTIVE_MODEL_FIXED` is set), cloud warning when a cloud backend is active.
- Model resolution in one function: `fixed` → `user choice` (if allowed) → `default` → `chat model`.
- Job runner groups `needs_llm` jobs by model within a window.
- Cloud gate: `PROACTIVE_CLOUD_ALLOWED` + per-user daily doc cap.

**Acceptance**
- [ ] WoL auto off + host asleep: LLM jobs stay queued, non-LLM jobs run.
- [ ] WoL auto on: host wakes, jobs run on each user's model (verify in backend logs); host shuts down ~30 min after the last job via the existing rule, not during the sweep.
- [ ] Two users with different models: exactly one model swap per window (backend log).
- [ ] Window budget 10, three users with backlog: docs split ~evenly; run stops at window end even with budget left.
- [ ] Cloud backend active + `PROACTIVE_CLOUD_ALLOWED=false`: no LLM job runs, deadline reminders still sent.
- [ ] Model not in allowlist can't be selected; a stale stored model falls back to the default with a log line.
- [ ] `PROACTIVE_MODEL_FIXED` set: dropdown read-only, all jobs on the fixed model, zero swaps per window; unset again → users' previous choices apply.
- [ ] `PROACTIVE_MODEL_FIXED` names a model the backend doesn't offer → worker refuses to start with a clear error.

---

## Phase 3 — Notifier + channel linking ✅ done (2026-10-26)

Built: `proactive/notify/` (`Notification`, `compose`, per-channel delivery jobs so a
failing channel retries alone), `notify/telegram.py` (Bot API over httpx — no new
dependency; worker-only poller, offset persisted per update, 409 logged and backed
off; `/start <code>`, `/stop`; callbacks act only for the finding owner's linked
chat), `notify/email.py` (**stdlib `smtplib` in a thread instead of `aiosmtplib`** —
no new dependency for a few mails a week; `Message-ID: <{finding_id}.{hex}@{domain}>`),
`proactive/linking.py` (codes: 8 chars, 10 min, single use, hashed), `i18n.translator_for`,
Settings > Proactive: channels, linking, email verification, full-text opt-in, offsets,
test message. Tests: `tests/test_proactive_notify.py`. Reactions arrive with Phase 4.

- `Notification` + `Channel` protocol (architecture §7). Localized templates via existing translator.
- Telegram: the worker is the sole poller of a dedicated bot token (no Hermes bot to migrate — diagnostics D3); `/start <code>`, `/stop`; outbound send; inline keyboard; callback handler verifying chat ↔ owner.
- Email outbound: `aiosmtplib`, STARTTLS/SSL/none per config, multipart text+HTML, `Message-ID` scheme.
- User settings: channel choice Telegram / E-Mail / beide (only available + linked channels selectable), link Telegram (code display), set email address (verification mail with code), full-text opt-in per channel, deadline offsets.
- "Testnachricht senden" button in user settings.

**Acceptance**
- [ ] Only one process polls the bot token (no 409 in logs).
- [ ] Link code expires after 10 min and works once; `/stop` removes the link.
- [ ] Test notification arrives on both channels; default content = title + urgency + link only.
- [ ] Callback from a chat not linked to the finding owner is ignored and logged.
- [ ] SMTP/IMAP not configured → email option disabled in the UI, no connection attempted.
- [ ] Channel "beide": notification on both; reacting on one channel, then the other → second reaction ignored.

---

## Phase 4 — Findings ledger + findings page ✅ done (2026-10-26)

Built: `proactive/paths.py`, `proactive/findings.py` (file format with `pbrain_id`
instead of the legacy `psage_id`; `upsert` by `finding_key` + `evidence_hash`),
`proactive/states.py` (pure), `proactive/interests.py`, `proactive/reactions.py`,
`proactive/tasks/ledger.py` (new insight → push; snoozed → wake; scoring in Phase 7),
`app_ui/pages/findings.py` (`/findings`, `/findings/{id}?action=` — the action is
offered as a confirm button, not applied on load, because link scanners follow
links), nav entry only when enabled, findings folder skipped by the vault index.
Conflicts use the vault editor's hash check + merge rather than mtime (D5). Subfolder
`Notices/` (English, the source language) instead of `Hinweise/`. Tests:
`tests/test_proactive_findings.py`.

- Read/write finding files (architecture §4.3) under `PROACTIVE_FINDINGS_ROOT` with `{user}` resolution as in D5; `psage_id` + `finding_id` generation; mtime conflict handling.
- State machine (architecture §4.3) as a pure function with tests.
- Ledger reload on vault change detection (status edits from Obsidian).
- Findings page in the app: list (filter by status/topic), detail view, action buttons; `?action=` link handling for email links.
- Reaction handling shared by all channels: interested / later / never / done / wrong. `interested` enqueues `deep_dive` (stub until Phase 7).
- `_interests.md` writer: counts updated on every reaction.

**Acceptance**
- [ ] Manually created finding → notification → button press → status updated in the md file and in `_interests.md`.
- [ ] Changing `status: dismissed` in Obsidian is reflected in the app after sync.
- [ ] Concurrent edit (Obsidian + worker) never loses the user's edit.
- [ ] Dismissed finding with unchanged `evidence_hash` is not re-surfaced.

---

## Phase 5 — IMAP replies ✅ done (2026-10-26)

Built: `proactive/imap_replies.py` — a module of its own (the chat tool's
`services/imap_service.py` stays read-only; folder-name and header helpers are
shared), `proactive/tasks/imap.py` (scheduled only when email is configured).
`BODY.PEEK` + move after the reaction: a crash re-applies a reaction the state
machine then ignores. MOVE, else COPY + `\Deleted` + (UID) EXPUNGE. First own line
or its first word (`Ja, gerne!` counts; a quoted `1` or one in the signature does
not). Tests: `tests/test_proactive_imap.py`.

- IMAP poller (reuse D4 helper, executor), `UNSEEN` in `IMAP_FOLDER`, match `In-Reply-To`/`References` → `finding_id`, sender == linked address, keyword parse, move to `IMAP_PROCESSED_FOLDER` (create if missing; modified UTF-7 names).

**Acceptance**
- [ ] Reply "1" from linked address → `interested`; "Später" → snoozed; "nie" → dismissed.
- [ ] Reply from a different address → ignored, logged, mail moved.
- [ ] Reply with quoted original + signature still parses correctly.
- [ ] Non-ASCII subject/folder names don't break the poller.

---

## Phase 6 — Deadlines ✅ done (2026-10-26) — real-data spot-check pending

Built: `proactive/analysis.py` (text-only pass over the sidecar text, schema +
verbatim-quote check by code, cached per document/version, re-run when the text
changes), `proactive/deadlines.py` (findings for actionable future deadlines only;
reminders at offsets from the ledger, no LLM; one reminder when several offsets
were missed; urgency by code; dashboard source), `proactive/tasks/documents.py`
(new-document poller — first poll only sets the cursor; per-user access check with
the user's token; sweep processor), `proactive/tasks/daily.py`. Dashboard: an
analysed document's deadlines come from `doc_analysis` (never both); "overdue" only
for a date the user was reminded of and has not closed. Reminder note line
("7 days left") is part of the default message — it tells no more than the
title's date. Tests: `tests/test_proactive_deadlines.py`.

Still to do on the dev machine: `scripts/proactive_deadline_check.py
--with-actions-only` over the D2 set; record the zero-past-reminders count and the
precision of 20 spot-checked deadlines in `docs/proactive-diagnostics.md`.

- `doc_analysis` job: summary + deadlines `{date, kind, actionable, quote}` (architecture §9), JSON-schema-validated output; verbatim quote check.
- New-document poller (`id__gt` cursor) enqueues `doc_analysis` → per-user permission check (user token) → deadline finding.
- Backlog sweep (budgeted, round-robin) for `doc_analysis` so existing documents get deadlines.
- Daily run: reminders at offsets, `reminders_sent` dedupe, critical bypass of weekly cap.
- Deadline buttons: Erledigt / Falsch erkannt (log for prompt tuning).

**Acceptance**
- [ ] Re-run on the D2 dataset: zero reminders for past dates; spot-check 20 extracted deadlines for actionable/informational accuracy, record precision in the diagnostics doc.
- [ ] Reminders fire exactly once per offset, also after worker restart and after deleting `proactive.db`.
- [ ] User without read access to a document never receives its deadline.
- [ ] Deadline reminders are sent while the inference host is asleep (cache-only path).

---

## Phase 7 — Triage, relevance, web guard, interim deep-dive

- Web query guard in the search tool wrapper (architecture §10) with unit tests per pattern; rejection = tool error + log.
- Triage job per (user, doc) after `doc_analysis` (architecture §10 output schema); topic label reuse from `_interests.md`.
- Advice validator (verbatim quote + fetched URL); disclaimer appended by code.
- Scoring + push/held decision + per-user weekly limit (architecture §8: setting, ceiling, counting rules); quiet-period digest in the daily run.
- User setting "Max. Nachrichten pro Woche" (0 … ceiling) with the critical-deadline note.
- `run_deep_dive` interim implementation using the existing RAG agent (web + docs, guard active); writes result into finding → `researched` → notification.

**Acceptance**
- [ ] Guard blocks queries containing an IBAN, a policy number, an amount, the user's name; allows "Riester Kosten Kritik 2026".
- [ ] Finding with a recommendation but no fetched URL is written without the Empfehlung section, `advice: false`.
- [ ] Limit 3: 4th non-critical push within 7 days becomes `held`; channel `beide` counts once; a digest counts once; requested deep-dive results don't count.
- [ ] Limit 0: no insights pushed, critical deadline reminders still arrive.
- [ ] Value above `PROACTIVE_MAX_PUSHES_PER_WEEK_LIMIT` can't be saved.
- [ ] After `PROACTIVE_QUIET_DAYS` without push, one digest with the top held items is sent.
- [ ] "Nie wieder" mutes the topic; a later non-critical finding on it is `held`.

---

## Phase 8 — Dossiers + topic revisit (requires Werkbank)

- Werkbank task `build_dossier(user, topic)`: candidate collection (metadata + semantic), LLM triage of relevance, full dossier (architecture §4.4).
- Incremental update job: new doc in topic + existing dossier → updated dossier.
- Revisit scheduler: `revisit_after` by interest weight (30/90/180 days, muted never); revisit may produce new findings.
- Swap `run_deep_dive` to a Werkbank task.
- `PROACTIVE_ANALYSIS_VERSION` bump → dossier rebuild + backlog re-sweep.

**Acceptance**
- [ ] Bausparvertrag test thread: dossier timeline covers the full history with correct doc IDs; spot-check 5 timeline entries against the documents.
- [ ] New letter in the thread updates the dossier without re-reading all documents (verify in job log).
- [ ] Version bump re-sweeps the backlog exactly once.

---

## Phase 9 — Retrieval integration

- Vault ingestion: include findings/dossiers with `source_type` metadata + low trust; exclude `held|snoozed|dismissed` and `_interests.md` via frontmatter filter.
- RAG system prompt rule: cite Paperless documents, not findings/dossiers.

**Acceptance**
- [ ] "Wie ist der Stand bei meinem Bausparvertrag?" retrieves the dossier and the answer cites the underlying documents.
- [ ] Dismissed findings never appear in retrieval results.
- [ ] A user never retrieves another user's findings.
