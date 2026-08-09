# Changelog

All notable changes to PaperlessBrain are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning is [semantic](https://semver.org/) from `v0.2.0` onward.

## [0.4.0] — 2026-08-09

**Voice memos.** Hold the microphone button in the header, speak, and the
recording is transcribed, tidied into structured Markdown, shown to you for
review, and filed into your vault as one Markdown file per memo — searchable in
chat like any other note.

The organising decision: **the button is the routing decision.** Memo capture
never goes through tool selection. There is no `create_memo` tool and no trigger
phrase, because `remember_fact`, `create_deadline` and "note to myself" are
near-synonymous at the language level — anything in `TOOL_DEFINITIONS` competing
with them would misroute a memo into the brain, or a deadline into a note.
Pressing the button is what tells the app which one you meant.

Transcription is **bring-your-own-endpoint**, the same principle as the chat
model registry: no bundled speech model, no new runtime dependency, no hardcoded
provider. Point `WHISPER_URL` at anything with an OpenAI-compatible
`/v1/audio/transcriptions` endpoint. An empty URL hides the feature everywhere
rather than failing at the first press.

### Added

- **Voice memos** (`services/transcription.py`, `services/memo_service.py`,
  `vault/memo_writer.py`, `app_ui/memo_dialog.py`, `app_ui/memo_routes.py`).
  One memo = one file in `MEMO_SUBFOLDER`, named `YYYY-MM-DD HHMM Topic.md`.
  Deliberately not an append-only journal: `vault/sync.py` re-embeds *every*
  chunk of a changed file, so appending would re-embed the whole history on
  every memo. Memos are vault notes, not brain facts — chunked, in the `vault`
  collection, found by `vault_search`.
- **Conversation mode.** A Memo/Conversation switch in the dialog turns a
  recorded dialog into speaker turns:

  ```markdown
  **Speaker 1:** The quote lands at 14,200 including the worktop.

  **Speaker 2:** And the deadline?
  ```

  The labels come from the transcription service, never from a model — the app
  asks for `response_format=verbose_json` and reads the per-segment `speaker`
  field, because speaker labels do not exist in the flat `text` field. Asking an
  LLM to attribute turns it was not given is fabrication, so the rewrite prompts
  forbid it. A service without a diarizer returns no such field and you get an
  ordinary transcript, no error. Diarization is unsupervised: speakers are
  numbered (renumbered by who speaks first, since the diarizer numbers by
  clustering order and a transcript opening at "Speaker 3" reads like a bug),
  never named. A single detected speaker drops the labels entirely.
- **Audio file upload.** The dialog accepts an existing recording — wav, m4a,
  mp3, ogg, flac — for memos made on a phone's own voice recorder. It shares one
  code path with the recorder (`transcribe_payload`), so the size cap and the
  silence guard cannot be skipped by either entry point.
- **Swipe to lock.** Drag the record button up 48 px while holding and the
  recording continues with your thumb off the button; the next tap stops it. The
  pointer is captured for the gesture — without that it leaves the button a few
  pixels into the swipe and the move events stop arriving.
- **Typed memos.** The dialog works with no microphone at all, which keeps the
  whole path usable without HTTPS (browsers block `getUserMedia` outside a
  secure context).
- **Extended thinking on the Anthropic backend.** `ClaudeChatBackend` never sent
  a `thinking` parameter, so reasoning was never *requested* — the UI only
  rendered thinking a model emitted unprompted. Claude therefore never thought,
  and a hybrid model like MiniMax M2 only sometimes did. Now a per-model
  three-state setting with a token budget; enabling it pins temperature to 1, as
  the API requires.
- `CONVERSATION_MAX_UPLOAD_MB` (200) and `CONVERSATION_MAX_SECONDS` (3600) — a
  meeting is not a memo, and the memo caps would cut it off mid-sentence.
- Voice-memo settings section, a dedicated **memo cleanup model** (a short,
  strictly formatted job — a small local model is usually enough, even if you
  chat with Claude), and both carried by the settings export/import.
- 30 tests across transcription, turn assembly, the shared upload helper, the
  memo writer and the silence guard.

### Changed

- **The chat mic uses Whisper when configured**, falling back to the Web Speech
  API. Noticeably better on German dictation, at the cost of no live preview
  while you talk. It inserts the **raw** transcript — you are composing your own
  message, and running it through the memo rewrite would put words in your mouth.
- Documents dialog and dashboard read `a.get("deadline") or "—"` rather than a
  dict default.

### Fixed

- **Whisper hallucinates on silence.** It does not return an empty string for
  non-speech — it returns a confident stock phrase ("Vielen Dank für's
  Zuschauen!"). An accidental press would otherwise file a completely genuine
  looking memo about nothing. `looks_like_silence()` is the guard; an
  `if not text` check is not sufficient.
- **Extended thinking dropped the block signature.** `_block_to_dict` did not
  preserve `signature` on thinking blocks, which the API verifies when one is
  replayed alongside `tool_use` — this would have failed the second iteration of
  every agentic loop once thinking was enabled.
- **A dateless action rendered as "None".** `.get("deadline", "—")` never fired
  its default because `action_dedupe` writes an explicit `None`.
- **The memo dialog's status line stuck on "Transcribing …".** The recorder
  script writes the label straight into the DOM, so Python's copy of the prop
  never changed and assigning the same value back produced no update. The label
  is now owned by one side only.
- **A long conversation pushed Save and Discard off-screen.** The card had no
  height ceiling, so an autogrow textarea grew it past the viewport. Capped at
  `88dvh` with the transcript as the only scrolling child — `dvh` because on
  mobile `vh` ignores the browser's collapsing toolbar, which is exactly where
  the buttons went missing.
- **A memo could fail because of the *brain* folder.** `create_memo` called
  `ensure_user_dirs()`, which creates a directory a memo does not need. Vault
  directory creation is also no longer fooled by a bind-mounted or FUSE-backed
  mount, where an existing directory can answer `EPERM` instead of `EEXIST` and
  `exists()` can report `False` when the mount refuses the `stat`. A genuine
  failure now names the mount instead of surfacing a bare errno.

## [0.3.0] — 2026-08-03

A visual discipline pass over the whole UI, plus three fixes that landed after
`v0.2.0` and had not yet shipped in an image.

The organising rule, now recorded as an invariant in `CLAUDE.md`: **colour
encodes state, not identity.** Tag chips are the single exception — their hue
comes from Paperless, the user assigned it, and it is what makes tags groupable
across documents at a glance.

### Fixed

- **Sync log went blank mid-run.** The log was appended straight to the dialog
  column, so a websocket drop — routine on a document that takes twelve minutes
  — swallowed every later line. The page froze on whatever it had received and
  looked like a hung sync while the task ran to completion server-side.
- **A slow Paperless could abort a running sync.** The metadata map refresh ran
  on httpx's 5 s default timeout, which a Paperless busy with its own consumer
  exceeds. The resulting `ReadTimeout` propagated into the caller. It now has a
  30 s timeout and serves the previous maps when a refresh fails.
- **Buttons and checkboxes rendered blue.** Quasar's `primary` was never set, so
  it stayed the stock blue, and `ui.button` defaults to `color='primary'`. The
  rule `.bg-primary{…!important}` then beat the `bg-purple-700` classes the
  pages set — twenty buttons read purple in the source and rendered blue on
  screen. `primary` is now the brand purple.
- **Sentence case never rendered.** Quasar ships `text-transform: uppercase` on
  `.q-btn` and `.q-tab`, so editing a label string had no visible effect.
  Overridden once, globally.
- **Deadline list buried everything actionable.** Upcoming deadlines were sorted
  descending from the furthest future date, putting a 2064 pension date above
  anything due this month.
- **"Show all" crashed the deadline table.** The button lives inside the
  container its own handler clears, so the follow-up `ui.run_javascript` could
  not resolve the client through the deleted slot.
- **Relevance was reported inverted.** The figure shown was a raw ChromaDB
  cosine *distance* — lower is better — displayed under a "Relevance" heading
  and sorted ascending, so a correctly ordered list read as least-relevant
  first. It is now converted to a percentage and hidden by default.
- **Unreadable text on filled buttons.** Light-purple labels on the accent
  background measured 3.2:1, below WCAG AA. Now white.
- **Progress ring read as complete at 98 %.** `stroke-linecap="round"` added
  8 px of cap against a ~5 px gap.

### Changed

- **Tag chips** keep their Paperless hue but are clamped to a fixed perceptual
  lightness in OKLCh, so no tag can outshout another. Cards show at most four,
  sorted alphabetically so the visible set is stable across renders, with the
  remainder behind a muted `+N` that expands on click.
- **`Actions` is now `Action required`**, moved out of the tag row onto its own
  line with warning semantics. A noun did not say what was being asserted, and
  system state does not belong among the user's own tags.
- **Icons are monochrome.** Nav, card actions, dashboard footers and section
  headers all inherit the neutral text ramp. The only coloured icons left encode
  state: sync-running, no-model-configured, task done, and the pinned marker.
- **Purple means two things**, the active selection and the user's own input.
  Neutralised elsewhere: document ID links, model and backend select borders,
  calendar deadline fills, relevance badges, and the per-user identity hue that
  used to colour document owners.
- **The active nav item is marked** with the accent plus an underline, so the
  state survives a greyscale screenshot and colour-blind viewing.
- **A document card opens from its body**, which retired the preview icon. Three
  icons per card across eight cards is a lot of competing targets.
- **Suggestion chips** use icons from the app's own set instead of emoji, read
  in sentence case, and fade at the scroll edges rather than clipping a chip
  mid-glyph.
- **Deadlines** list upcoming first, nearest first, then overdue most-recently
  missed first. The overdue counter click-throughs to a filtered worklist.
- **The calendar** marks a deadline with a dot under the date instead of a
  saturated fill.
- **The sync status card** drops the progress ring — `461 / 467` and
  `98 % indexed` already sat beside it — and its labels are sentence case
  (`Paperless-ngx`, `Brain vault`) with monochrome sync, log, vault and dream
  icons.

### Added

- `SHOW_RELEVANCE_SCORES` — debug flag, off by default, surfaces retrieval
  scores on result cards and in the detail dialog.
- `app_ui/tag_style.py` — the single path for rendering a tag chip, with the
  Paperless colour map cached for 15 minutes, primed at startup and invalidated
  on manual sync.
- Unit tests for the colour clamp and the relevance direction.
- A UI visual discipline section in `CLAUDE.md`, plus the two task documents the
  pass was worked from under `docs/`.

### Documentation

- Install instructions no longer require cloning the repository.

## [0.2.0] — 2026-08-02

First semantically versioned release. See the
[release notes](https://github.com/vailsen/paperless-brain/releases/tag/v0.2.0).

[0.4.0]: https://github.com/vailsen/paperless-brain/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/vailsen/paperless-brain/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/vailsen/paperless-brain/releases/tag/v0.2.0
