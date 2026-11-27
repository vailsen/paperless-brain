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

**Bug found on the way (fix in Phase 3):** `pipelines/search.py` appends tag-name hits
(`_tag_search`, "query word equals a tag name") to semantic results **without
intersecting them with the filtered candidates**. A correspondent filter plus the query
"Rechnung" also shows documents tagged "Rechnung" from every other correspondent. The
chat's `search` tool uses the same function, so the model sees the leak too.

---

## Phase 1 — Navigation: landing, knowing where you are, hiding modules

### 1.1 One module registry

Today the module list is `NAV_ITEMS` + `FINDINGS_ITEM` + two special cases in
`nav_items()` (proactive switch, "Use experts"). Everything below needs the same list,
so first make it one:

- [ ] `app_ui/modules.py`: `Module(key, icon, label, path, optional, available)` for
      `dashboard`, `chat`, `findings`, `vault`, `research`, `experts`, `browser`.
      `available()` holds the existing conditions (`PROACTIVE_ENABLED`); `optional`
      is False for dashboard and chat.
- [ ] `visible_modules()` = available and not hidden by the user (1.4). The header,
      the ☰ menu, the help dialog, the start-page picker and the settings switch list
      all read it. `NAV_ITEMS` stays as the `N_()` label source only.
- [ ] `module_for_path(path)` — used by the header label (1.3), the page title and the
      hidden-module banner.

### 1.2 Start page and app shortcuts

- [ ] Per-user pref `start_page` (`services/user_prefs`): any visible module, plus
      **"Last used module"**. Default `dashboard` (no change for anyone who does nothing).
      Setting in Settings > General, next to "When the chat opens" — the two belong
      together ("what do I see when I open the app").
- [ ] New route `/start`: reads the pref and redirects. `/` stays the dashboard, so the
      logo and the "Dashboard" item keep meaning the dashboard.
      `take_login_target()` falls back to `/start` instead of `/`; a push link's target
      still wins.
- [ ] "Last used module": each page writes its module key on load (cheap pref write);
      `/start` sends there. A finding/dossier deep link counts as findings, nothing finer
      — reopening a stale half-state is worse than the module's home.
- [ ] `manifest.json`: `start_url: "/start"` and **`shortcuts`** — Chat, Note vault,
      Dashboard, plus "New memo" (`/start?memo=1` → opens the memo dialog on the
      landing page) when that is cheap. Long-press on the Android icon and the Windows
      taskbar jump list show them; iOS has no manifest shortcuts, which is what the
      start-page pref covers. Shortcut icons: 96 px monochrome, rendered by
      `scripts/make_icons.py`, carrying `ICON_VERSION` in the name.
- [ ] Note in the help text: an already installed PWA may keep its old `start_url`
      (`/`) until Chrome refreshes the WebAPK, so the start-page pref only takes effect
      there after a refresh or reinstall. A fresh login is not affected — it always goes
      through `/start`.

### 1.3 The phone (and narrow desktop) shows which module you are in

Today, in `pb-nav-compact`, the header is wordmark + ☰ + memo + help + user. Inside
Findings nothing says "Findings".

- [ ] Replace the bare ☰ with a **module switcher button**: current module icon +
      label + `expand_more`, e.g. `[💡 Findings ▾]`. Same menu as today (active item
      highlighted, Paperless-ngx link at the bottom). This is the one place a phone user
      looks to answer "where am I" and "go elsewhere" — one control for both.
- [ ] In compact mode the wordmark shrinks to the mark alone (`brand.py` already has
      the SVG) to make room. Keep the link to `/`.
- [ ] Help moves into the user menu when compact (it is the least used of the four
      header buttons); memo stays — capture must be one tap from everywhere.
- [ ] `document.title` per module ("Findings · PaperlessBrain"), so the OS app
      switcher, browser tabs and history say where each one is.
- [ ] `NAV_FIT_JS` keeps deciding *when* to collapse — it measures, so the wider
      switcher button is accounted for automatically.

Rejected: a bottom tab bar. The chat input, the findings dock and the phone keyboard
already compete for the bottom edge; another 56 px there costs the chat a line of text
on every screen. The switcher button gives the same "where am I" for zero height.

### 1.4 Hide modules (Settings > General > Modules)

- [ ] Per-user pref `hidden_modules: list[str]` of `browser`, `vault`, `experts`,
      `findings`, `research`. Chat and dashboard are listed as switches that are on and
      disabled, so the user sees why they cannot go.
