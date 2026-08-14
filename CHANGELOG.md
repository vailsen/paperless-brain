# Changelog

All notable changes to PaperlessBrain are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning is [semantic](https://semver.org/) from `v0.2.0` onward.

## [0.7.0] — 2026-08-14

**Like Obsidian, without needing Obsidian — and an answer in the language you
asked in.**

The Memory page could edit exactly two things: agent-curated facts and manual
deadlines. Everything else in the vault — to-do notes, memos, logs — was
reachable only through a shell on the server or an Obsidian client mounted over
WebDAV. Neither is something you can hand to a person with a phone.

So the module became a note editor: folder tree on the left, the note on the
right, frontmatter as typed properties above it. It is now called the **Note
vault**, because it stopped being only the agent's memory the moment it could
edit every note in the folder. Obsidian still works on the same directory, at
the same time — the editor is one more client, not a new format. That claim is
load-bearing, and it is why an untouched property is written back byte for byte
rather than re-serialized, and why two people editing the same note merge
instead of overwriting.

The other half of this release is a language bug worth describing honestly: an
English question about a German invoice came back in German. The rule said "do
not switch language because of a document" — but never said what to *do* with
the German summary the model had just read, and repeating what you read is the
path of least resistance. Saying "translate it" was still not enough, because
the rule sat in the system prompt while the German text sat immediately before
the answer. It is now re-asserted after the tool results, in the language you
actually typed in rather than the one the UI is set to.

### Added

- **Note explorer and editor at `/brain`.** Create, rename, move and delete
  notes and folders; edit any `.md` in a CodeMirror markdown editor with a
  rendered-preview toggle. Attachments (PDFs, images) are visible in the tree
  and can be opened read-only via `GET /api/vault/file`; there is no upload.
  `.gitignore`, `*.conflict.md`, `*.tmp` and dot-directories stay hidden.

- **Reading is the default, and the editor shows only the note.** A note opens
  rendered; the pencil switches to source. The `---` block is no longer in the
  editor at all — it is the properties panel's job, and two editable copies of
  the same bytes drift apart. The panel is expanded by default, since it now
  holds the half of the note the editor does not show.

- **Renamed: "Memory" → "Note vault"** (de: "Notizspeicher"), with a notes icon
  instead of the head. The module stopped being only the agent's memory the
  moment it could edit every note in the vault. The dashboard's module overview
  says so too.

- **The tree behaves per device.** On a desktop a click selects *and* opens —
  both panes are on screen, so there is nothing to protect. On a phone the tree
  is a drawer, and a tap that opened the note would close the drawer over the
  toolbar it was aiming for; so there the first tap only selects (outlined in
  the accent colour, with a "Selected: …" line, and the create dialogs naming
  the target folder), and any later tap on that same node opens it — no time
  limit, because Quasar reports a tap on the selected node as "unselect", which
  is exactly the signal needed. A phone also lands on the tree instead of an
  empty canvas.

- **Properties panel.** Frontmatter as typed rows: `pbrain_id` shown but never
  editable (and never *minted* here — the indexer assigns it, and a second
  identity path is the one thing this subsystem must not grow), `dont_ingest` a
  switch outside the brain folder, `tags` a chip input that shows you the
  sanitised value it stored, everything else free-form, plus add/remove and an
  "Edit YAML" escape hatch for nested maps and block scalars the row UI cannot
  represent. `pbrain_id` is hidden from that YAML too and restored verbatim
  afterwards: change it and the old index entry is orphaned while the note
  re-embeds as a stranger — the escape hatch is for properties, not identity.

- **Deferred indexing, visibly.** Autosave writes the file and stops there: no
  commit, no embedding. The dirty working tree *is* the pending queue, drained
  by the `sync_user()` that already runs at the start of every chat turn — so an
  edit is indexed before the retrieval that needs it, and a session of edits
  costs one commit instead of one per keystroke. A muted dot marks notes that
  are written but not yet indexed, and "Index now" runs the sync on demand.

### Changed

- **The fact and deadline cards are gone.** They were a second editor for a
  subset of the same files. Sharing a fact is now the `common:` property, a
  manual deadline is `kind: deadline` + `due:` — both read back into Chroma by
  sync exactly as before, so the chat tools and the dashboard are unaffected.
  `VaultBrainWriter.set_common()` went with the UI that was its only caller.

- **`vault/frontmatter.write()` is no longer the only writer.** It re-dumps the
  whole YAML block, which is fine when an agent builds the dict and destructive
  when a human owns the file: comments, key order and `2026-08-14T10:00:00`
  versus `2026-08-14 10:00:00` do not survive it. With autosave that meant
  *opening* a note would rewrite it. The new `vault/note_text.py` splits a note
  losslessly and rewrites only the keys that changed, so a save with no edits
  produces identical bytes.

### Fixed

- **An English question about a German document gets an English answer.** Two
  fixes, because the first was not enough on its own:
  - The rule told the model not to switch language because of a document, but
    never said what to do with the German summary it had just read — and
    repeating what you read is the path of least resistance. The directive now
    orders the content **translated**, names summaries and extracted fields
    explicitly, and exempts proper names, document titles and quotations.
    Werkbank roles share the directive and get it too.
  - That still lost to position: the rule sits in the system prompt, thousands
    of tokens away, while a long German document summary lands immediately
    before the model answers. So the answer language is now **re-asserted right
    after the tool results**, the same recency fix the tool-use guard already
    used. And the language is taken from **what the user typed**, not the UI
    setting — `detect_language()` reads the message, falling back to the UI
    language when it is too short to tell (a bare `#42` must not flip it).

- **The assistant no longer offers to edit notes it cannot edit.** It has always
  been able to write only its own memory — the user's notes are read-only to it
  — but nothing said so, so it would helpfully propose "shall I add that to your
  note?" and then have no way to do it. `vault_search` now states it is
  read-only and forbids the offer, `create_note` says it comments on a
  *Paperless document* rather than a note, and the memory tools say the memory
  is the agent's own. The `vault` and `memory` prompt blocks draw the same line,
  and `tests/test_tool_store_boundaries.py` fails if a future tool blurs it.

- **The voice memo's Memo/Conversation switch reads as a switch again.** Both
  halves were filled: the `color=purple` prop paints the *unselected* segments,
  and in Quasar's own Material purple rather than the brand one, so the control
  showed two clashing colours and no clear active state. The inactive half is
  now outlined only, and the active one takes the accent from the theme token
  (by rebinding `--q-primary` on the container, instead of an `!important` duel
  with Quasar's `.bg-primary`).

- **The download button on a document card works behind a reverse proxy.** It
  sent the browser straight at `PAPERLESS_URL` — which is how *the server*
  reaches Paperless, an internal address the browser either cannot resolve or
  has no session for, so an outside user got "unauthorized". The card now
  fetches server-side through the user's own session client and hands the bytes
  to the browser, keeping the filename Paperless reports. (The PDF button in the
  document dialog was always fine: it serves the app-relative `/pdftmp/…`.)

- **A Werkbank run now syncs the vault before it starts.** Workers reach for
  `vault_search` and `brain_search`, but nothing triggered a sync on that path —
  a run read whatever the last chat turn happened to index, so a note written
  minutes earlier was invisible to the task started to act on it.
  `orchestrator.run_task()` syncs once per run, before the split, and a failing
  sync is logged rather than taking the task down.

- **Editing in two places at once no longer loses either side.** Saves compare a
  sha256 of the file under the per-user lock; on mismatch a three-way merge runs
  first, so sync writing `pbrain_id` into a note you are typing in is invisible
  rather than alarming, and only a genuine overlap raises the conflict banner
  (with a diff, and reload / overwrite / save-as-copy). An external delete is
  caught too, and a note moved in Obsidian can be followed by its `pbrain_id`.
  The same lock closes a narrower hole: a write landing inside sync's
  `git add -A` … `commit` window used to be committed without ever being
  embedded, and stayed invisible to search until the file changed again.

## [0.6.0] — 2026-08-13

**Search that finds the mail, memory that knows where it lives, and a model
that admits what it does not know.**

The email tool could not find a German word. Not "found it late" — never, for
any query containing an umlaut, since the feature shipped. The cause turned out
to be smaller than any theory about Gmail's index: `imaplib` encodes string
command arguments as ASCII, so the query raised `UnicodeEncodeError` *inside the
library* and the surrounding `except` reported it as "no emails found". The
query never reached the server at all.

Vault and brain retrieval now embed where a note lives, not only what it says. A
chunk reading "change the brake pads" is ambiguous alone and unambiguous under
`To-Dos/Car.md`, so the folder and heading breadcrumb go into the vector — in
one short line, because a long header would drown out the chunk it is meant to
disambiguate.

And a model asked "what did I buy?" that answers without calling a tool did not
remember anything; it invented it. Models can now be marked per entry as unable
to use tools at all, or as required to reach for one when the question is about
the user's own data.

### Added

- **Standing chat instructions.** Settings > AI > Chat instructions is a
  per-user text box appended to every chat system prompt — for describing your
  archive (what is in it, your correspondents, how your reference numbers look)
  and how you want answers written. Capped at 2000 characters with a live
  counter, because the assembled prompt is already ~3k tokens and a local model
  has to fit the conversation into what is left.

  Deliberately additive only: the per-tool-group blocks stay in code, since they
  are calling contracts and a renamed tool there stops being called with no
  error to explain why. The block is also subordinated to the ground rules
  rather than merely appended — later prompt text outweighs earlier text, so an
  unframed user block would quietly outrank the rules that stop the assistant
  inventing document IDs, amounts and dates.

- **Per-model tool flags.** "OpenAI-compatible" describes the wire format, not
  the behaviour behind it. Each model in Settings > AI models now carries
  *Model can use tools* (off = never offered any, shown as a "no tools" badge)
  and *Require a tool for personal questions*. The second one drives a guard
  that inspects the question, never the answer: a question about the user's own
  mail, invoices or calendar is unanswerable from model weights by construction,
  so the turn is retried once with `tool_choice: required`. At most one forced
  retry per turn — a model that refuses costs one extra request, not a loop.

- **Context-aware embedding for vault and brain.** `vault/context.py` prepends a
  one-line header — `[To-Dos] Car › Brakes` — to every chunk before embedding,
  and puts folder, filename and title into metadata where they can be filtered
  and cited without touching the vector. The same schema for both collections,
  or a distance from `brain` stops being comparable to a distance from `vault`.
  Brain writes by the agent build the identical header, so a fact written now
  and the same fact re-embedded by a later reindex land in the same place.

- **Reindex on embedding-schema change.** `EMBEDDING_SCHEMA_VERSION` is recorded
  per user and compared on sync; a mismatch forces one full rebuild, then marks
  itself current so it does not run again. Also available as a manual button
  with per-file progress.

- **Preview images on web results.** SearXNG only passes `img_src` through when
  the answering engine supplies one, which is why cards were sometimes all
  illustrated and sometimes none. The top results now get their `og:image`
  fetched in parallel; every card ends with a picture or the same placeholder,
  never a gap.

- **`scripts/imap_debug.py`** — runs one search against a real account with the
  IMAP traffic visible. The email path has no other way to be checked end to
  end: credentials are encrypted per user, the server is remote, and the
  interesting failures are invisible from the outside.

### Changed

- **The response language follows the user, not the interface.** The old
  directive said "always respond in *X*, regardless of…", which conflated two
  different rules. Keeping a German invoice from dragging the reply into German
  is right; overriding the language the *user* writes in is not — a German
  question in an English interface is the normal case here. Models that follow
  instructions literally obeyed the old wording and answered German questions in
  English, while looser ones ignored it and did the sensible thing. The
  instruction was at fault, not the models. Documents still get no vote.

- **Voice memo audio is never written to disk.** Failed recordings used to be
  parked under `data/memo_failed/` and the path named in the error. It was meant
  to make a failure recoverable; in practice it accumulated the user's voice in
  a folder nobody looks at and put a filesystem path in front of the one person
  who could not use it. The audio is dropped and the notice says what went wrong.

- **"Max. tool calls" is now "Max. model rounds".** It always capped agentic loop
  iterations, and one iteration can fire several tool calls in parallel, so 40
  meant 40 model round-trips and potentially far more calls. The chat badge shows
  both: `search · 3/40 · 7 calls`.

- **Gmail accounts are detected by capability, not hostname.** `X-GM-EXT-1` in
  CAPABILITY rather than "does the host contain 'gmail'", so custom-domain
  Google Workspace accounts get Google's own index too.

### Fixed

- **Email search found nothing for any query containing an umlaut.** `imaplib`
  encodes str command arguments as ASCII (`IMAP4._encoding`), so the query
  raised `UnicodeEncodeError` inside the library on all three paths — X-GM-RAW,
  `SEARCH CHARSET UTF-8` and the ASCII fallback — and the surrounding `except`
  made it indistinguishable from an empty result. Criteria are now built as text
  and handed to `search()` as bytes with `CHARSET UTF-8`.

  Two further faults surfaced on the first live run and are fixed as well: Gmail
  answers `OK` to 8-bit inside quotes and then matches nothing, so non-ASCII is
  sent as an IMAP literal — the only form RFC 3501 defines for it; and results
  came back oldest-first, so a search for a recent order returned mail from 2011.

- **Folder names with umlauts could not be selected.** IMAP carries them in
  modified UTF-7, so `Bestellvorgänge` arrives as `Bestellvorg&AOQ-nge`. Names
  are decoded at the tool boundary and re-encoded before `SELECT`: the model
  never sees the encoded form and never has to produce it.

- **Mail bodies lost the words that were being searched for.** Payloads are now
  decoded through quoted-printable/base64 (`Drehmomentschl=C3=BCssel` matched
  nothing), and HTML bodies are flattened keeping `<img alt>` text and link
  slugs — shop mails carry the full product name there long after the visible
  subject has been truncated.

- **Whisper's invented transcripts are discarded.** A short recording of silence
  came back as a fluent, structured slab of memorised broadcast boilerplate —
  the stock-phrase list cannot catch those, since they are long and never the
  same twice. The guard is arithmetic instead: speech has a maximum rate, so a
  recording only holds so many characters. Both constants are slack (12 kbps
  assumed against the 24–64 browsers write; 25 chars/s against fast speech at
  ~20), so a real 30-second memo comes out about four times under budget while
  the 1200-character transcript from under two seconds of audio was ten times
  over it.

- **A mis-tapped memo button reads as "nothing was recognised", not an error.**
  Recordings too small to hold speech never reach the transcription service, and
  a service failure on a few seconds of audio reports the plain message rather
  than the raw 500 — the two are indistinguishable from where the user sits and
  the remedy is the same. A failure on a longer recording keeps the real error.

- **The memo dialog kept its old text after saving.** NiceGUI sends nothing when
  an assigned value equals its Python-side copy, and the reset that runs on close
  had already set it to `""` while the browser still showed the transcript — so
  the reset on reopen was a silent no-op. Reproduced and fixed against the real
  dialog under Playwright.

- **Tool calls went missing from the thinking log.** The label was written onto
  the last existing entry, so several tool calls in one iteration overwrote each
  other and only the last survived; a tool call arriving before any thinking text
  was dropped entirely. The trace dialog had them all along, which is what made
  the two disagree.

- **Chat settings reset themselves on "New chat".** Tool group toggles and the
  iteration cap were reset to defaults while the settings panel kept rendering
  the user's values — so the next conversation silently ran with 16 rounds and
  the default tool set no matter what was on screen.

## [0.5.0] — 2026-08-10

**Voice memos get a voice of their own, and the phone gets its screen back.**

Dictation is no longer wired to assumptions. The language you speak is now a
setting rather than a guess derived from the interface language — an English UI
with German dictation is the normal case, not an edge case — and it drives both
the transcription service and the browser's own recogniser. Which of those two
engines the chat microphone uses is likewise a preference: one recognises far
better, the other writes along while you speak, and no server can decide that
for you.

The mobile pass was measured, not eyeballed. Wide markdown tables now scroll
inside their bubble instead of dragging the conversation off-screen; long model
names truncate instead of walking out of their card; settings are grouped into
seven collapsible sections instead of fifteen equally distant cards. Each of
those had a cause that reading the CSS did not reveal — a Quasar scroll area
that sizes itself to its widest child, a flex minimum that outranks a maximum, a
column that sizes labels to their own content — so the fixes were verified by
rendering the real pages at 390px and measuring them.

### Added

- **The mic in chat can use either engine.** Settings > Voice memos now offers
  *Transcription service* (Whisper, default) or *Browser dictation* (Web Speech
  API) for the chat input's microphone. Whisper recognises far better but says
  nothing until the round trip returns; Web Speech writes along while you speak.
  Which trade-off is right is a preference, not something the server can decide.
  Whichever engine a browser cannot provide falls back to the other one.

- **Dictation language is its own setting.** The language you speak is not the
  language you read the app in — an English interface with German dictation is a
  normal combination, so it cannot be derived from the UI language. One setting
  drives both engines: it is sent to the transcription service (overriding
  `WHISPER_LANGUAGE` per user) and it sets the browser's recognition language,
  which otherwise defaulted to English and dropped every German word.

### Changed

- **A PWA switched away from no longer reloads on return.** `reconnect_timeout`
  goes from 30 s to 5 min, so a suspended phone tab reattaches to its existing
  session instead of being rebuilt from scratch — the half-typed message, the
  open document and the streaming answer survive. NiceGUI derives the socket
  keep-alive from that same number, which would have pushed pings past nginx's
  `proxy_read_timeout`; `_keep_pings_short()` in `main.py` pins them at 25 s/20 s
  so the two settings stay independent.
- **The memo status line names the phase it is in.** Recording, transcription
  and the AI cleanup are three different waits; the line said "Transcribing …"
  through the last two of them. The rewrite is now its own route
  (`POST /api/memo/rewrite`), so the dialog flips to "AI is tidying it up …" the
  moment the transcript is back. A failed rewrite keeps the raw transcript
  rather than losing the recording.
- **Settings are grouped into collapsible sections** (General, AI, Documents,
  Search & memory, Voice memos, Connections, Backup). Fifteen cards in one
  column put everything equally far away.

### Fixed

- The chat microphone's Web Speech recognition was pinned to `de-DE`. It now
  follows the dictation-language setting (see above) rather than either a
  hardcoded locale or the interface language.
- **Wide markdown tables scroll inside the chat bubble** instead of stretching it
  off-screen. Two independent causes, both fixed:
  - Quasar's `.q-scrollarea__content` is absolutely positioned with
    `min-width: 100%`, so the message list's content box grew to the max-content
    width of the widest thing in the conversation. Every bubble's `max-width: 85%`
    then resolved against *that*, and the list scrolled sideways rather than the
    table scrolling inside its bubble — which is why nothing appeared to overflow
    the screen. `.chat-messages-scroll .q-scrollarea__content` is now capped, the
    same fix the results panel already had. Measured at 390px: content box
    892px → 388px, bubble 704px → 275px.
  - A flex item's automatic `min-width: auto` is its content width, and min-width
    beats max-width, so the bubble's cap lost against a `max-content` table. The
    bubbles and `.chat-md` now carry `min-width: 0`.
- **Chat bubbles use more of a phone screen.** Below 768px the message list's
  gutters go 16px → 8px, the bubble cap 85% → 92%, and the bubble's own side
  padding 14px → 10px. Measured at 390px, an assistant bubble's content width
  goes 276px → 293px and the list itself 358px → 356px of usable width with far
  less waste around it. Both bubbles keep a visible offset, so left-aligned
  assistant and right-aligned user still read at a glance. Desktop is unchanged.
- **Table columns are no longer sized by their heading.** Inside a bubble the table
  is laid out at min-content, and `.chat-md`'s `overflow-wrap: anywhere` made a
  cell's minimum one character — so a narrow heading like "Ort" rendered
  "Karlsruhe" as stacked pairs of letters. Cells now wrap at word boundaries with
  a `6rem` floor. Measured at 390px on a five-column table: 4rem still broke
  "1.240,00 EUR" over two lines, 6rem put every value on one line for 509px of
  scroll, and 8rem only widened the scroll. Long prose in a cell still wraps.
- **Long model names no longer walk out of their settings card.** The labels had
  `text-overflow: ellipsis` all along, but `ui.column()` sets
  `align-items: flex-start`, which sizes a child label to its own content — there
  was no width to truncate against. Adding `w-full` is what makes the ellipsis
  work. Measured on the real page at 390px: the AI models card held 442px of
  content in 278px of card; now 236px in 236px.
- **Long strings no longer widen settings cards on a phone.** Vault paths, model
  ids and URLs in hints have no spaces, so a browser treats each as one
  unbreakable word; they now wrap or ellipsise.

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

[0.6.0]: https://github.com/vailsen/paperless-brain/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/vailsen/paperless-brain/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/vailsen/paperless-brain/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/vailsen/paperless-brain/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/vailsen/paperless-brain/releases/tag/v0.2.0
