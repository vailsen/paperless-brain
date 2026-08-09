# Voice memos — architecture & phased tasks

Status: **implemented 2026-08-08.** Phases 1-6 done; not yet exercised end to end
in a browser with a real microphone.

Scope: a quick-capture path for personal memos. Press a button, speak, get the cleaned-up
text back for confirmation, and it lands as a Markdown file in the user's vault. Optional
feature — invisible unless a transcription service is configured.

Run one phase at a time, `/clear` between phases, verify in the browser before moving on.

---

## Why this is not a chat tool

The obvious design is a `create_memo` tool the LLM calls when it hears "notiz für mich".
**Rejected.** The chat already ships `remember_fact` ("stores a fact in long-term memory")
and `create_deadline` ("use this when the user asks for a reminder"). An utterance like
*"Notiz für mich: Klempner kommt Dienstag"* satisfies both descriptions, and a memo tool would
satisfy all three. The three are near-synonymous at the language level; no prompt wording
separates them reliably, and the write tools already under-trigger on local models in a 20+
tool set. A fourth overlapping write tool makes an existing problem worse.

A configurable trigger phrase ("Memo an mich selbst") was also rejected: ASR mangles fixed
phrases (casing, commas, run-together words), so it needs fuzzy matching, and it is a hidden
mode the user must remember.

**The disambiguating signal is the mode, not the words.** Pressing a dedicated memo button
*is* the routing decision. Deterministic, no classification, no tool competition. This is the
same principle the Werkbank orchestrator is built on — control flow is Python, the LLM only
does the cognitive step inside a scoped role. Here the LLM's only job is rewriting a
dictation; it decides nothing.

Consequence: **no changes to `services/chat_service.py` at all.** No new tool, no new
`ChatEvent`, no agentic loop involvement.

---

## Invariants

Append to `CLAUDE.md` once agreed:

- **The memo button is the routing decision.** Memo capture never goes through tool selection.
  Nothing in `TOOL_DEFINITIONS` may compete with `remember_fact` / `create_deadline`.
- **One memo = one file = one note.** Never an append-only journal file. `vault/sync.py`
  deletes and re-embeds *every* chunk of a changed file, so an append-only memo file re-embeds
  its entire history on each new memo — cost grows linearly forever.
- **Memos are vault notes, not brain facts.** They live outside `BRAIN_SUBFOLDER`, are chunked
  by the normal vault path, land in the `vault` collection, and are found by `vault_search`.
  They are the user's own words, not agent-curated knowledge.
- **The feature is invisible when unconfigured.** No transcription URL, or user opted out →
  no button anywhere, settings section greyed out with activation instructions.
- **Transcription is bring-your-own-endpoint.** No bundled Whisper, no new runtime dependency.
  Any OpenAI-compatible `/v1/audio/transcriptions` works, same as the model registry story.

---

## Decisions with rationale

- **Button lives in the nav/header, not the chat input row.** A memo is not a chat turn and
  must not enter the conversation. Quick capture has to work from any page.
- **Filename carries date, time and topic** — `PaperlessBrain Memos/2026-08-08 1432 Klempner.md`.
  Sorts chronologically, readable in Obsidian's file list, and the topic makes it findable by
  title (`vault_search` boosts title matches).
- **Fixed subfolder name**, not user-configurable: `PaperlessBrain Memos/`. One less setting,
  and it mirrors how `BRAIN_SUBFOLDER` names a real directory.
- **The rewrite call returns `{topic, text}`** as structured output — one call produces both
  the filename topic and the cleaned body. Uses the user's currently selected chat model.
- **`.env` for the service URL, not the model registry.** This is server-level infrastructure
  like `SEARXNG_HOST` and `OLLAMA_SERVER`, not a per-user credential. Consistent with the
  removal of `ANTHROPIC_API_KEY` (that *was* a per-user credential in the wrong place).

### Known constraints

- **`getUserMedia` requires a secure context** — HTTPS or `localhost`, no exceptions. Users
  running plain `http://192.168.x.x:8080` cannot use this, and cannot use the existing Web
  Speech mic button either. Not a regression, but it must be documented rather than silently
  failing.
