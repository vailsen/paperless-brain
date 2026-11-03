# Findings workspace

Status: **built**, 2026-11-02. Code: `app_ui/pages/findings.py` (the
workspace), `proactive/dossiers.py` (tree, anchors, insights),
`services/dossier_merge.py` + `app_ui/dossier_save.py` (save to dossier);
tests `tests/test_findings_workspace.py`.

**Changed from the plan while building:** the chat dock is the chat page
itself, embedded (`/chat?embed=1&dossier=<id>` in an iframe), instead of
splitting `app_ui/pages/chat.py` into components (Phase 3 below is dropped).
Same tools, citations, experts, Library tab and "Save to dossier" — with no
second chat implementation and no rewrite of a 4 000-line page. In embed mode
the chat has no app header and keeps its conversation out of the browser's
`/chat` state; a saved insight tells the page around it via `postMessage`.

**Changed 2026-11-03 (after the first use):**

- **Phone:** the zone switch is a slim segmented control; "Chat" no longer
  replaces the item — the viewer stays on top and the chat sits below it
  (drag the grip), so the data stays in sight while typing. The dock is a
  compact chat (input, expert, model, delete); its maximize button enlarges
  it in place — same frame, so a half-typed message and a running answer
  survive — with the full controls, and the bottom switch stays for the way
  back. (A first version navigated to `/chat` and lost both; `/chat?conv=<id>`
  still opens a conversation by id.)
- The "Ask" buttons are gone (the dock and the switch are the way to chat);
  a finding opened from a dossier goes "Back to the dossier", on the tab it
  came from. "Relook" is called **Rescan**.
- **Search** shows its results in the navigator, under the field — typing
  never switches the view. It also matches the titles of the documents that
  findings and dossiers name (read as the user), and lists those documents.
- **Counters** on every binder tab and on every inbox bucket.
- **Rescan** (formerly "Relook"; dossier menu, and a notice when documents are pending): a
  user-requested worker job (`dossier_relook`, in `USER_REQUESTED_KINDS`) that
  rebuilds the dossier from all its documents — its own, the ones the user
  added (`added_docs`, code-owned, never dropped), and those of its findings.
  Insights, research and resolved questions stay.
- **Add documents** to a dossier by hand (search or `#id`); **file a finding**
  into a dossier (it takes the dossier's topic — the answer to open question 2
  changed: yes, by moving the topic). "Not in a dossier" cards have the action.
- **Triage** updates an existing dossier for every new document of its topic;
  the model's `dossier_update` flag is only needed to *start* one.