- [ ] **Hidden means out of the way, not off:**
      - gone from header, ☰ menu, help dialog, start-page picker, dashboard links and
        cross-module buttons (e.g. the chat's "Deep research" hand-off);
      - the **URL still works** — push links, citations in research notes and old
        bookmarks keep opening — with a thin banner "This module is hidden in your
        settings · Show it again";
      - background work is unaffected: hiding Findings does not stop the proactive
        worker or push (the banner links to the notification settings); hiding the vault
        keeps memory and memos working (the memo dialog still says where it saved).
- [ ] **Experts: one switch, not two.** Hiding Experts *is* today's "Use experts" off
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

Today: an AI search field, a Search button, "Advanced filters" collapsed behind a link
(full text, id, tags, correspondent, type, owner, two date widgets), a sort select,
cards. Every change needs a button press, and filters are a form, not a view of the
archive.

### 3.1 Load the catalogue once, filter in memory

The archive is hundreds to a few thousand documents. Their metadata (id, title,
correspondent, type, tags, storage path, owner, created, added, page count) is a few
hundred KB.

- [ ] `services/doc_catalogue.py`: the documents the *user* can see, fetched with their
      session token (`get_session_paperless`, never the superuser), per-user cache with
      a short TTL and invalidation at the end of a sync. Same cap as `doc_library.MAX_DOCS`
      (5 000); above it, fall back to today's server-side filtering.
- [ ] All facet filtering, counting and sorting runs on that list in Python — no round
      trip per click, which is what makes "live" possible.

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
      - **Grouped** — the `doc_library` tree (by correspondent, tag or type), reusing
        `build_nodes`, over the *filtered* set.
- [ ] Sort: created, added, title, correspondent, type; relevance only while an AI
      query is active (and then the default).

### 3.6 URL state

- [ ] Filters, query and view live in the URL (`/browser?tag=12&corr=3&view=list`,
      `replaceState` like the findings page): back button works, a filtered view can be
      bookmarked or used as a start page, and the dashboard can link to "overdue" or a
      tag directly.

**Acceptance:** with ~500 documents a facet click re-renders in under 150 ms;
filter + AI query never returns a document outside the filter (test); 390 px — no
horizontal scroll in any view, facet sheet closes with the back gesture.

---

## Phase 4 — Note vault A: safe by default, searchable

Everything here works with today's CodeMirror editor; Phase 5 swaps the editor
underneath without touching it.

### 4.1 Managed folders are read-only until unlocked

The folders PaperlessBrain writes — `PaperlessBrain Memory`, `… Memos`, `… Research`,
`… Experts`, `… Skills`, `… Findings` (names from `config/settings.py` and
`proactive/paths.py`, never hard-coded) — hold state other parts of the app parse.
A stray keystroke in a finding's frontmatter changes its status; one in an expert
file changes the expert.

- [ ] `vault/managed.py`: `managed_root(rel) -> str | None` — the single place that
      knows which folders are managed.
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
      - Research / Memos → plain confirm.
- [ ] **Unlock scope: this note, until another note is opened** (not stored, not per
      folder). A locked badge returns on reopen.
- [ ] Tree operations inside managed folders (new, rename, move, delete, drag in/out)
      go through the same confirm. **The managed root folders themselves can never be
      renamed, moved or deleted** — even unlocked: a renamed folder silently orphans
      everything (see the warnings in `config/settings.py`).
- [ ] This is a UI guard against accidents, not security: agent writes
      (`brain_writer`, `memo_writer`, findings) are unaffected.

### 4.2 Properties: read by default, edited on purpose

- [ ] The properties panel shows values read-only: chips for lists/tags, a
      check/dash for booleans, plain text otherwise; dates as written in the file
      (`_raw_scalar`, unchanged).
- [ ] **"Edit properties"** button on the panel → today's widgets (switches, tag
      select, text fields, Add property, Edit YAML) + "Done". Closing the note or
      opening another leaves edit mode.
- [ ] In managed folders the button appears only after "Unlock to edit".
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
tapping a checkbox or a property without passing the confirm; the managed root cannot be
renamed; search for a word that appears only inside a note body finds it, `tag:steuer`
finds frontmatter and inline tags; tests for `managed_root`, the query parser and the
scanner (hidden files, `resolve` escapes).

---

## Phase 5 — Note vault B: live-preview editor, checkboxes, links

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
- [ ] Rename: offer "Update N links in other notes" (explicit, with the count; never
      touches managed folders — it says how many it left there).

**Acceptance:** opening and closing any existing note without typing leaves its bytes
untouched (test over the sample vault in `tests/`); ticking a checkbox changes one byte;
`[[` autocomplete and backlinks work for a renamed note via its new name; 390 px — the
formatting bar does not cover the cursor line.

---

## Phase 6 — Smaller things found during the review

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
- [ ] **Dashboard: "my day" first, system second.** It mixes the user's day (deadlines,
      calendar, pinned documents) with operations (sync log, compute host, power,
      vault sync, dream). Proposal: the day on top; operations in a "System" section
      that remembers being collapsed. No feature removed. Worth doing once Phase 1 makes
      the dashboard one choice of start page instead of the only one.
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
| 3 | Browser: catalogue, facets, live, views, search fix | L | medium (perf, search) |
| 4 | Vault A: locks, read-only properties, content search, tree | M | low |
| 5 | Vault B: live-preview editor, checkboxes, links | L | medium (new JS bundle) |
| 6 | Polish | S–M | low |

Vault A comes before B on purpose: locking and search are the safety and speed wins,
and they don't depend on the new editor; B then replaces the editor under an unchanged
save/merge model.

## Invariants to add to `CLAUDE.md` as phases ship

- The module list lives in `app_ui/modules.py`; nav, menu, help, start page and the
  hide switches read `visible_modules()`. A hidden module's URL still works.
- Managed vault folders come from `vault/managed.py` only; their root folders are never
  renamed, moved or deleted from the UI; edits need an explicit per-note unlock.
- The vault editor never re-serializes Markdown — decorations only; an untouched save
  is byte-identical.
- The browser filters before it searches semantically; nothing outside the candidate
  set may enter a result (tag-name hits included).

## Open questions for the owner

1. **Memos locked?** They are your own dictated notes, unlike the other managed folders.
   The plan locks them as requested; the alternative is to lock only their properties.
2. **Start page "Last used module"** — wanted, or only fixed choices?
3. **Live preview vs. full WYSIWYG** — the plan recommends Obsidian-style live preview
   (markup reappears on the line you edit) for the byte-safety reasons in 5.1.
4. **Link update on rename** — offer it (plan) or always do it silently like Obsidian's
   setting "Automatically update internal links"?