- **Ollama has no `/v1/audio/transcriptions`.** Whisper is a separate service even for users
  who already run Ollama. Say so in the settings help text.

---

## Transcription service setup (user-provided)

Not shipped with the app. Documented in the README and in the settings section.

**Deployment note:** do not run this on a Wake-on-LAN GPU host. The idle watchdog shuts that
machine down, so the first memo of the evening would wait 30–60s for a boot — which destroys
quick capture. Run it on the app host, CPU.

Reference target: AMD Ryzen 7 6800H (8c/16t, Zen3+, AVX2), CPU only.

**Measured 2026-08-08** on that hardware with `large-v3-turbo`, int8, 8 threads, a real 30.9s
German dictation: **~6.1s**, i.e. roughly 5× realtime. Consistent across runs. Press-speak-wait
of about six seconds for a half-minute memo — acceptable for quick capture.

Quality on that sample was excellent: dates (`21.11.2025`), figures, and domain vocabulary
(`Steuerbescheid`, `Mieteinkünfte`, `Gebäudeversicherung`) all correct. Only nits were `AFA`
for `AfA` and a spoken `Nr.` rendered as `Nummer` — exactly the class of thing the Phase 2
rewrite pass cleans up.

`WHISPER_BEAM=1` was tested and **is not worth it**: 5.9s vs 6.1s, ~3%, because the cost is
dominated by the encoder rather than the decoder search. Keep beam 5 — it costs nothing here
and helps on harder audio. Do not reach for this lever first if things feel slow.

Pin threads to physical cores (8), not 16 — SMT typically hurts CTranslate2. If the LXC is
capped below ~4 cores, fall back to `small` (~0.5 GB RAM, faster, but stumbles on names and
compounds — which is most of what a German memo contains).

