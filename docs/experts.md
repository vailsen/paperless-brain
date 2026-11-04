# Experts

Status: **built**, 2026-11-02. Companion: `docs/findings-workspace.md`
(the chat dock there gets the expert picker from here).

Code: `services/experts.py`, `services/expert_scope.py`, `services/skills.py`,
`services/briefings.py`, `services/expert_prompt.py`, `services/skill_wizard.py`,
`services/vault_config.py`; UI `app_ui/pages/experts.py`,
`app_ui/skill_wizard_dialog.py`, `app_ui/doc_library.py`; tests
`tests/test_experts.py`.

**Decisions taken after the plan (2026-11-02), they override the text below:**

- Every scope dimension is optional — empty means no restriction there.
- **Every dimension can also exclude** (2026-11-03): `exclude_correspondents`,
  `exclude_tags`, `exclude_document_types` in the file's `scope:`. "Never
  these" wins over "only these"; exclusions alone narrow a full view. The
  Access tab picks both through a dialog with search and "Done"
  (`app_ui/multi_pick.py`) — the back gesture closes it on a phone.
- **The briefing studies every document up to 1 500** (it sampled above 400:
  a 515-document expert showed "300 documents" and looked narrowed). Above
  that it still samples, and the Briefing tab says "sees N, studies M".
- **Every expert has its memory and vault tools.** `memory` and `vault` can
  never be switched off for an expert (`experts.ALWAYS_ON_GROUPS`).
- **Skills and expert files are not embedded.** No `skills` collection:
  sharing reads the other users' skills folders directly and only honours
  files that say `common: true` at the moment of reading (§4.2 below is
  superseded on that point).
- **Experts do not go to the Werkbank**; `use_skill` is in its
  `forbidden_tools`. No per-expert memory. Not used by proactive triage.
- **The chat has a third drawer tab, "Library"**: every document the active
  expert can see, as a compact tree grouped by correspondent / tag / type
  (`app_ui/doc_library.py`). The findings dock is the chat itself, so it has
  it too, and the expert's Access tab shows the same list live while editing.
- The briefing, once built, is shown in the expert's Briefing tab: archive
  map (code), orientation (model, checked), and a checks panel listing what
  code dropped and why.

## 1. Goal

A new module `/experts`. An **expert** is a chat persona with a narrowed view of
the archive: a name, a personality / instruction text, a document scope
(correspondents, tags, document types — opt-in), a set of **skills**, and tools
that can be switched off. Experts are picked in the chat and in the findings
chat dock.

One expert is built in: the **Universal expert** — all documents, all tools,
not deletable; only its personality text is editable. It replaces the
"Chat instructions" field in Settings.

Skills are reusable instruction packs, stored in the vault, assignable to many
experts, shareable with other users the way `common: true` memory facts are.
A wizard helps write them with a model the user picks.

The "killer feature": an expert can **study** its documents beforehand and keep
a short briefing, so it starts its first conversation oriented (§6).

## 2. What exists (and constrains the design)

| Piece | Where | Relevant fact |
|---|---|---|
| Custom instructions | `config/chat_prompts.py` (`CUSTOM_INSTRUCTIONS_HEADER`, `MAX_CUSTOM_INSTRUCTIONS_CHARS = 2000`), Settings > AI, stored in credential store `chat_settings.custom_instructions` | **The only user-editable part of the chat prompt**, appended under a subordinating header. `_CORE` and tool blocks stay in code (decided 2026-08-13). Expert text goes into exactly this slot. |
| Access boundary | `services/chat_service._user_paperless()` | Every document tool gets a Paperless client with the user's token; Paperless filters. 13 call sites. This is the natural chokepoint for the expert scope. |
| Tool groups | `app_ui/pages/chat.py:_TOOL_GROUPS` | Tool on/off is per group; prompt blocks follow active groups (`build_system_prompt(active_groups=…)`). |
| Shared memory | `BrainService.get_shared_by_others` | `common: true` in frontmatter → visible to others **from the index**; the file stays in the owner's vault. Skills reuse the `common: true` flag, read from the owner's folder (not embedded). |
| Vault routing | `vault/router.is_brain_path`, `vault/sync._embed_file` | Route by top-level folder. A skills folder needs its own route. |
| Model choice | `services/model_registry`, `app_ui/model_picker.model_select` | Models referenced by display name; every dropdown goes through `model_select`. |
| Sidecars | `services/sidecar_service` | Every ingested document already has `full_summary_summarized` — cheap material for the briefing, no PDF/vision needed. |