- **Save to dossier:** "This answer" or "The whole conversation" (condensed,
  sources merged from every answer's tool log); the merge round retries once
  and falls back to plain text when a local model answers past the tool, and
  a failure says why.
- The dossier the chat sees (`dossiers.context_text`) puts state, open
  questions and saved insights before the timeline.

Companion: `docs/experts.md` (the expert picker in the
chat dock comes from there). Invariants of the proactive system (CLAUDE.md
"Proactive system") apply unchanged; this plan adds a few of its own (§7).

## 1. Goal

The findings module works, but it is a list. A power user who has 40 open
findings and a dozen dossiers wants a **workspace**: everything open in one
view, every matter (dossier) one click away, and the conversation about a matter
next to the matter — not in another module.

Three things change:

1. **Layout** — navigator on the left (global search + dossier tree), viewer in
   the middle, chat dock below. Phone: the same three zones as tabs.
2. **Dossiers become binders** — one per matter, holding current state,
   timeline, findings, research, documents and **saved insights**.
3. **Chat → dossier** — a "Save to dossier" button on chat bubbles; a separate
   model round fits the excerpt into the dossier; code records where and when it
   came from. Everything stays Markdown in the user's vault.

## 2. What exists (and constrains the design)

| Piece | Where | Relevant fact |
|---|---|---|
| Findings | `proactive/findings.py`, files in `PaperlessBrain Findings/Notices/` | Hash-checked writes via `findings.update` → `vault.notes.save_note`. Body is a timeline (`split_timeline`). |
| Dossiers | `proactive/dossiers.py`, `PaperlessBrain Findings/Dossiers/<topic>.md` | Body is **rebuilt by the model** (`from_result` → `render_body`). Only `research` survives a rebuild, because the model never sees it. Sections are parsed **by position** (headings are translated). Flat folder: `_path(user, topic)`. |
| Findings page | `app_ui/pages/findings.py` (700 lines), `app_ui/findings_view.py`, `app_ui/findings_chart.py` | Inbox semantics (open / relevant / done / dismissed) live in `states.DISPLAY` — keep. |
| Chat page | `app_ui/pages/chat.py` (4 258 lines) | One page function (`chat()` from line 714) owns streaming, bubbles, citations, images, dialogs. **Not reusable as a component today.** |
| Chat history | `services/chat_history_service.py` (SQLite) | No notion of context ("this conversation is about dossier X"). |
| Indexing | `proactive/retrieval.index_meta` | Dossiers are indexed with `trust: low`, `FINDINGS_RULE` says: never evidence. |

Two consequences drive the plan:

- **Saved insights must survive a dossier rebuild.** They become a code-owned
  section, like `research` today (§4.3).
- **The chat dock needs the chat extracted** from `chat.py`. That is the largest
  and riskiest piece of work (§6, phase 3) — it is done once and both `/chat`
  and the dock use the result.

## 3. UX

### 3.1 Desktop (≥ 1024 px)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ 🔍 Search findings & dossiers…   (Ctrl K)     ▣ 12 open  ⚠ 3 due ≤7d  ● 5 rel │
├───────────────────────┬──────────────────────────────────────────────────────┤
│ INBOX            12   │  Kfz-Versicherung                         ⋯  ✎  ⇩PDF │
│  ⚠ Pressing       3   │  binder · 14 documents · updated 2 d ago · revisit 3 Dec│
│  Relevant         5   │  ┌Overview┬Timeline┬Findings 3┬Insights 4┬Docs┬Research┐│
│  Done · Dismissed ›   │  │ Current state                                     ││
│                       │  │ Vertrag läuft bis 31.12., Beitrag +8 % [#241] …   ││
│ DOSSIERS          ＋  │  │                                                   ││
│ ▾ 📁 Auto             │  │ Open questions                                    ││
│   ▾ Kfz-Versicherung  │  │ ☐ Kündigungsfrist 1 Monat oder 3? [#241 p2]       ││
│       Findings (3)    │  │ ☑ Werkstattbindung → answered 2 Nov (Insight)     ││
│       Insights (4)    │  │                                                   ││
│     ADAC              │  │ Latest insight · 2 Nov 14:03 · chat · web+docs    ││
│ ▸ 📁 Wohnung          │  └───────────────────────────────────────────────────┘│
│ ▸ 📁 Steuern 2026     │━━━━━━━━━━━━━━━━━━━━━ drag ━━━━━━━━━━━━━━━━━━━━━━━━━━━│
│   Unfiled         2   │  CHAT  about: Kfz-Versicherung ✕   Expert: Versicherung ▾  Model ▾ │
│                       │  🧑 Lohnt sich ein Wechsel zum ADAC-Tarif?           │
│                       │  🤖 Laut Police [^241-p2] … ADAC bietet [web] …      │
│                       │       [⧉ Copy] [📁 Save to dossier] [↻]              │
│                       │  [ Ask about this dossier…                    🎤 ➤ ] │
└───────────────────────┴──────────────────────────────────────────────────────┘
```

- **Navigator (left, 280 px, collapsible to icons).** Inbox views on top
  (`states.DISPLAY` buckets + "Pressing" = `findings_view.is_pressing`), then
  the dossier tree, then "Unfiled" (open findings whose topic has no dossier).
  Counts are badges in `--c-text-muted`; only "Pressing" uses `--c-warn`
  (a state the user must react to — colour rule).
- **Viewer (centre).** Nothing selected → a home view: KPI strip, the existing
  treemap (`findings_chart`), the next 30 days of deadlines as a horizontal
  timeline. A finding selected → today's finding detail (reactions, notes,
  research), unchanged in substance. A dossier selected → the binder with tabs.
- **Chat dock (bottom, `ui.splitter` horizontal, default 35 %, collapsible to a
  one-line input).** It always shows its context chip ("about: Kfz-Versicherung")
  and the expert + model pickers. Selecting another item in the navigator does
  **not** silently re-target a running conversation: the chip offers
  "Switch context" (starts a new conversation) or keep.
- **Topic colours** stay the user's choice (`app_ui/topic_colors.py`) and appear
  as the 3 px outline on tree rows and cards — nowhere else.

### 3.2 Phone (< 1024 px)

```
┌──────────────────────────┐   Three zones as a bottom segmented bar:
│ ☰  Kfz-Versicherung   ⋯  │   [ Navigator | Viewer | Chat ]
│ Overview Timeline Find… ›│   - Tapping a tree item switches to Viewer.
│ Current state …          │   - "Ask" in the viewer header switches to Chat
│                          │     with the context chip set.
│                          │   - Tabs in the binder scroll horizontally in
│                          │     their own container (no page side-scroll).
├──────────────────────────┤   - Chat input sits above the bar, safe-area aware.
│ ▤ Navigator ▣ Viewer 💬 Chat│
└──────────────────────────┘
```

Tabs instead of a drawer + bottom sheet: a sheet over a binder over a drawer is
three stacked scroll contexts on a 390 px screen. Tabs are one at a time, thumb
reachable, and match how the user actually works on the phone (read, then ask).
Verify with Playwright at 390 px (CLAUDE.md: measure, don't read CSS).

### 3.3 Global search

- Ctrl K / tap on the search field. Searches **findings and dossiers only**
  (titles, topic labels, body, notes, saved insights).
- **Lexical in code first**: the ledger is small (hundreds of files), every
  file is already parsed by `list_findings` / `list_dossiers`. Token match with
  accent folding, ranked: title > topic > insight heading > body. Includes held
  and dismissed findings (with a filter chip), which the semantic index
  deliberately never contains (`retrieval.index_meta`).
- Results grouped: Dossiers · Findings · Insights; Enter opens in the viewer.
- Semantic search over the same set is a later option (the vault collection
  already holds the surfaced ones) — not in scope now.

### 3.4 The binder (dossier viewer)

| Tab | Content | Writer |
|---|---|---|
| Overview | Current state, open questions (with resolved ones ticked and linked to the insight that answered them), latest insight, revisit date | model (state, questions), code (resolution links) |
| Timeline | Dated entries, each with `[#id]` → document dialog | model, validated by code (`dossiers.validated`) |
| Findings | Cards of all findings with this topic, inbox reactions inline | proactive system |
| Insights | Saved chat excerpts, newest first, with provenance footer | **user action + merge round**, code-owned section |
| Documents | Thumbnails of `doc_ids` (Paperless as the user), opens the document dialog | code |
| Research | Werkbank research notes (`research` list) → PDF | code |

Header actions: rename, move to folder, "Ask" (focus chat), export PDF, open in
the note explorer (`/brain`), rebuild (with confirm — shows that insights stay).

## 4. Data model (vault files)

### 4.1 Folder tree

```
PaperlessBrain Findings/
├── _interests.md
├── Notices/
└── Dossiers/
    ├── Auto/                     ← folders = the tree; created by the user
    │   └── Kfz-Versicherung.md
    ├── Wohnung/
    └── Steuern 2026.md           ← root level stays valid (all current files)
```

- **The folder structure on disk is the tree.** No separate tree file, no DB.
  Moving a dossier = moving the file inside `Dossiers/` (under `get_user_lock`);
  `pbrain_id` survives, Obsidian moves work too.
- `dossiers._path(user, topic)` becomes a lookup: scan `Dossiers/**/*.md`, map
  `topic` (frontmatter) → path. New dossiers from the proactive system land at
  the root ("Unsorted" in the tree). `list_dossiers` → `rglob`.
- Depth capped at 3 levels in the UI (a binder shelf, not a file system).

### 4.2 Dossier frontmatter additions

```yaml
pbrain_id: …            # unchanged
topic: kfz-versicherung # unchanged — the key findings match on
origin: proactive       # proactive | user  (user = created by hand in the UI)
expert: <pbrain_id>     # optional default expert for the chat dock (experts.md)
insights_rev: 4         # count, for the "new insight" badge
```

A **user-created dossier** (`origin: user`) has a topic key from
`interests.slugify(label)`, empty `doc_ids`, and is never rebuilt by the
proactive system unless findings of that topic appear — then it is *updated*
incrementally like any other, and its insights stay.

### 4.3 Saved insights — a code-owned section

Today `parse()` reads sections by **position**, which breaks the moment a
section is added. Change: every section gets a stable, invisible anchor.

```markdown
<!-- pb:state -->
## Aktueller Stand
…
<!-- pb:timeline -->
## Zeitleiste
…
<!-- pb:questions -->
## Offene Fragen
- Kündigungsfrist 1 oder 3 Monate? [#241]
- ~~Werkstattbindung?~~ → [[#Insight 2026-11-02 1403]]
<!-- pb:insights -->
## Erkenntnisse

### 2026-11-02 14:03 — ADAC-Tarif ohne Werkstattbindung günstiger
Der aktuelle Vertrag bindet an Partnerwerkstätten (Police S. 2). Der ADAC-Tarif
„Komfort" kostet laut Website 412 €/Jahr ohne Bindung …

> pb-source: chat · conversation `c-7f3a` · message 6 · 2026-11-02 14:03
> model: claude-sonnet-5-5 · expert: Versicherung
> documents: [#241 p2](…) · web: https://www.adac.de/… (fetched 14:02)
> merged: 2026-11-02 14:04 by qwen3.8 — verified: 2 numbers ✓, 0 unsupported

<!-- pb:research -->
## Recherche
- [[PaperlessBrain Research/…]]
```

- HTML comments are invisible in Obsidian's reading view and in the app's
  Markdown render; parsing keys on them, headings stay translated. A file
  without anchors (all current ones) is parsed the old positional way once and
  rewritten with anchors on the next write.
- **Insights are code-owned like `research`**: `from_result` copies them from
  `existing`; a rebuild never drops them. The rebuild *model* sees them read-only
  (as context, so the current state can reflect them) but cannot edit them.
- Provenance (`pb-source` block) is **written by code from the turn's tool log**,
  never by a model: conversation id, message index, timestamp, chat model,
  expert, document ids/pages cited in the bubble, web URLs that a `web_search`
  / `web_fetch_page` call of that turn actually returned. A URL the model wrote
  but no tool returned is not listed as a source.
- Each insight gets an evidence class shown as a small neutral label:
  `documents` · `web` · `documents + web` · `reasoning only` (no tool evidence
  in the turn). "Reasoning only" is allowed but labelled — the user decides.

## 5. "Save to dossier" — the merge round

### 5.1 Flow

1. Bubble action "Save to dossier" (next to copy). Long-press on phone.
   Optional: select text in the bubble first → only the selection is saved.
2. Dialog: target dossier (pre-selected = the dock's context; else a searchable
   picker, or "New dossier…"), optional one-line note from the user.
3. **Merge round** (model = the user's chosen "dossier model", default: the chat
   model; picked via `model_picker.model_select`). Input: the excerpt, the
   bubble's citations and web sources (with fetched text snippets from the tool
   log), the dossier's current state and open questions. Output (structured,
   JSON-schema constrained like Werkbank):

   ```json
   {
     "title": "ADAC-Tarif ohne Werkstattbindung günstiger",
     "insight": "… condensed, in the dossier's language …",
     "answers_questions": [1],
     "new_questions": ["Gilt der Rabatt auch für Zweitwagen?"],
     "contradicts_state": "Current state says 'no alternatives checked' — now one was."
   }
   ```

4. **Code checks** before anything is shown:
   - Numbers and long words in `insight` must be supported by the excerpt or
     its sources — reuse `werkbank.v2.checks.unsupported_tokens()`. Unsupported
     tokens are highlighted in the preview, not silently accepted.
   - `answers_questions` indices must exist; `[#id]` refs must be among the
     turn's cited documents.
   - Length caps (title 120, insight 1 500 chars).
5. **Preview** (diff-style): the new insight card, questions to tick, new
   questions, the contradiction note. User can edit text, untick, or "Save
   verbatim" (skip the model, store the excerpt as-is — the escape hatch when
   the merge round is unavailable or wrong).
6. Write: `dossiers.add_insight(user, topic, entry)` under the user lock, via
   `save_note` with hash check (re-read, retry once). Bumps `insights_rev`.
   The vault's background sync indexes it within 2 min; nothing else to do.

### 5.2 Why a separate round instead of "the chat model writes it"

- The chat answer is written for the conversation ("as I said above…"); the
  dossier needs a standalone, dated statement. Rewriting is a different job.
- The merge round sees the dossier's open questions — that is what makes
  "fit neatly" real: it can say which question this answers.
- It can run on a cheaper/local model regardless of which model chatted.
- Its output is checked by code before the user sees it, matching the v2
  principle: honesty comes from schema + checks, not from prompt wording.

### 5.3 Chat in the dock uses the normal tools

The dock is the same chat (same tool groups, same expert/model rules, same
`execute_tool`), so web search, document search etc. are available to produce
insights. The dossier context enters the prompt as one extra block:

> "The user is looking at this dossier. It is this system's own summary — a map,
> not evidence. Use its document ids to decide what to read; cite documents, never
> the dossier." + current state, open questions, doc ids (capped ~1 500 chars).

That is `FINDINGS_RULE` applied to an explicitly loaded dossier. It goes into
`config/chat_prompts.py` as a new code block (not user-editable — see
memory note on the editable boundary).

Conversations started in the dock are stored with `context = "dossier:<pbrain_id>"`
(or `finding:<id>`) — new nullable column in `chat_history_service`. The
binder's Insights tab links back: "from conversation of 2 Nov" opens it in
the dock; `/chat` lists them with a context chip.

## 6. Phases

Work one phase at a time; each ends green (tests + 390 px Playwright check for
UI phases). One build/deploy at the end of a phase batch, not per fix.

### Phase 1 — Dossier storage (no UI change)
- Anchored sections + positional fallback in `dossiers.parse` / `render_body`.
- `insights` field, code-owned through `from_result` (like `research`).
- Folder tree: topic → path lookup, `rglob`, `move(user, topic, folder)`.
- `origin: user` dossiers; `create_manual(user, label, folder)`.
- `add_insight()` with hash check.
- **Accept:** a rebuild of a dossier with insights keeps them byte-identical;
  an old-format file parses and is rewritten with anchors; a moved file is still
  found by topic; tests in `tests/test_dossiers_*.py`.

### Phase 2 — Workspace shell (findings page rewrite)
- New layout: navigator / viewer / dock placeholder; phone tabs.
- Navigator: inbox buckets, dossier tree (create folder, rename, move via
  menu — drag & drop is a later nicety), Unfiled.
- Viewer: home (KPIs, treemap, 30-day deadline strip), finding detail (current
  content moved into a component), binder with tabs.
- Global search (lexical, in code).
- URL state: `/findings?d=<pbrain_id>&tab=insights` / `/findings/<fid>` — deep
  links from push/email keep working (`/findings/{id}?action=…` unchanged).
- **Accept:** every existing finding action still works; push links open the
  right finding; no sideways scroll at 390 px; i18n complete (de catalog).

### Phase 3 — Chat extraction (dropped — the dock embeds the chat page)
- Split `app_ui/pages/chat.py` into `app_ui/chat/`:
  `session.py` (turn runner: backend, tool loop events, persistence, WoL
  pre-check), `bubbles.py` (render, citations, images, actions slot),
  `composer.py` (input, mic, send/stop), `dialogs.py` (docx/email/pdf/kanban).
- `/chat` becomes a thin page over these; behaviour identical. Do it in small
  moves, each covered by existing tests + a Playwright smoke of `/chat`.
- `ChatPanel(context=…, compact=True)` for the dock.
- **Accept:** `/chat` unchanged for the user (same features, same layout);
  the dock streams, cites, shows images, stops; conversation context column
  migrated.

### Phase 4 — Save to dossier
- Bubble action slot → "Save to dossier" (dock and `/chat`).
- Provenance collector from the turn's tool log.
- Merge round (schema-constrained), checks, preview dialog, verbatim fallback.
- Insights tab, question resolution links, "new insight" badge.
- Dossier context block in the prompt.
- **Accept:** a saved insight lists exactly the documents/URLs the turn's tools
  returned; an invented number in the merge output is flagged in the preview;
  the insight survives a proactive rebuild; works with Ollama and Claude.

### Phase 5 — Polish
- Keyboard: Ctrl K, ↑/↓ in navigator, `e` done, `r` relevant, `x` dismiss
  (only when focus is in the navigator, never in an input).
- Drag & drop in the tree; dossier PDF export (reuse research PDF route).
- Expert picker in the dock (depends on `docs/experts.md` phase 3).

## 7. Invariants (to add to CLAUDE.md when built)

- **The folder tree under `Dossiers/` is the dossier tree.** No tree file, no DB.
- **Insights are code-owned.** A dossier rebuild never edits or drops them;
  the rebuild model sees them read-only.
- **Provenance comes from the tool log, never from a model.** A URL no tool
  returned in that turn is not a source.
- **A saved insight is shown before it is written.** No silent dossier write
  from chat; the merge output passes `unsupported_tokens` first.
- **A dossier in the chat context is a map, not evidence** (same rule as
  `FINDINGS_RULE`); the model cites documents.
- **The module stays behind `PROACTIVE_ENABLED`**, like today (see open question 1).
- The proactive system stays read-only on Paperless; the dock's
  `create_note` tool follows the user's normal tool toggles.

## 8. Open questions

1. **Dossiers without the proactive system?** Manual dossiers + save-to-dossier
   are useful even with `PROACTIVE_ENABLED=false`. That would mean splitting
   "binders" from "proactive" (own folder, own module flag). Proposal: keep
   behind the flag for now; revisit after Phase 4.
2. Should a finding be movable to another dossier? Today membership is the
   topic key, which also feeds interest weights. Proposal: no — offer
   "Save to dossier" on a finding as an insight instead.
3. Should insights reach the chat's retrieval as hints? They are user-approved,
   but still model-condensed. Proposal: indexed with `trust: low` like the
   dossier itself, never evidence.
