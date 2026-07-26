# PaperSage — Vault-Backed Memory: Implementation Tasks

Phased for Claude Code. Each phase has concrete tasks + acceptance criteria. Read
`vault-architecture.md` first; it is authoritative for design decisions.

Module suggestion: `papersage/vault/` (sync engine, git wrapper, frontmatter, chunker,
routing). Reuse the existing embedding helper and Chroma client — do not reimplement them.

---

## Phase 0 — Settings & per-user paths

**Tasks**
- [ ] Add settings (pydantic-settings): `vault_root` (base, e.g. `/mnt/vaults`),
      `brain_subfolder` (default `PaperSage Memory`), `vault_sync_cooldown_s` (default 3).
- [ ] Resolve per-user vault path `<vault_root>/<username>` and the brain subfolder beneath it.
- [ ] On first access for a user: ensure the directory + brain subfolder exist; pre-create the
      brain subfolder so it's never missing.
- [ ] Settings UI: surface the resolved per-user vault path read-only, and a short note telling
      the user where to point their Obsidian/WebDAV sync (the **same** directory — see arch §8).

**Acceptance**
- A new username yields an isolated, existing vault dir with a `PaperSage Memory/` subfolder.

---

## Phase 1 — Git wrapper & change detection

**Tasks**
- [ ] `git_wrapper.py` using `subprocess` + `git -C <path>`: `ensure_repo()` (init + `.gitignore`
      ignoring all-but-`*.md` + baseline commit), `diff_name_status()`, `commit(msg)`.
- [ ] `.gitignore` content: ignore everything, un-ignore `*.md` (+ any other ingest types).
- [ ] Parse `--name-status` into a typed change list: `Added|Modified|Deleted|Renamed(old,new)`,
      with the rename-similarity score so a 100% rename can skip re-embed.
- [ ] Document the Remotely Save side requirement: exclude `.git/` (this is a deployment note,
      not code — put it in the settings UI help text).

**Acceptance**
- On a fresh vault: `ensure_repo()` creates a repo, baseline commit, `.gitignore` excludes
  non-`.md`.
- Editing a `.md` then calling `diff_name_status()` returns exactly that file as `Modified`.
- A rename of an unchanged `.md` is reported as `Renamed` with 100% similarity.

---

## Phase 2 — Frontmatter, chunking, routing

**Tasks**
- [ ] `frontmatter.py`: read/parse YAML frontmatter + body; write back frontmatter atomically
      (temp + rename within mount). Prefer PyYAML; no new heavy dep.
- [ ] `psage_id` lifecycle: read existing; if missing on a brain file, generate UUID + write back.
- [ ] Markdown-aware chunker for vault files: split on heading hierarchy, fall back to paragraph,
      max token budget; emit `(chunk_index, heading_path, text)`.
- [ ] Router: `path under brain_subfolder → brain target; else → vault target`.

**Acceptance**
- A brain file without `psage_id` gains one persisted to disk; re-running is a no-op (no churn).
- A multi-heading vault file yields stable, atomic chunks with correct `chunk_index`/`heading_path`.

---

## Phase 3 — Sync engine

**Tasks**
- [ ] `sync_user(username)`:
  1. `ensure_repo()`
  2. `diff_name_status()`
  3. for each change → route → embed/upsert or delete (per arch §4/§6)
  4. `commit()` **last**
- [ ] Embed via existing helper; brain = whole file, vault = per chunk.
- [ ] Upsert keys: brain `id = psage_id`; vault `id = f"{psage_id}:{chunk_index}"`.
- [ ] Metadata per arch §6 (include `path` on every entry).
- [ ] Deletion: on `Deleted(path)` → `collection.delete(where={"path": path})`.
- [ ] Rename 100% → update `path` metadata only; otherwise delete-old + add-new.
- [ ] Per-user `asyncio.Lock`; "at most once per turn" guard (per-turn flag or cooldown).
- [ ] Idempotency: re-running `sync_user` with no on-disk changes is a no-op and commits nothing.

**Acceptance**
- Add/modify/delete/rename a file → exactly the right collection entries appear/update/vanish.
- Killing the process between embed and commit, then re-running, converges to the same state
  (no duplicates, no missing entries).
- Two concurrent `sync_user` calls for one user serialize; no `index.lock` errors.

---

## Phase 4 — Tool layer

**Tasks**
- [ ] `vault_search(query, k)` tool against the `vault` collection (mirror `brain_search`).
      Query embedding uses the e5 instruct query prefix via the existing helper.
- [ ] Re-implement brain write tools on top of the sync primitives (signatures unchanged):
  - [ ] `create_memory(text, tags?)` → gen id, write file, embed, upsert, commit.
  - [ ] `update_memory(psage_id, text)` → resolve path via metadata, rewrite body + `updated`,
        re-embed, upsert, commit.
  - [ ] `delete_memory(psage_id)` → resolve path, `rm`, delete entry, commit.
- [ ] All brain writes take the per-user lock and run **synchronously** (agent sees its own
      write immediately).
- [ ] `brain_search` read path unchanged.

**Acceptance**
- `create_memory` then `brain_search` in the same turn returns the new fact.
- The agent's tool contract (names, params, return shapes) is byte-identical to before for
  read tools; write tools behave identically from the agent's view.

---

## Phase 5 — Interaction trigger

**Tasks**
- [ ] Hook `sync_user(username)` at the start of handling a user turn, before brain/vault
      retrieval; guarded by lock + once-per-turn.
- [ ] Dashboard **"Sync now"** button → same path, manual trigger.
- [ ] (Optional) lightweight "syncing memory…" indicator if a turn's sync exceeds a threshold.

**Acceptance**
- No background scheduler exists anywhere.
- A user's edits in Obsidian are reflected on their next interaction (or on manual sync).
- A rapid multi-tool turn runs the sync at most once.

---

## Phase 6 — Frontend memory module

**Tasks**
- [ ] Remove the old Chroma-direct "Gedächtnis" module.
- [ ] Build a slim **file-backed** brain viewer/editor: lists facts from `PaperSage Memory/`,
      edit/delete write the file + re-embed (reuse the write primitives). Brain only.

**Acceptance**
- Editing a fact in the UI updates the `.md` on disk, re-embeds, and commits.
- The vault knowledge base is **not** editable here (managed in Obsidian).

---

## Phase 7 — Tests

Mock at the network/filesystem boundary; test application logic, not git/Chroma internals.
`pytest` + `pytest-asyncio` + `AsyncMock`.

**Tasks**
- [ ] Git wrapper on a real temp repo (init, diff add/modify/delete/rename, commit-last).
- [ ] Frontmatter round-trip + `psage_id` backfill idempotency.
- [ ] Chunker: heading split, paragraph fallback, stable `chunk_index`.
- [ ] Sync engine: each change type → correct Chroma mutation (Chroma client mocked/in-memory).
- [ ] Crash-safety: embed-then-kill-before-commit → re-run converges.
- [ ] Concurrency: two `sync_user` calls serialize via the lock.
- [ ] Tool contract: brain read tools unchanged; write tools produce correct file + index state.

---

## Suggested order

0 → 1 → 2 → 3 → (4 ∥ 6) → 5 → 7. Phases 4 and 6 can proceed in parallel once 3 is green.