## 3. Concepts

### 3.1 Expert

```
name            "Versicherung"            unique per user
icon            material icon, user-picked (neutral colour — see §8)
personality     free text ≤ 2000 chars    → custom-instructions slot
scope           {correspondents: [ids], tags: [ids], document_types: [ids]}
skills          [skill refs]              own or shared
tools_off       ["email", "calendar", …]  tool group slugs
default_model   optional display name
briefing        on/off + state            §6
```

**Scope semantics** (opt-in): each dimension that is empty does not restrict;
filled dimensions are AND-combined, values within a dimension OR-combined.

> correspondents ∈ {Allianz, HUK} **and** document type ∈ {Police, Rechnung}
> (tags empty → any tag)

A document without a correspondent is outside a scope that lists
correspondents. The editor shows a **live count**: "This expert can see
143 of 2 031 documents" (`list_documents` with the filters, as the user).

**The scope is a focus, not a security boundary.** The security boundary stays
the user's Paperless token — the user owns everything an expert sees. But the
scope is still **enforced in code**, not asked for in the prompt: an expert told
"only insurance" that can still search everything will drift, and an answer
citing a document outside its scope is exactly the kind of quiet
untrustworthiness the Werkbank principles exist to prevent.

### 3.2 Universal expert

- Built in, always present, first in every picker, default for new chats.
- Scope: all; tools: all (the chat's own per-conversation toggles still apply);
  not deletable, not renamable; **only the personality text is editable**, plus
  its skills.
- **Migration:** on first read, if no Universal expert file exists and
  `chat_settings.custom_instructions` is non-empty, its text becomes the
  Universal expert's personality. The old value stays in place as a fallback
  copy (same pattern as the `llm_models` migration). Settings > AI loses the
  field and shows a link: "Personality and instructions now live in
  Experts → Universal".

### 3.3 Skill

A skill is a Markdown note: **name**, **description** ("use when …"), and
**instructions** (the body). Like Claude's skills: the description decides
*when*, the body says *how*.

Examples: "Read a German insurance policy", "Check an invoice against the
contract price", "Write a formal complaint letter (DIN 5008)", "Explain a
tax assessment line by line".

Assignable to any number of experts. Public via `common: true`.

## 4. Storage (vault, file-based)

```
<vault>/
├── PaperlessBrain Experts/
│   ├── Universal.md              builtin: true
│   ├── Versicherung.md
│   └── Briefings/
│       └── Versicherung.md       code + model written, §6
└── PaperlessBrain Skills/
    ├── Versicherungspolice lesen.md
    └── Reklamation schreiben.md
```

### 4.1 Expert file

```markdown
---
expert_id: 4c1e…
kind: expert
name: Versicherung
icon: shield
builtin: false
scope:
  correspondents: [{id: 12, name: Allianz}, {id: 31, name: HUK-Coburg}]
  tags: []
  document_types: [{id: 3, name: Police}, {id: 5, name: Rechnung}]
skills: [8a2f…, "shared:alice/77d0…"]
tools_off: [email, calendar, create]
default_model: ""
briefing: true
---
Du bist mein Versicherungsberater. Antworte knapp, nenne immer
Kündigungsfristen und Selbstbeteiligung, vergleiche mit Marktpreisen …
```

- **Ids are the reference, names are display copies.** A renamed correspondent
  in Paperless is shown with its new name on next open (names refreshed from
  the user's maps); an id that no longer exists is shown struck through and
  ignored.
- Body = personality text. Editable in the app or in Obsidian.
- `builtin: true` (Universal): code ignores `scope`, `tools_off` and `name` in
  the file, and recreates the file if it is deleted. An Obsidian edit can't
  narrow or remove it.
- Expert files are **not embedded** (not knowledge, configuration). The
  `PaperlessBrain Experts/` folder is excluded in `vault/sync._embed_file`,
  like the findings folder.

### 4.2 Skill file

```markdown
---
skill_id: 8a2f…
kind: skill
name: Versicherungspolice lesen
description: Use when the user asks about coverage, deductibles or notice periods in an insurance policy.
common: false
version: 3
created_with: qwen3.8   # wizard only, informational
---
1. Find the policy with search_exact (document type "Police") …
2. Report: insurer, policy number, term, notice period, deductible …
3. Quote the clause and cite its page …
```

- **Not embedded** (built that way): `vault/router.is_config_path` makes
  `vault.sync._embed_file` skip the skills and experts folders. Identity is
  `skill_id` / `expert_id` in the frontmatter, written by the code that
  creates the file — no `pbrain_id` is minted for configuration.
- **Sharing = the memory mechanism's flag, without an index:** `common: true`
  → other users see it in the library's "Shared by others" section, read
  directly from the owner's skills folder (paths resolved under `VAULT_ROOT`,
  symlinks skipped); the file stays in its owner's vault. Read-only for them.
- **Assigning a shared skill is by reference** (`shared:<owner>/<skill_id>`),
  resolved at chat time. The owner un-sharing it → the expert shows "skill no
  longer shared" and the skill is skipped — immediately, no sync involved.
- "Duplicate to my skills" is an explicit user action that copies the text into
  the user's own folder (new `pbrain_id`, `forked_from:` in frontmatter). Never
  automatic.

## 5. Runtime

### 5.1 Prompt assembly

`build_system_prompt()` gets an `expert` argument. Order (later outweighs
earlier — the header keeps user text below the ground rules):

```
_CORE
capabilities + tool blocks         (only active groups = chat toggles ∩ expert)
scope block (code)                 "You see only: correspondents Allianz, HUK …
                                    Documents outside this scope do not exist for
                                    you; if the answer needs them, say so and
                                    suggest the Universal expert."
briefing block (code-framed)       §6.4 — a map, not evidence
skills block (code-framed)         §5.3
CUSTOM_INSTRUCTIONS_HEADER + personality
language directive
```

The personality keeps the 2 000-char cap and the subordinating header — the
expert is a tuning of the existing editable slot, not a new hole in `_CORE`.

### 5.2 Scope enforcement — `ScopedPaperlessClient`

- `_user_paperless()` returns a `ScopedPaperlessClient(token, scope)` when the
  conversation's expert has a scope. Subclass of `PaperlessClient`:
  - `list_documents(**filters)` → adds the scope as Paperless filters
    (`correspondent__id__in`, `document_type__id__in`, `tags__id__in`), **and**
    re-checks every returned document in code (Paperless ignores unknown
    filters silently — CLAUDE.md, proactive invariants).
  - `get_document`, `download_document*`, `get_thumbnail` → fetch, check scope,
    raise the same not-found error as a missing document (404 parity, like the
    image routes).
  - `list_documents(ids=…)` (used by `pipelines/search.py` after Chroma) is
    filtered too — Chroma has no scope metadata, so the semantic path is
    filtered post-hoc; `n_results` over-fetch raised for scoped experts so a
    narrow scope still returns enough hits.
- **Audit every tool that reads by document id without Paperless**: sidecar
  readers (`get_document_details`, `get_document_table`,
  `get_document_page_text`, `view_document_page`, `show_document_image`),
  `get_actions` (scans all sidecars), cross-ref lookups, `find_similar_documents`,
  `_web_search_archive_hint`, brain/vault hints in `search`. Each must go
  through the scoped client first. A test pins every document tool against an
  out-of-scope id: answer must equal the "no such document" answer.
- Brain and vault: not document-scoped (the user's memory isn't sorted by
  correspondent). An expert that should not use them switches the `memory` /
  `vault` groups off. The automatic brain/vault *hints* inside `search` follow
  those switches for scoped experts.
- Citations: `_resolve_cite_titles` already resolves titles via Paperless as
  the user → goes through the scoped client, so a made-up out-of-scope `[^id]`
  reveals nothing.

### 5.3 Skills at runtime

- Assigned skills' **name + description** always go into the skills block.
- Bodies: if all assigned bodies together are ≤ 2 500 chars → inlined. Else a
  tool `use_skill(name)` (always-on for that expert) returns the body on demand
  — progressive disclosure, so an expert with eight skills does not burn a local
  model's 8k context. The block tells the model to load a skill before a task
  its description matches.
- `use_skill` is read-only, returns only skills assigned to the current expert.
  It joins `_TOOL_ALWAYS_ON` conditionally; the `_GROUPED_TOOLS` import guard
  must accept it.

### 5.4 Conversations

- A conversation records its expert (`chat_history` column `expert_id`).
  **The expert is fixed per conversation**: earlier turns may hold documents a
  narrower expert must not see. Switching the expert offers "New conversation
  with X" (optionally carrying the last question over).
- Expert picker: chat header next to the model picker, findings dock, and
  `/chat?expert=<pbrain_id>`. The expert's `default_model` preselects the model;
  the user can override per conversation.
- Werkbank hand-off (`create_kanban_task`) from a scoped expert: v1 of this
  feature does not scope Werkbank runs — the brief says which expert asked and
  the run uses the user's full access. Open question 2.

## 6. Study mode ("briefing") — the killer feature

### 6.1 Is it a good idea?

**Yes — with a narrower promise than "knows the documents".** What it buys:

1. **Better searches.** The biggest failure of a fresh chat on a local model is
   not lack of knowledge but *bad queries*: it doesn't know the insurer is
   "HUK-COBURG Allgemeine", that policies are tagged "Vertrag", that the archive
   is German. A briefing gives it the vocabulary, the parties and the time
   range — so its first `search_exact` hits.
2. **An honest overview** for the "what do I even have here?" question, which
   today costs 5–8 tool calls and still misses things.
3. **Proactive questions.** "3 policies renew in December" is something the
   expert can mention unprompted.

What it must not become: a **substitute for reading**. A model with a
plausible summary in its prompt will answer from it. Hence the design below
treats the briefing exactly like a dossier — a map, never evidence — and puts
the hard guarantees into code.

### 6.2 What the briefing contains

Two parts, clearly separated:

| Part | Made by | Content |
|---|---|---|
| **Archive map** | code only, always correct | counts per correspondent / type / tag / year, date range, newest 10 titles with ids, recurring series (same correspondent + type, monthly) |
| **Orientation** | model, validated | what this collection is about, key parties and their role, recurring matters, notable events with dates, open items, glossary of terms/abbreviations — every sentence carries `[#id]` refs |

Target size ≤ ~1 200 tokens total (fits an 8k local context with room for the
conversation). The map alone is often the most useful part — it is also what
the Universal expert gets by default (no model cost, whole archive).

### 6.3 How it is built

1. **List** the scoped documents as the user (`ScopedPaperlessClient`).
2. **Material**: per document the sidecar's `full_summary_summarized`, title,
   date, correspondent, type — no PDF, no vision. Documents without a sidecar
   are listed in the map and counted as "not studied" (a gap, stated).
3. **Cost preview before start**: "143 documents · ~6 model calls · local model
   qwen3.8" (or the Claude cost estimate). The user confirms.
4. **Map** batches of ~25 summaries → notes with `[#id]` (schema-constrained).
   **Reduce** the notes + the archive map → the orientation.
5. **Caps**: above 400 documents, the newest 200 plus up to 5 per correspondent
   are studied; the briefing says so ("studied 260 of 1 140 documents").
6. **Code checks** (Werkbank-style, not overridable):
   - every `[#id]` must be in the studied set → sentence dropped otherwise;
   - numbers and long words must appear in the cited summaries
     (`werkbank.v2.checks.unsupported_tokens`) → sentence dropped;
   - dates must parse; a sentence without any ref is dropped from "events"
     and "open items" (allowed in "what this is about").
7. Runs app-side as a background job: progress in a store, the page polls
   (a run survives the tab), local-lane calls go through
   `werkbank.llm_lane.yield_to_chat` and `inference_activity.track`, WoL via
   `wol.ensure_awake`. Stop/resume like a Werkbank run; never auto-resumed
   after restart.

### 6.4 How it is used

- Written to `PaperlessBrain Experts/Briefings/<name>.md` (frontmatter:
  `expert`, `built`, `model`, `studied_ids`, `last_doc_id`, `skipped`). Not
  embedded (it would echo into every retrieval as pseudo-evidence).
- Injected under a code header: *"Your orientation from studying these documents
  earlier. It is a map, not evidence: never state a fact from it without reading
  the document through a tool and citing the tool result."*
- The refs inside it are written as `(#241)`, **not** `[^241]` — so the
  citation machinery cannot turn a briefing ref into a citation. Only tool
  results print citable refs.
- **Staleness**: new in-scope documents since `last_doc_id` → the expert card
  shows "7 new documents since briefing"; "Update" re-runs map on the new ones
  and reduce with the old orientation (incremental, like dossiers). A scope
  change marks it outdated.
- If the active model's context is small (`_fetch_ollama_ctx`), the
  orientation is dropped first, the map last.

## 7. UI

### 7.1 `/experts` — desktop

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Experts                                          [＋ New expert] [Skills ▸]  │
├─────────────────────────┬────────────────────────────────────────────────────┤
│ ◎ Universal      built-in│  🛡 Versicherung                        Chat ▸  ⋯ │
│ 🛡 Versicherung   143 docs│  ┌Profile┬Access┬Skills 3┬Tools┬Briefing─────────┐│
│ 🧾 Steuern        388 docs│  │ Access — opt-in                               ││
│ 🏠 Wohnung         61 docs│  │ Correspondents  [Allianz ✕][HUK-Coburg ✕] [+] ││
│                          │  │ Document types  [Police ✕][Rechnung ✕]    [+] ││
│                          │  │ Tags            any                       [+] ││
│                          │  │                                               ││
│                          │  │ ▣ Sees 143 of 2 031 documents   preview list ›││
│                          │  │                                               ││
│                          │  │ Briefing  ● current · 2 Nov · qwen3.8         ││
│                          │  │ "143 studied · 0 skipped"   [Update] [View]   ││
│                          │  └───────────────────────────────────────────────┘│
└─────────────────────────┴────────────────────────────────────────────────────┘
```

- **List** (left): icon, name, document count, briefing state dot
  (current / outdated / none — state, so it may carry `--c-warn` when outdated).
- **Editor tabs**: Profile (name, icon, personality with live char counter and
  "how this is used" help), Access (chip selectors fed by the user's Paperless
  maps, searchable, live count, "preview 10 documents"), Skills (assigned list,
  add from library, reorder), Tools (group switches as in chat, with the
  prompt-block consequence explained: "off = the expert is never told this
  tool exists"), Briefing (state, cost preview, build/update/view).
- **Briefing tab shows the briefing itself**, once built — the user should
  see what their expert "knows" before trusting it:

  ```
  Briefing   ● current · built 2 Nov · qwen3.8 · 143 studied · 0 skipped
             a map, not evidence — the expert reads documents before answering
  ┌ Archive map (code) ──────────────────────────────────────────────┐
  │ Allianz 61 · HUK-Coburg 82   Police 18 · Rechnung 125            │
  │ 2019–2026  ▁▂▃▅▇▆▅▃  (docs per year)                             │
  │ Series: HUK Beitragsrechnung, monthly since 2021                 │
  ├ Orientation (model, checked) ────────────────────────────────────┤
  │ What this is about  Two car policies and one household …  (#241) │
  │ Key parties         HUK-Coburg — Kfz since 2021 (#88) …          │
  │ Notable events      2025-12-01 premium +8 % (#241)               │
  │ Open items          Notice period for policy 4711 unclear (#241) │
  │ Glossary            SF-Klasse = no-claims class …                │
  ├ Checks ──────────────────────────────────────────────────────────┤
  │ 3 sentences dropped (2 unsupported numbers, 1 unknown id)  show ›│
  └──────────────────────────────────────────────────────────────────┘
  [Update · 7 new documents]   [Rebuild]   [Open in note explorer]
  ```

  - Every `(#id)` opens the document dialog (Paperless as the user, scoped).
  - The **checks panel** lists what code dropped and why — the honesty
    mechanism made visible, so a thin briefing reads as "checked", not
    "broken".
  - Map and orientation are visually separated and labelled by who made them
    (code vs. model) — the same distinction the expert's prompt makes.
  - Read-only here; the file is editable in the note explorer like any vault
    note, and an edited briefing is marked "edited by you" (hash vs. the
    stored `built_sha`) and still passes the same checks on next use.
  - Phone: map collapsed to one summary line, orientation sections as
    expandable rows.
- **"Chat ▸"** opens `/chat?expert=…`.
- Autosave with the same hash-check as the note editor (`save_note`); Universal
  shows Profile + Skills only.

### 7.2 Skill library + wizard

```
Skills                                        [＋ New skill] [✨ Wizard]
 MY SKILLS                     used by
  Versicherungspolice lesen    Versicherung, Universal     🔒 private
  Reklamation schreiben        Wohnung                     🌐 shared
 SHARED BY OTHERS (read-only)
  Steuerbescheid erklären      alice                       [Use] [Duplicate]
```

**Wizard** (dialog, stepper — one question per screen on phone):

1. **Model** — `model_select` over the user's registry (any lane).
2. **Goal** — "What should the expert be able to do?" + optional example
   question and a good answer (few-shot material).
3. **Interview** — the model asks 3–5 clarifying questions (structured JSON:
   question + suggested answers as chips); the user answers or skips.
4. **Draft** — name, description ("Use when …"), instructions as numbered steps
   with an output format. Editable inline.
5. **Lint (code)**: description present and phrased as a trigger; body ≤ 4 000
   chars; tool names mentioned exist in `TOOL_DEFINITIONS` (an invented
   `search_policies` would be followed by the model and fail silently —
   flagged with the real alternatives); no tool the target experts have
   switched off.
6. **Try it** — run the example question with and without the skill on the
   chosen expert, side by side (two short chat turns, read-only tools). The
   user sees whether it actually helps — this is the step that gets *better
   results*, not just more text.
7. **Save** — private or shared, assign to experts.

### 7.3 Phone

- `/experts`: list → editor as a full screen with tabs scrolling horizontally
  in their own container; chip pickers open as full-screen search lists.
- Wizard: one step per screen, sticky "Next" above the safe area.
- Expert picker in chat: a compact chip in the header ("🛡 Versicherung ▾") →
  bottom sheet list with doc counts.
- Verify at 390 px with Playwright.

## 8. Visual discipline

- Expert icons are **user-picked material icons in text colour** — no colour per
  expert. Colour stays reserved for state (briefing outdated → `--c-warn`) and
  the two user-colour exceptions (tags, finding topics). A third colour
  exception for experts is not added.
- Sentence case, no emoji in chrome (the emoji in the sketches above stand for
  icons), tokens not hexes, `break-all` on ids/model names.

## 9. Phases

One phase at a time; tests green, 390 px check for UI phases, one deploy per batch.

### Phase 1 — Expert model + scope enforcement (no new page)
- `services/experts.py`: load/save expert files, Universal bootstrap +
  migration from `custom_instructions`.
- `ScopedPaperlessClient`, `_user_paperless()` takes the conversation's expert.
- Tool audit (§5.2) + test pinning every document tool against an out-of-scope id.
- `build_system_prompt(expert=…)`; tool groups ∩ `tools_off`.
- `chat_history.expert_id`; picker in `/chat` header (expert fixed per
  conversation).
- Settings > AI: field removed, link to Experts.
- **Accept:** a scoped expert never returns, cites or renders an out-of-scope
  document through any tool; Universal behaves exactly like today's chat with
  the migrated instructions; existing tests unchanged.

### Phase 2 — `/experts` page
- List + editor (Profile, Access with live count, Tools), autosave, delete
  (not Universal), icon picker. Nav item. i18n.
- **Accept:** create → chat with it → scope visible in answers; 390 px clean.

### Phase 3 — Skills
- `PaperlessBrain Skills/` folder (excluded from embedding), sharing,
  shared-by-others section, assign by reference, duplicate.
- Skills block + `use_skill` with the inline/on-demand threshold.
- Library UI (no wizard yet). Findings dock picker (findings workspace phase 5).
- **Accept:** a shared skill appears for another user at once,
  read-only; un-sharing removes it from their experts; `use_skill` cannot load
  an unassigned skill.

### Phase 4 — Wizard
- Steps 1–7, lint, side-by-side try.
- **Accept:** a lint finding for an invented tool name; the try step runs
  with read-only tools only.

### Phase 5 — Briefings
- Archive map (code) → Universal gets it on by default.
- Study job (map/reduce, checks, caps, cost preview, progress store, stop/resume,
  yield-to-chat, track, WoL), briefing file, staleness, prompt injection with
  context-size fallback.
- Briefing view in the Briefing tab (map, orientation, checks panel).
- **Accept:** an injected wrong number in a model note is dropped by the check (and listed in the checks panel);
  an out-of-set `#id` is dropped; a briefing ref never renders as a citation;
  a chat on an 8k model with briefing still has room for 6 turns.

## 10. Invariants (to add to CLAUDE.md when built)

- **The expert scope is enforced in the Paperless client, not in the prompt.**
  Every document tool goes through `_user_paperless()`; out of scope = not found.
- **Personality text uses the existing custom-instructions slot** (header,
  2 000-char cap). Experts never edit `_CORE` or tool blocks.
- **The Universal expert cannot be narrowed, renamed or deleted** — not from the
  UI, not from an edited file.
- **An expert is fixed per conversation.**
- **Skills and experts are configuration, never embedded**; a shared skill is
  read from its owner's folder while it says `common: true`, never copied
  except by the user's explicit "Duplicate".
- **Every expert keeps memory and vault**; only other groups can be off.
- **A briefing is a map, not evidence**: code-checked, written with `(#id)` not
  citation markers, not embedded, never cited.
- **Expert and briefing files are not embedded.**
- **No agent writes skills or experts.** The wizard drafts, the user saves.

## 11. Open questions — decided 2026-11-02

1. Experts in proactive triage / deep dives: **no.** A dossier may name a
   default expert for its *chat* dock (`expert:` in its frontmatter), nothing more.
2. Werkbank runs from a scoped expert: **no** — experts do not go to the
   Werkbank.
3. Per-expert memory: **no** — every expert shares the user's memory and vault.