**Option A — Docker in the LXC** (needs `nesting=1`, plus `keyctl=1` if unprivileged).
[hwdsl2/docker-whisper](https://github.com/hwdsl2/docker-whisper), faster-whisper, port 9000,
auto-generated Bearer token (`docker exec whisper whisper_manage --getkey`), `WHISPER_MODEL`
and `WHISPER_LANGUAGE` env vars, amd64 + arm64.

> **Deployed 2026-08-08** on the PaperlessBrain LXC at `~/whisper/` (compose.yaml +
> whisper.env). Published on `0.0.0.0:9000` so a dev machine can reach it during development;
> the API key is what protects it. Narrow the port line back to `127.0.0.1:9000:9000/tcp`
> once the app and the service share a host. Model cache lives in the `whisper-data` volume (1.6 GB),
> so container recreates do not re-download. Verified: `GET /v1/models` returns
> `large-v3-turbo`, unauthenticated POST → **401**, authenticated **webm/opus** upload → 200
> with `{"text": ...}`.
>
> **webm/opus is accepted directly**, so the Phase 5 audio leg needs no client-side
> transcoding — `MediaRecorder` output can be posted as-is.
>
> Validated on a real 30.9s German dictation — see the timing and quality numbers above.

**Option B — no Docker**, matching how PaperlessBrain itself is deployed there (systemd unit):

```bash
git clone https://github.com/ggml-org/whisper.cpp && cd whisper.cpp
cmake -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j8
./models/download-ggml-model.sh large-v3-turbo-q5_0     # ~570 MB

./build/bin/whisper-server \
  --model models/ggml-large-v3-turbo-q5_0.bin \
  --host 127.0.0.1 --port 2022 \
  --inference-path /v1/audio/transcriptions \
  --language de --threads 8 --convert
```

`--inference-path` is required — whisper.cpp serves `/inference` by default. `--convert` is
required too: it runs the upload through ffmpeg, so `MediaRecorder` output (webm/opus) is
accepted instead of rejected for not being 16 kHz WAV.

**Other options:** [Speaches](https://github.com/speaches-ai/speaches) (ex
`faster-whisper-server`, ghcr images CPU/CUDA, port 8000, **no auth by default**),
[openedai-whisper](https://github.com/matatonic/openedai-whisper), or hosted Groq / OpenAI —
cloud means dictated memos leave the house, which contradicts the point of a private vault.

### `.env` keys

```bash
# Voice memos — optional. Empty WHISPER_URL = feature hidden everywhere.
WHISPER_URL=http://127.0.0.1:2022/v1
WHISPER_API_KEY=
WHISPER_MODEL=large-v3-turbo
WHISPER_LANGUAGE=de
```

---

## Phase 1 — Vault memo writer

No UI, no audio. Pure write path, testable from a REPL.

- [x] `vault/memo_writer.py` — mirrors `vault/brain_writer.py` in shape but targets the
      memo subfolder and the **vault** collection.
- [x] `vault/paths.py`: add `memo_path(username)` next to `brain_path()`, and create the
      folder in `ensure_user_dirs()`.
- [x] Filename: `YYYY-MM-DD HHMM <topic>.md`, sanitised through the existing `_slug()` rules
      (strip `\ / : * ? " < > | # ^ [ ]`, collapse whitespace, cap length), with the
      `_unique_path()` collision suffix.
- [x] Frontmatter: `pbrain_id`, `created`, `updated`, `source: memo`, `dont_ingest: false`,
      `tags`. The `dont_ingest` key must be present so it stays toggleable in Obsidian, same
      as any other vault note.
- [x] Write under `get_user_lock(user)` so it serialises with `sync_user()`.
- [x] **Indexing order matters.** `sync_user()` detects changes via
      `git diff --name-status HEAD` and commits *last* — so do not commit the file and then
      expect sync to see it. Either write + embed + commit inline (brain_writer pattern,
      but through `chunk_vault_file()` into the vault collection), or write without
      committing and let a forced `sync_user()` do both. Pick one and note it in the module
      docstring; do not mix.
- [x] Atomic write (temp + rename) within the mount, per the vault invariants.

**Acceptance:** calling the writer produces a file in `PaperlessBrain Memos/`, `vault_search`
finds it by content *and* by title, `git log` shows the commit, and a second memo with the
same topic in the same minute does not overwrite the first.

## Phase 2 — Rewrite call

- [x] Single-purpose LLM call: no tools, not the agentic loop. Structured output
      `{topic, text}` — `topic` is a short filename subject (3–5 words), `text` is the
      formatted memo body.
- [x] Uses the user's currently selected chat model from the registry.
- [x] **Prompt contract** — the memo should come out *better formatted*, not merely
      de-filler'd:
      - Never pad. The result must not be longer than the content warrants.
      - Never drop relevant content. Every fact, figure, date and reference survives.
      - Structure it: bullet points when the dictation enumerates, a numbered list when it
        sequences, a **table** when the content is genuinely tabular (repeated
        item + value pairs). Prose stays prose when it is prose.
      - Improve sentence formulation where the spoken version is clumsy — but the content
        is fixed. No new claims, no interpretation, no answering the memo.
- [x] Failure path: rewrite fails → fall back to the raw transcript with a topic derived from
      the first few words. A failed rewrite must never lose the user's words.

**Acceptance:** a deliberately messy dictation comes back punctuated and de-filler'd, with a
sensible topic, and the meaning is unchanged.

## Phase 3 — Confirm dialog and typed entry

Everything except audio. Testable end-to-end by typing.

- [x] Memo dialog: textarea prefilled with the rewritten text, editable topic field, and
      save / discard actions. Editing before saving is the point — do not make it read-only.
- [x] Nav/header entry point (`NAV_ITEMS` in `app_ui/layout.py` is the reference for how nav
      is built; the memo control is a button, not a page route).
- [x] All strings wrapped per the i18n rules — `_()` resolved per render, `N_()` for anything
      module-level. New msgids land in the catalog in the usual extract → update → translate →
      compile cycle.
- [x] Visual discipline: neutral icon inheriting text colour, sentence case, no new accent
      meaning.

**Acceptance:** typing a memo in the dialog and pressing save produces the Phase 1 result.
No audio involved yet.

## Phase 4 — Settings section and feature gating

- [x] Settings section built with `_section_header()` like every other block on the page.
- [x] Per-user opt-in toggle, stored in the credential store next to the other chat settings.
- [x] `WHISPER_URL` empty → section rendered **greyed out** with a short activation note
      (what to install, that it must be OpenAI-compatible, that Ollama cannot do this) and
      a pointer to the README section.
- [x] Unconfigured **or** opted out → memo button hidden everywhere. No dead control.
- [x] Travels with settings export/import (`services/settings_transfer.py`).

**Acceptance:** with `WHISPER_URL` unset the section is visibly inert and explains itself, and
no memo button renders. Setting it and opting in makes the button appear after a reload.

## Phase 5 — Audio leg

- [x] Push-to-talk control: `MediaRecorder` in JS, hold to record, release to send.
- [x] **The chat mic switches to Whisper when it is configured.** Measured quality on German
      dictation is far above the browser's Web Speech API, and the endpoint is already there.
      Web Speech remains the fallback when `WHISPER_URL` is unset or the user opted out — so
      `app_ui/pages/chat.py`'s existing recogniser stays, it just stops being the first choice.
      Accept the UX change knowingly: Web Speech streams interim results as you speak, Whisper
      returns nothing until you release and the round trip finishes (~6s for 30s of speech).
- [x] Share one recorder + one upload route between chat and memos. The chat mic wants the
      **raw transcript** (the user is composing a message, and a rewrite would put words in
      their mouth); only the memo path runs the Phase 2 rewrite.
- [x] Upload route: `main.py` already registers FastAPI routes through NiceGUI's `app`
      (`@app.get("/manifest.json")` is the reference). Add an authenticated POST endpoint —
      it must reject unauthenticated uploads, since it accepts a file and spends CPU.
- [x] Transcribe: multipart POST to `{WHISPER_URL}/audio/transcriptions` with `file`, `model`,
      `language`; `Authorization: Bearer` only when `WHISPER_API_KEY` is set.
- [x] Cap upload size and duration. A stuck recording must not post a 300 MB blob.
- [x] Error paths surface in the dialog, not the log: service unreachable, timeout, empty
      transcript. Empty transcript → say so, keep the dialog open, do not write a file.
- [x] **Guard against silence hallucination.** Whisper does not return an empty string on
      non-speech input — it invents a plausible phrase. A 12s tone transcribed as
      `"Vielen Dank."` during deployment testing. So an accidental button press produces a
      confident-looking memo out of nothing. Checking for an empty transcript is not enough;
      reject on a too-short recording before uploading, and treat known filler outputs
      ("Vielen Dank.", "Untertitel von…", "Amara.org") as empty.
- [x] Secure-context check in JS: no `getUserMedia` → hide the control and explain why
      (HTTPS required), rather than failing on click.

**Acceptance:** hold, speak, release → dialog opens with cleaned text within a few seconds →
save writes the file. Stopping the Whisper service produces a clear message and loses nothing.

## Phase 6 — Documentation

- [x] README: a section covering what the feature is, the transcription service options above,
      the HTTPS constraint, and the `.env` keys. Follows the tone of the existing sections.
- [x] `.env.example.full`: annotated block for the four keys.
- [x] `CLAUDE.md`: the invariants above.

**Acceptance:** someone who has never seen the feature can get from "no memos" to a working
memo without reading source.

---

## Explicitly out of scope

- ❌ A `create_memo` chat tool, or any keyword/trigger-phrase routing.
- ❌ Bundling Whisper, faster-whisper or CTranslate2 as a dependency.
- ❌ Append-only journal files, daily notes, or any many-memos-per-file layout.
- ❌ Streaming / live transcription (WebSocket). Push-to-talk is a complete interaction.
- ❌ Routing memos into the `brain` collection or `BRAIN_SUBFOLDER`.
- ❌ A version bump. This ships with the next larger release.
