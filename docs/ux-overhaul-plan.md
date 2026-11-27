# UX overhaul — navigation, findings, note vault, browser

Follow-up to `ui-polish-tasks.md` / `ui-polish-tasks-round2.md`. Those made the app
*look* consistent; this round is about how it *works* for someone who uses it every
day: where you land, knowing where you are, getting to a document or note in two
taps, and never changing something by accident.

The app is a working tool, not a casual app. Density, keyboard paths and precise
control are features; hand-holding is not.

Work it one phase at a time (`/clear` between phases). Every layout claim is verified
by measuring the real page with Playwright at 390 px and 1280 px (see "UI visual
discipline" in `CLAUDE.md`), not by reading CSS.

---

## 0. Answers to the questions behind the requests

**Is it a single-page app?** No. NiceGUI serves every module as its own route
(`/`, `/chat`, `/brain`, `/findings`, `/werkbank`, `/experts`, `/browser`), each a
server-rendered page with its own websocket. Switching modules is a page load. That
is why "where do I land" is a real question (every entry point is a URL), and also why
it is cheap to fix: a start page is a redirect, a shortcut is a URL. Converting to an
SPA (`ui.sub_pages`) is **not** proposed — the chat page alone is 5 000 lines of
per-page state, and the live-turn machinery (`_LIVE_TURNS`, `_persist_turn`) already
covers the one thing an SPA would buy (not losing a running answer).

**What are "tags" in a note's properties?** Obsidian's frontmatter `tags:` list.
PaperlessBrain writes them itself — `remember_fact` stores the model's topic labels,
deadlines get `frist`, research notes `research`, memos their topic tags
(`vault/brain_writer.py`, `memo_writer.py`, `research.py`) — and `vault/sync.py` copies
them into the Chroma metadata. **Nothing reads them back**: no search filters by them,
no UI groups by them. Today they are decoration that Obsidian shows and we don't. This
plan makes them useful (§4.6 `tag:` search, clickable chips) instead of hiding them.

**Why the chat's Library takes 3–4 s (fix in Phase 3):** `DocLibrary.reload()` calls
`list_documents()`, which fetches **full** documents — including `content`, the whole
OCR text of every document — 100 per page, one page after the other. On top, the chat
builds a fresh `PaperlessClient` per load, so the tag/correspondent/type/user maps
(cached per client instance) are fetched again each time. The tree needs none of the
text. So the fix has two halves: fetch only the fields the list needs, and keep the
result on the server until the next sync (§3.1).

**Bug found on the way (fix in Phase 3):** `pipelines/search.py` appends tag-name hits
(`_tag_search`, "query word equals a tag name") to semantic results **without
intersecting them with the filtered candidates**. A correspondent filter plus the query
"Rechnung" also shows documents tagged "Rechnung" from every other correspondent. The
chat's `search` tool uses the same function, so the model sees the leak too.

---

## Shipped first (2026-11-27)

Small things that did not need a phase:

- **App shortcuts** with badge icons (1.2a).
- **Library speed**: lean list fetch, shared name maps (3.1).
- **Reload after switching apps on a phone.** Three causes in NiceGUI's client, each
  addressed: a connection attempt that times out reloads (a waking phone radio can take
  longer than the default 20 s → the timeout is 60 s now); a page further behind than
  the replay buffer reloads ("outbox rewind failed" → `message_history_length` 5 000
  instead of 1 000); and Android discarding a background PWA outright, which no socket
  logic can prevent — so the half-typed chat message now survives it
  (`layout.DRAFT_JS`, per address, dropped on send). Every reload now logs its reason
  as `[revive]` (NiceGUI's own reason, or "discarded"), so what remains can be
  measured instead of guessed.
- **Model host offline**: a local model whose endpoint accepts no connection fails at
  once with its name ("The model host … does not answer"), instead of "The model host
  is starting…" until the connect timeout. A wake-on-request proxy always accepts, so a
  sleeping host behind one is still woken. Connect errors are named, not "Error: ".
- **Findings**
  - The dock's chat on the overview (home, a list, a folder) gets the overview as
    context: urgent findings, what is due in 60 days, other open findings, dossiers —
    built in code, fresh every turn (`findings_view.overview_context_text`), its own
    conversation (`findings:overview`) that the dock reopens.
  - "Pressing" is "Urgent" everywhere ("Mark as urgent"). A finding that is urgent by
    itself — a deadline within a week, a high urgency — says why ("Urgent: due in 3
    days") instead of showing no button, which read as "deadlines cannot be marked".
  - "Rescan pending" lists every dossier whose page offers "Rescan": changed by hand
    *or* with documents it does not cover yet (`dossiers.needs_rescan`). The night still
    rescans only the hand-changed ones.
  - Back gesture: every selection is a history step (`pushState`), and back/forward
    show what the address names — a finding opened from a dossier goes back to the
    dossier, not to the dashboard.

## NiceGUI 4

4.0.0 (2026-11-04) and 4.1.0 (2026-11-24) are out; the app pins `nicegui>=3.19,<4`.
Worth doing — as its own step, not mixed into feature work, because a lot here leans
on NiceGUI's client internals (socket revive, `next_message_id`, replay, Quasar CSS).
What 4.x changes that this code base must be checked for:

- **Prop names are no longer migrated** to kebab-case: a snake/camelCase name in a
  `.props("…")` string silently does nothing. Audit every `.props()` string.
- **Cleared text inputs hold `''`, not `None`** (`ui.input`, `ui.textarea`,
  `ui.date_input`): every `value is None` check on those.
- **DOM ids are `ng-12`, not `c12`**: any JS that builds an element id by hand.
- **httpx is no longer a NiceGUI dependency** — the app lists it itself (already).
- `ui.echart`: `:`-prefixed keys outside `dataset.source` are no longer evaluated as
  JavaScript (matters for Phase 6 charts).
- `run.cpu_bound` defaults to "spawn"; `ui.timer` callbacks must not swallow
  `CancelledError`; `app.on_startup(coro())` must pass the function.
- Quasar 2.18 → 2.35, Vue 3.5.44, socket.io 4.8.4, Starlette 1.10, uvicorn 0.54.
- 4.1: an expired login on reconnect reloads instead of "Connection lost" forever.

Plan: bump the pin, run the suite, then drive every module with Playwright at 390 px
and 1280 px (header, chat streaming, findings dock, vault editor, reconnect after a
dropped socket) before it ships.

## Phase 1 — Navigation: landing, knowing where you are, hiding modules

Shipped 2026-11-27, except the settings export of the two new prefs. The hidden-module
"banner" is a notice with a "Show it again" action at the top (the findings workspace
covers the page below the header with a fixed layout, which would hide a row).

### 1.1 One module registry

Today the module list is `NAV_ITEMS` + `FINDINGS_ITEM` + two special cases in
`nav_items()` (proactive switch, "Use experts"). Everything below needs the same list,
so first make it one:

- [x] `app_ui/modules.py`: `Module(key, icon, label, path, optional, available)` for
      `dashboard`, `chat`, `findings`, `vault`, `research`, `experts`, `browser`.
      `available()` holds the existing conditions (`PROACTIVE_ENABLED`); `optional`
      is False for dashboard and chat.
- [x] `visible_modules()` = available and not hidden by the user (1.4). The header,
      the ☰ menu, the help dialog, the start-page picker and the settings switch list
      all read it. `NAV_ITEMS` stays as the `N_()` label source only.
- [x] `module_for_path(path)` — used by the header label (1.3), the page title and the
      hidden-module banner.

### 1.2 Start page and app shortcuts

- [x] Per-user pref `start_page` (`services/user_prefs`): any visible module, plus
      **"Last used module"**. Default `dashboard` (no change for anyone who does nothing).
      Setting in Settings > General, next to "When the chat opens" — the two belong
      together ("what do I see when I open the app").
- [x] New route `/start`: reads the pref and redirects. `/` stays the dashboard, so the
      logo and the "Dashboard" item keep meaning the dashboard.
      `take_login_target()` falls back to `/start` instead of `/`; a push link's target
      still wins.
- [x] "Last used module" (decided: offered, not the default). Each page writes its
      module key on load (cheap pref write); `/start` sends there. It earns its place on
      a phone, where Android kills a backgrounded PWA and a cold start would otherwise
      always land on one fixed page. Module level only: a finding/dossier deep link
      counts as "findings" — reopening a stale half-state is worse than the module's home.
- [x] `manifest.json`: `start_url: "/start"` and `shortcuts` (1.2a).

### 1.2a App shortcuts on Android — one PWA, several home-screen icons

**How Android does it.** The web app manifest's `shortcuts` list (Chrome on Android
since M84) becomes the app's *launcher shortcuts*: long-press the PaperlessBrain icon and
they appear as a list — the same mechanism native apps use. Each shortcut has a name, a
URL inside the app and its own icon. And any of them can be **dragged out of that list
onto the home screen**, where it becomes an icon of its own that opens the PWA directly at
that URL. Most launchers (Pixel, Samsung One UI) draw the shortcut's icon with a small
badge of the parent app. So "an icon for Chat, one for Notes" is exactly this — no second
app needed. Windows shows the same list in the taskbar jump list; iOS has no manifest
shortcuts (the start-page pref covers it there).

**Several separate PWAs (own name, own icon, own start URL) — possible, not
recommended.** It would take one manifest per entry point, each with its own `id`, linked
from different pages (`/chat` links `manifest-chat.json` …), and the user installing each
from the right page. But all of them would share one origin, one scope (`/`) and one
service worker:

- a link or push notification would open in whichever of them Android picks — tapping a
  finding notification could land in the "Chat" app;
- every module links to every other (the findings dock *is* `/chat`), so each "app" would
  in practice contain the whole app under a different name;
- each install is its own WebAPK, updated (and asking to confirm icon/name changes) on
  its own.

The scopes cannot be separated without splitting the URL space, which the embedded dock
rules out. Shortcuts give the same result on the home screen without any of this.

- [x] Shortcuts (shipped): **Chat** (`/chat`), **Note vault** (`/brain`), **New voice
      memo** (`/?memo=1` — the dashboard opens and the memo dialog opens on it; the flag
      is taken out of the address). The dashboard is the app icon itself. The manifest is
      served by a route (`app_ui/pwa_manifest.py`): names follow the browser's
      `Accept-Language`, the memo shortcut is left out without a transcription service.
      It is the same for every user — Chrome fetches it without a session.
- [x] **Shortcut icons = the app mark with a corner badge** (shipped): the current icon, slightly
      smaller, with a solid circle at the bottom right carrying a monochrome glyph (chat
      bubble, note, microphone, grid). Same colours as the app icon — the badge is a
      shape, not a new colour. Rendered by `scripts/make_icons.py` from the `brand.py`
      path at 96 and 192 px, in an `any` and a `maskable` variant (safe zone respected so
      round launchers don't cut the badge), named with `ICON_VERSION`.
- [ ] Help text: installed apps pick up a changed manifest when Chrome next checks it
      (on launch, roughly daily) — shortcuts and the new `start_url` appear then; a
      reinstall is the fallback. A fresh login is not affected — it always goes through
      `/start`.

### 1.3 The phone (and narrow desktop) shows which module you are in

Today, in `pb-nav-compact`, the header is wordmark + ☰ + memo + help + user. Inside
Findings nothing says "Findings".

- [x] Replace the bare ☰ with a **module switcher button**: current module icon +
      label + `expand_more`, e.g. `[💡 Findings ▾]`. Same menu as today (active item
      highlighted, Paperless-ngx link at the bottom). This is the one place a phone user
      looks to answer "where am I" and "go elsewhere" — one control for both.
- [x] In compact mode the wordmark shrinks to the mark alone (`brand.py` already has
      the SVG) to make room. Keep the link to `/`.
- [x] Help moves into the user menu when compact (it is the least used of the four
      header buttons); memo stays — capture must be one tap from everywhere.
- [x] `document.title` per module ("Findings · PaperlessBrain"), so the OS app
      switcher, browser tabs and history say where each one is.
- [x] `NAV_FIT_JS` keeps deciding *when* to collapse — it measures, so the wider
      switcher button is accounted for automatically.

Rejected: a bottom tab bar. The chat input, the findings dock and the phone keyboard
already compete for the bottom edge; another 56 px there costs the chat a line of text
on every screen. The switcher button gives the same "where am I" for zero height.

### 1.4 Hide modules (Settings > General > Modules)

- [ ] Per-user pref `hidden_modules: list[str]` of `browser`, `vault`, `experts`,
      `findings`, `research`. Chat and dashboard are listed as switches that are on and
      disabled, so the user sees why they cannot go.
- [x] **Hidden means out of the way, not off:**
      - gone from header, ☰ menu, help dialog, start-page picker, dashboard links and
        cross-module buttons (e.g. the chat's "Deep research" hand-off);
      - the **URL still works** — push links, citations in research notes and old
        bookmarks keep opening — with a thin banner "This module is hidden in your
        settings · Show it again";
      - background work is unaffected: hiding Findings does not stop the proactive
        worker or push (the banner links to the notification settings); hiding the vault
        keeps memory and memos working (the memo dialog still says where it saved).
- [x] **Experts: one switch, not two.** Hiding Experts *is* today's "Use experts" off
      (`experts.enabled()`, `_apply_expert` coerces to Universal). Migrate
      `experts_enabled=false` into `hidden_modules` once and remove the separate switch
      from Settings > AI; `experts.enabled()` reads the new pref.
- [ ] Settings backup/export (`data` group) includes both new prefs.

**Acceptance:** at 390 px inside every module the header names the module; Findings
hidden → not in the menu, `/findings/<id>` from a push still opens with the banner;
start page "Chat" → a fresh login and the installed app open the chat; Experts hidden ≡
old switch off (existing `tests/test_experts.py` stays green).

---

## Phase 2 — Findings: zone tabs at the top on phone/tablet

Shipped 2026-11-27 (plain underline tabs, as asked).

Below 1024 px the workspace is three zones switched by `.pbm-mbar`, a segmented control
fixed to the bottom. It looks like a second, foreign navigation, it sits *under* the
chat input when the chat zone is open, and "Viewer" says nothing about what is shown.

- [ ] Move the zone switch to the **top of `.pbm-root`** (a grid row under the header),
      full width, same segmented-pill look as `.pbm-seg` today. Delete `.pbm-mbar`;
      `.pbm-root` gets its 48 px bottom back — the chat input then sits on the screen
      edge exactly as on `/chat`.
- [ ] Labels: **"Browse"** (account_tree) · **the selected thing's kind** — "Inbox",
      "Finding", "Dossier", "Folder" (article; the item's title as tooltip/aria) ·
      **"Chat"** (forum; a small dot while an answer is streaming in the dock).
- [ ] Visual hierarchy, so the two rows never read as one: the zone switch is the pill
      control; a dossier's own sections (Overview / Timeline / Findings / …) keep the
      underline `.pbm-tabs` *inside* the viewer.
- [ ] The maximized-dock rules (`dock-max`) and `_zone()` keep working unchanged — only
      the container of the buttons moves.
- [ ] Desktop (≥1024 px) is untouched: navigator column, viewer, dock column.

**Acceptance:** 390 px and 800 px — no element fixed to the bottom besides the chat
input; the zone tab names the selected kind; switching zones never scrolls the page;
`pbmDockHeal` and the drag handle still work in the chat zone.

---

## Phase 3 — Browser overhaul: facets, live results, card/list views

Shipped 2026-11-27. The list is not cached (§3.1).

Today: an AI search field, a Search button, "Advanced filters" collapsed behind a link
(full text, id, tags, correspondent, type, owner, two date widgets), a sort select,
cards. Every change needs a button press, and filters are a form, not a view of the
archive.

### 3.1 A fast document list — no cache (decided)

The grouped tree already exists — the chat's right drawer → Library
(`app_ui/doc_library.py`, also on the experts page). It is not rebuilt here; the browser
uses the same data source. Why it was slow: §0.

- [x] **Lean fetch** (shipped): `PaperlessClient.list_catalogue()` asks
      `/api/documents/` with `fields=` for the list's keys only (no `content`), 250 per
      page, the remaining pages in parallel (4 at a time); the name maps are shared by
      every client of the same token (`_SHARED_MAPS`). The Library and an expert's
      scoped Library (`ScopedPaperlessClient.list_catalogue` → `scope.allows`) use it.
- [ ] **No server cache.** With the lean fetch the list loads in well under a second,
      so it is fetched when needed, with the user's own token — nothing to invalidate,
      nothing that can be stale after a change in Paperless, nothing that holds titles
      for users who are not there. Revisit only if a large archive measures slow.
- [ ] The browser loads the list once per page and filters in memory (§3.2–3.4);
      above `doc_library.MAX_DOCS` (5 000) it falls back to server-side filtering.
- [ ] A document deleted in Paperless since the list was loaded answers 404 on open —
      the dialog says "No longer in Paperless".

### 3.2 Facet panel

- [ ] Left column on desktop, **a full-height sheet on phone through
      `app_ui/multi_pick.MultiPick`** (the existing invariant for long pickers).
- [ ] Facets: Tags · Correspondent · Document type · Storage path (only if any are used)
      · Owner · Created (year → month tree) · Added (7 days / 30 days / this year /
      range). PaperlessBrain-generated facets in their own group below, never mixed
      into the tag list (the "tag row = Paperless data only" invariant): "Has
      action / deadline", "Pinned", "Has pictures".
- [ ] Each value shows its **count within the current result**, computed the
      disjunctive way (counts of a facet ignore that facet's own selection, so picking
      one tag still shows what the others would add). Zero-count values are dimmed, not
      removed — they still say "exists, but not here".
- [ ] Semantics **identical to an expert scope**: values within a facet OR (tags: an
      any/all switch), facets AND, and a second tap on a value *excludes* it (shown
      struck through) — the same model as `exclude_*` in `services/expert_scope.py`, so
      the user learns it once. Follow-up idea: "Create expert from these filters".
- [ ] Active filters as removable chips above the results, with "Clear all".
- [ ] Each facet list has its own quick filter field once it exceeds ~12 values.

### 3.3 Live results

- [ ] No Search button for filters: any facet/date/sort change re-renders at once.
- [ ] One search field with two behaviours: typing filters titles/correspondents
      instantly (in memory); the **AI search** runs debounced (≈600 ms after typing
      stops) or on Enter, with an inline spinner. The existing `_search_seq` guard
      drops stale answers.
- [ ] Paperless full-text search (`query`) and "Go to ID" stay, as a mode of the field
      (`#241` jumps straight to the document; a toggle or `ft:` prefix for full text).

### 3.4 Filter first, then semantic

- [ ] The candidate set is the in-memory filter result; the semantic query runs only
      over it (`where={"paperless_id": {"$in": ids}}`, as `search()` already does).
- [ ] **Fix the leak from §0**: tag-name hits are intersected with the candidates
      before they are appended — in `pipelines/search.py`, so the chat tool is fixed
      too. Test: a filter that excludes the tagged document must exclude it from the
      semantic result as well.
- [ ] Semantic results beyond the first 20: "Show more" (today they are cut silently),
      and weak matches below the model's archive-hint distance
      (`embedding_registry.thresholds`) folded under "N weaker matches".

### 3.5 Views

- [ ] View switch **Cards / List / Grouped**, remembered per device (`localStorage`,
      like the dock width):
      - **Cards** — today's card with thumbnail.
      - **List** — one dense row: title, correspondent, created date, type, up to four
        tag chips (`tag_style.render_tag_chips`), action flag, pages. Sortable column
        headers on desktop; on a phone two lines (title / meta).
      - **Grouped** — the existing `DocLibrary` component (by correspondent, tag or
        type) over the *filtered* set: one tree implementation for chat, experts and
        browser.
- [ ] Sort: created, added, title, correspondent, type; relevance only while an AI
      query is active (and then the default).

### 3.6 URL state

- [ ] Filters, query and view live in the URL (`/browser?tag=12&corr=3&view=list`,
      `replaceState` like the findings page): back button works, a filtered view can be
      bookmarked or used as a start page, and the dashboard can link to "overdue" or a
      tag directly.

**Acceptance:** the chat's Library and the browser show the list in under a second
with ~500 documents; an expert's Library never lists a document outside its scope
(test); a facet click re-renders in under 150 ms;
filter + AI query never returns a document outside the filter (test); 390 px — no
horizontal scroll in any view, facet sheet closes with the back gesture.

---

## Phase 4 — Note vault A: safe by default, searchable

Shipped 2026-11-27, together with Phase 5. "By meaning" in the vault search is not built: the literal search covers what the plan asked; the chat's `vault_search` stays the semantic path.

Everything here works with today's CodeMirror editor; Phase 5 swaps the editor
underneath without touching it.

### 4.1 Managed folders are read-only until unlocked

The folders PaperlessBrain writes — `PaperlessBrain Memory`, `… Research`,
`… Experts`, `… Skills`, `… Findings` (names from `config/settings.py` and
`proactive/paths.py`, never hard-coded) — hold state other parts of the app parse.
A stray keystroke in a finding's frontmatter changes its status; one in an expert
file changes the expert.

**`PaperlessBrain Memos` is the exception (decided):** memos are the user's own dictated
notes, so they open and edit like any other note. Only the folder itself is protected —
like every managed root, it cannot be renamed, moved or deleted (below).

- [ ] `vault/managed.py`: `managed_root(rel) -> str | None` (all six roots, for the
      folder protection) and `is_locked(rel) -> bool` (the roots above, without Memos,
      for the note lock) — the single place that knows either.
- [ ] Tree: managed folders show a small lock; notes in them open **read-only**
      (reading view, no editor cursor, no autosave tick, checkboxes disabled).
- [ ] Title bar of such a note: "Managed by PaperlessBrain · read-only" and an
      **"Unlock to edit"** button — secondary style, on the right, *never* where the
      editor's first tap lands, and it always goes through a confirm dialog that says
      what the folder is for:
      - Memory → "The chat's memory. Changes take effect in the next chat."
      - Findings → "Findings and dossiers are managed on the Findings page. Editing the
        file can change their state." + button "Open in Findings".
      - Experts / Skills → "Edit this in Experts" (primary) / "Edit the file anyway".
      - Research → plain confirm.
- [ ] **Unlock scope: this note, until another note is opened** (not stored, not per
      folder). A locked badge returns on reopen.
- [ ] Tree operations inside locked folders (new, rename, move, delete, drag in/out)
      go through the same confirm; inside Memos they are free. **The six managed root
      folders themselves can never be renamed, moved or deleted** — Memos included, even
      unlocked: a renamed folder silently orphans everything (see the warnings in
      `config/settings.py`). Their ⋯ menu simply has no such entries.
- [ ] This is a UI guard against accidents, not security: agent writes
      (`brain_writer`, `memo_writer`, findings) are unaffected.

### 4.2 Properties: read by default, edited on purpose

- [ ] The properties panel shows values read-only: chips for lists/tags, a
      check/dash for booleans, plain text otherwise; dates as written in the file
      (`_raw_scalar`, unchanged).
- [ ] **"Edit properties"** button on the panel → today's widgets (switches, tag
      select, text fields, Add property, Edit YAML) + "Done". Closing the note or
      opening another leaves edit mode.
- [ ] In locked folders the button appears only after "Unlock to edit" (memos have it
      like any note).
- [ ] `pbrain_id` collapses into an "Identity" row with copy button — always read-only.
- [ ] `common` (brain facts) stays a deliberate action: read-only badge "Shared with
      everyone", the switch only in edit mode.
- [ ] Panel open state remembered per device; collapsed shows a one-line summary
      ("tags: steuer, 2025 · common") instead of just "Properties".
- [ ] Tag chips are clickable → content search `tag:<name>` (4.3).

### 4.3 Search contents, not just names

Today the tree field filters node labels (`search.bind_value_to(tree, "filter")`).

- [ ] `vault/search.py`: scan the user's `.md` files (resolved through
      `vault.notes.resolve`, skipping hidden/`.git`/attachments) in a thread; per-file
      mtime cache of the text. Vaults are hundreds to low thousands of small files — a
      linear scan is milliseconds and needs no index (and must not get a SQLite one:
      the vault do-not list).
- [ ] Query language, small on purpose: words AND, case- and accent-insensitive;
      `"exact phrase"`; `tag:x` (frontmatter tags and inline `#x`); `path:folder`;
      `-word` to exclude.
- [ ] While a query is typed, the tree is replaced by results grouped per note: name
      matches first, then up to three snippet lines with the hit highlighted, and a
      count. Clearing the field brings the tree back with its expansion state.
- [ ] Opening a result scrolls to the first hit and highlights it in the note.
- [ ] Toggle **"By meaning"**: the same `vault_search` the chat uses (Chroma), with a
      hint that notes edited since the last sync are not in it yet ("Index now").

### 4.4 Tree interactions

- [ ] **One tap opens.** Drop "Tap to select, tap again to open" — actions move to a
      per-node menu (`⋯` on hover/desktop, long-press on phone, right-click):
      New note here · New folder here · Rename · Move to… · Delete.
- [ ] **Move**: drag and drop on desktop, "Move to…" folder picker everywhere
      (`notes.move` exists; nothing in the UI calls it today).
- [ ] Title bar: breadcrumb (`Projects › Tax › 2025 notes`) instead of the raw rel path;
      the note name is editable in place (= rename).
- [ ] Quick switcher **Ctrl/Cmd+O**: fuzzy jump by note name; recent notes at the top.
- [ ] New note: created in the folder of the current note/selection, opened with the
      cursor in the body.

**Acceptance:** a note in `PaperlessBrain Findings/` cannot be changed by typing,
tapping a checkbox or a property without passing the confirm; a memo edits without one;
no managed root (Memos included) can be renamed, moved or deleted; search for a word that appears only inside a note body finds it, `tag:steuer`
finds frontmatter and inline tags; tests for `managed_root`, the query parser and the
scanner (hidden files, `resolve` escapes).

---

## Phase 5 — Note vault B: live-preview editor, checkboxes, links

Shipped 2026-11-27. Image embeds render for `![[file]]` and relative `![](…)`; tables render outside the cursor.

### 5.1 Editor: Obsidian-style live preview, not a rich-text editor

Today the note opens as rendered Markdown (`ui.markdown`) and the pencil switches to raw
CodeMirror source — two modes, and you never see what you write.

**Decision: CodeMirror 6 with live-preview decorations** (what Obsidian does): the text
stays Markdown, the markup (`#`, `**`, `[[…]]`, link URLs, `- [ ]`) is hidden or drawn as
widgets everywhere except on the line the cursor is in.

Rejected: ProseMirror/Tiptap/Milkdown/Toast UI style WYSIWYG. They parse Markdown into
a document model and *serialize it back* — list markers, emphasis style, spacing and
anything they don't know (wikilinks, callouts, embeds, comments) get normalized or lost.
That breaks the vault invariant "a save with no edits produces byte-identical bytes",
rewrites the user's Obsidian files, and would mangle the frontmatter-free bodies the
merge logic (`save_note` three-way merge) works on. Decorations never change the bytes.

- [ ] Custom NiceGUI element `app_ui/vault_editor.py` + a vendored ESM bundle in
      `app_ui/static/vault_editor/` (CodeMirror 6, `@codemirror/lang-markdown` with GFM,
      our decoration plugin). `ui.codemirror` cannot take custom extensions from Python,
      hence a component. The bundle is built by `scripts/build_vault_editor.sh` from a
      pinned `package-lock.json` and committed — no Node at runtime or in the Docker
      build. Licence entry in `THIRD_PARTY_NOTICES.md`.
- [ ] Same contract as today: the element holds the body only; `_compose()`, the dirty
      check (`_compose() != base`), autosave, `Mod-s`, conflict merge and polling are
      unchanged.
- [ ] Three modes: **Live** (default), **Source** (raw, for power users / debugging) and
      **Reading** (no cursor — used for locked notes and shared facts). The current
      preview/edit toggle goes away; Source is in the note's `⋯` menu, remembered per
      device.
- [ ] Rendered in live mode: headings, bold/italic/strike/code, lists, blockquotes,
      tables (rendered when the cursor is outside), horizontal rules, fenced code with
      highlighting, `![[image.png]]` / `![](…)` embeds via `vault_routes.FILE_PATH`.
- [ ] Phone formatting bar above the keyboard: heading, bold, italic, list, checkbox,
      `[[` link, undo/redo. Desktop: the usual shortcuts (Mod-B/I/K, Mod-L checkbox,
      Mod-Shift-7/8 lists).

### 5.2 Checkboxes

- [ ] `- [ ]` / `- [x]` render as real checkboxes in Live and Reading mode; a tap
      changes exactly one character through a normal editor transaction (so it goes
      through the same dirty → autosave → merge path; nothing special-cased server-side).
- [ ] Locked notes: disabled until unlocked (4.1).
- [ ] Done items struck through and muted; Mod-L / the bar button toggles the current
      line between plain, `- [ ]` and `- [x]`.

### 5.3 Links between notes

- [ ] `[[Note]]`, `[[Note|label]]`, `[[Note#Heading]]` and relative Markdown links to
      `.md` files render as links. Resolution follows Obsidian (exact path, else unique
      file name, else shortest path) — one function, `vault/links.py`, also used by the
      findings page's Obsidian links (`findings._wikilinks`) so the two can't disagree.
- [ ] Click opens the note in place; unresolved links are dimmed and offer "Create
      note"; a document link (`[Title](#356)`, as research notes carry) opens the
      document dialog.
- [ ] Typing `[[` autocompletes note names (list sent with the tree, refreshed with it);
      `#` after a space autocompletes known tags.
- [ ] **Backlinks**: a "Linked mentions" section under the note (collapsed, with count),
      computed by `vault/links.py` from the same mtime-cached scan as 4.3.
- [ ] **Rename and move update links automatically** (decided), like Obsidian's
      "Automatically update internal links": every `[[…]]` and relative Markdown link that
      resolved to the old path is rewritten to the new one, then a toast says "Updated
      links in N notes".
      - Every rewrite goes through `vault.notes.save_note` under `get_user_lock` (hash
        check, three-way merge), so a note open in Obsidian or being written by the
        worker is merged, not overwritten.
      - Links inside locked folders are updated too — a finding or dossier pointing at a
        moved research note must not break. That is the one write into a locked file the
        user did not unlock: code-made, link-only, through the same merge path.
      - Only the link target changes; the label (`[[new|label]]`) and everything else in
        the line stay byte-identical. A link that is ambiguous after the move gets the
        full path, as Obsidian does.

**Acceptance:** opening and closing any existing note without typing leaves its bytes
untouched (test over the sample vault in `tests/`); ticking a checkbox changes one byte;
`[[` autocomplete and backlinks work for a renamed note via its new name; 390 px — the
formatting bar does not cover the cursor line.

---

## Phase 6 — Dashboard: charts that answer a question

Shipped 2026-11-27. Figures are computed after the page is on screen (not after a sync, which would need a per-user cache — rejected with the catalogue cache); vectors are cached per user while catalogue and index are unchanged.

Rule for this phase: a chart earns its place only if it answers a question the user
actually has, from data the app *knows* — and tapping it leads somewhere (the browser,
filtered; a document). Pretty-but-mute is out.

All charts with NiceGUI's built-in `ui.echart` (no new dependency), neutral single-hue
marks from the theme tokens, `--c-warn` only for "you must react", the accent only for a
selected element — the colour invariant applies to charts too. Data comes from the
catalogue (§3.1) and the sidecars, computed after each sync and cached per user (same
rebuild path as the catalogue), never on page load.

### 6.1 Layout: my day first, the archive second, the system last

- [ ] Top, "Today": deadlines (the existing calendar + table), "Expected but missing"
      (6.3), open findings count.
- [ ] Middle, "Archive": 6.2, 6.4, 6.5.
- [ ] Bottom, "System" — sync and its log, compute host and power, vault sync, dream —
      one section that remembers being collapsed. Nothing is removed; operations just
      stop pushing the user's own day below the fold.

### 6.2 Documents over time

- [ ] Bars per month (last 24 months, or all years as a year view), stacked by the five
      largest document types + "other". Switch **created / added**: created shows the
      user's life (when things happened), added shows scanning activity (a backlog
      scanned in one weekend is a spike there, nothing in "created").
- [ ] Tap a bar → browser filtered to that month (and type, if a segment was tapped).
- [ ] Question it answers: "When did I get what, and is something thin?" — a month with
      no bank statement stands out.

### 6.3 Expected but missing — the most useful one

Many correspondents send on a rhythm: monthly salary slips, quarterly statements, the
yearly utility bill, the yearly tax notice, insurance renewals.

- [ ] Per correspondent (+ document type) with at least three documents: the intervals
      between their `created` dates; when they are regular (median ≈ 1, 3, 6 or 12
      months, small spread) the next date is predicted. Past it by a tolerance → listed:
      "Stadtwerke · Invoice — yearly, last 2025-02-14, expected around 2026-02".
- [ ] Each row: a small dot timeline of the past documents (the evidence for the rhythm),
      the prediction, and "Not coming any more" (hides this series for good — a contract
      ends) / "Open last one".
- [ ] Pure code, no model; the reason is always shown, so the user can judge it. No push
      and no finding from this — dashboard only. (Could later feed the proactive system
      as a finding type; not part of this plan.)

### 6.4 Topics in the archive — semantic clusters, done usefully

The semantic structure is real and worth showing, but **not as a 2D scatter "map"**: it
looks impressive and rarely answers a question, its axes mean nothing, and it needs one
colour per cluster to be readable — colour as identity, which the UI invariant forbids.
What the clusters are good for is three concrete things:

- [ ] **Topics**: document vectors (mean of each document's chunk embeddings from the
      active index, via `ChromaClient`), clustered with plain k-means in numpy
      (already a dependency; k picked by silhouette over a small range). Per user, over
      the documents *they* can see — a cluster label built from someone else's
      correspondents would leak them. Each topic is labelled **by code** from its most
      common correspondents, types and tags ("Allianz, HUK · Insurance policy") — no
      model, so no invented names. Shown as a neutral treemap (size = documents); tap →
      browser with those documents.
- [ ] **Possible duplicates**: pairs of documents whose vectors are nearly identical and
      whose page counts match — double scans and the same letter uploaded twice. A list
      with both titles and "Open both". Cheap and immediately useful.
- [ ] **Tagging outliers**: documents whose type/correspondent/tags disagree with the
      clear majority of their nearest neighbours ("typed *Contract*, but its ten nearest
      neighbours are all *Invoice*"). A suggestion list with a link to the document in
      Paperless — the app changes nothing itself.
- [ ] Recomputed after each sync and when the active embedding model changes; stored with
      the model id, so a model switch never shows clusters from the old index.

### 6.5 Archive health (for whoever keeps Paperless tidy)

- [ ] Counts with a link each: documents without correspondent / without type / without
      tags; not indexed yet; outdated extraction (`sidecar_service.outdated_ids()`);
      excluded from ingest. One row of figures, not a chart — a number is the honest form
      of a single count.

### Rejected

- **Spending per month / per correspondent.** The extraction stores no structured
  amounts (`config/extraction_rules/base.py`: page text, summaries, actions, tables) —
  a spending chart would rest on numbers guessed from free text, a confident false
  statement in chart form. Only with a dedicated, validated amount field in a future
  `PROMPT_VERSION`.
- **Usage statistics** (chats per week, tokens): interesting to the developer, not a
  question the user has about their documents.

**Acceptance:** every chart and list taps through to a filtered browser or a document;
nothing on the dashboard is computed on page load (timing logged); topics, duplicates
and outliers only ever contain documents the user can see (test with two users);
greyscale screenshot still readable.

---

## Phase 7 — Smaller things found during the review

Shipped 2026-11-27 except the shared lock vocabulary outside the vault.

- [ ] **Sentence case**: ten labels still use `uppercase tracking-wide(r)` —
      `app_ui/pages/browser.py:46` and `app_ui/pages/chat.py` (2235, 2251, 2271, 4296,
      4339, 4371, 4524, 4545, 4566). Violates "sentence case everywhere".
- [ ] **Global quick switcher, Ctrl/Cmd+K, on every page**: documents (title, `#id`),
      notes, findings and dossiers, experts, conversations, settings sections, modules.
      The findings page's local Ctrl+K moves to `/`. Built on the module registry
      (1.1); results respect hidden modules.
- [ ] **Readable route aliases**: `/vault` → `/brain`, `/research` → `/werkbank`
      (old paths stay forever — they are in notes, links and push messages). The UI
      names and the URL then match.
- [ ] **Browser multi-select**: select several documents → Pin all / Download / "Ask
      the chat about these" (pins feed every chat already, so this is mostly a bulk
      pin plus navigation).
- [ ] **One lock vocabulary**: the lock icon and "read-only · unlock" wording from 4.1
      reused wherever something is protected (Universal expert, shared facts, archived
      dossiers), so the user learns it once.

---

## Order and size

| Phase | What | Size | Risk |
|---|---|---|---|
| 1 | Module registry, header label, start page + shortcuts, hide modules | M | low |
| 2 | Findings zone tabs at the top | S | low |
| 3 | Catalogue (fixes the slow Library), browser facets, live, views, search fix | L | medium (perf, search) |
| 4 | Vault A: locks, read-only properties, content search, tree | M | low |
| 5 | Vault B: live-preview editor, checkboxes, links | L | medium (new JS bundle) |
| 6 | Dashboard: layout, over time, expected-but-missing, topics, health | M–L | medium (clustering) |
| 7 | Polish | S–M | low |

The catalogue (3.1) is worth shipping on its own first: it fixes the Library's 3–4 s
today and is what Phases 3 and 6 stand on. Vault A comes before B on purpose: locking and
search are the safety and speed wins, and they don't depend on the new editor; B then
replaces the editor under an unchanged save/merge model.

## Invariants to add to `CLAUDE.md` as phases ship

- The module list lives in `app_ui/modules.py`; nav, menu, help, start page and the
  hide switches read `visible_modules()`. A hidden module's URL still works.
- One PWA. Extra home-screen entry points are manifest `shortcuts`, never a second
  manifest/app on the same origin.
- Document lists fetch metadata only (`list_catalogue`), with the user's own token,
  and are not cached; experts narrow them with `scope.allows`.
- Managed vault folders come from `vault/managed.py` only; their root folders (Memos
  included) are never renamed, moved or deleted from the UI; notes in them, except
  memos, need an explicit per-note unlock to edit.
- Renames and moves rewrite links automatically, through `save_note`, link targets only.
- The vault editor never re-serializes Markdown — decorations only; an untouched save
  is byte-identical.
- The browser filters before it searches semantically; nothing outside the candidate
  set may enter a result (tag-name hits included).
- Dashboard figures are computed after a sync, per user over what they can see, never on
  page load; topic labels come from code, never a model.

## Decisions (2026-11-27)

- Memos are **not** locked; only the Memos folder itself is protected (no rename, move,
  delete) like every managed root.
- Start page offers "Last used module" (not the default — the dashboard stays the
  default).
- The vault editor is Obsidian-style live preview.
- Links are updated automatically on rename and move.
- The Library/browser list is **not** cached: the lean fetch makes it fast enough.
- Shortcuts: Chat, Note vault, New voice memo; the dashboard is the app icon itself.
  One PWA, never several.
